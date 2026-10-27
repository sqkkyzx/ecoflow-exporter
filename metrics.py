"""Prometheus metrics for received EcoFlow telemetry.

Only fresh, non-cached readings supplied by the collector are exposed as device
values. The per-metric availability and receive-age series remain useful when a
device value disappears. This module never logs or labels account credentials.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Callable

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily


def _number(value, *, allow_bool: bool = False) -> float | None:
    if isinstance(value, bool) and not allow_bool:
        return None
    if not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _soc(value) -> float | None:
    number = _number(value)
    return number if number is not None and 0 <= number <= 100 else None


def _output_power(value) -> float | None:
    number = _number(value)
    return abs(number) if number is not None else None


def _flag(value) -> float | None:
    number = _number(value, allow_bool=True)
    return number if number in (0, 1) else None


@dataclass(frozen=True)
class MetricSpec:
    name: str
    help: str
    sources: tuple[str, ...]
    normalize: Callable = _number


# The most recently received valid candidate wins, rather than a fixed CMS/BMS
# preference hiding a fresh heartbeat behind an old display-property value.
DEVICE_METRICS = (
    MetricSpec("battery_soc_percent", "Battery state of charge in percent.",
               ("cms_batt_soc", "bms_batt_soc", "bms_heartbeat_display_soc", "bms_heartbeat_soc"), _soc),
    MetricSpec("ac_input_power_watts", "AC input power in watts.", ("pow_get_ac_in",)),
    MetricSpec("ac_output_power_watts", "Absolute AC output power in watts.",
               ("pow_get_ac_out", "pow_get_ac"), _output_power),
    MetricSpec("total_input_power_watts", "Total input power in watts.", ("pow_in_sum_w",)),
    MetricSpec("total_output_power_watts", "Absolute total output power in watts.",
               ("pow_out_sum_w",), _output_power),
    MetricSpec("solar_input_power_watts", "Solar input power in watts.", ("pow_get_pv",)),
    MetricSpec("ac_connected", "Native AC charger connection flag, 1 connected or 0 disconnected; verify on the device.",
               ("plug_in_info_ac_charger_flag",), _flag),
)


def _selected(values: dict, spec: MetricSpec, now: float) -> tuple[float, float, float] | None:
    candidates = []
    for source in spec.sources:
        item = values.get(source)
        if not isinstance(item, dict):
            continue
        value = spec.normalize(item.get("value"))
        timestamp = _number(item.get("timestamp"))
        if value is None or timestamp is None or timestamp < 0:
            continue
        # A receipt timestamp materially ahead of the local wall clock is not
        # evidence of a fresh reading; do not turn it into a negative age.
        if timestamp > now:
            continue
        candidates.append((value, timestamp, max(0.0, now - timestamp)))
    return max(candidates, key=lambda entry: entry[1]) if candidates else None


def _gauge(name: str, help_text: str, labels: list[str], value: float, *, extra_labels=None):
    family = GaugeMetricFamily(name, help_text, labels=["device", *(extra_labels or [])])
    family.add_metric(labels, value)
    return family


def _counter_value(value) -> float:
    number = _number(value)
    return number if number is not None and number >= 0 else 0


class _EcoFlowCollector:
    def __init__(self, snapshot: dict):
        self.snapshot = snapshot

    def collect(self):
        snapshot = self.snapshot
        device = str(snapshot.get("device") or "river3")
        now = _number(snapshot.get("now"))
        if now is None:
            now = 0.0
        stale = _number(snapshot.get("stale_seconds"))
        if stale is None or stale <= 0:
            stale = 120.0
        connected = snapshot.get("connected") is True
        subscribed = snapshot.get("subscribed") is True
        ready_transport = connected and subscribed
        labels = [device]

        probe_success = snapshot.get("probe_success")
        if isinstance(probe_success, bool):
            yield _gauge("probe_success", "Whether fresh core telemetry is available for this probe.",
                         labels, int(probe_success))
        yield _gauge("ecoflow_mqtt_connected", "Whether the MQTT connection is established.", labels, int(connected))
        yield _gauge("ecoflow_mqtt_subscribed", "Whether the device subscriptions are acknowledged.", labels, int(subscribed))
        yield _gauge("ecoflow_login_success", "Whether the most recent account login succeeded.", labels,
                     int(snapshot.get("login_ok") is True))

        values = snapshot.get("values")
        if not isinstance(values, dict):
            values = {}
        available = GaugeMetricFamily(
            "ecoflow_metric_available",
            "Whether this device metric has a valid fresh reading and an active MQTT subscription.",
            labels=["device", "metric"],
        )
        ages = GaugeMetricFamily(
            "ecoflow_metric_age_seconds", "Seconds since the selected device field was last received, including stale fields.",
            labels=["device", "metric"],
        )
        received = GaugeMetricFamily(
            "ecoflow_metric_last_received_timestamp_seconds", "Unix time when the selected device field was received.",
            labels=["device", "metric"],
        )
        any_available = False
        for spec in DEVICE_METRICS:
            selection = _selected(values, spec, now)
            current = bool(ready_transport and selection is not None and selection[2] <= stale)
            available.add_metric([device, spec.name], int(current))
            family = GaugeMetricFamily("ecoflow_" + spec.name, spec.help, labels=["device"])
            if selection is not None:
                value, timestamp, age = selection
                ages.add_metric([device, spec.name], age)
                received.add_metric([device, spec.name], timestamp)
                if current:
                    family.add_metric(labels, value)
                    any_available = True
            yield family
        yield available
        yield ages
        yield received
        yield _gauge("ecoflow_telemetry_available", "Whether at least one supported device metric is fresh and available.",
                     labels, int(any_available))

        last_live = _number(snapshot.get("last_live_timestamp"))
        if last_live is not None and 0 <= last_live <= now + 5:
            yield _gauge("ecoflow_last_live_received_timestamp_seconds", "Unix time of the last decoded non-cached device message.",
                         labels, last_live)
            yield _gauge("ecoflow_last_live_receive_age_seconds", "Seconds since the last decoded non-cached device message.",
                         labels, max(0.0, now - last_live))
        last_read = _number(snapshot.get("last_read_timestamp"))
        if last_read is not None and 0 <= last_read <= now + 5:
            yield _gauge("ecoflow_last_read_timestamp_seconds", "Unix time of the last read request attempt.", labels, last_read)
        reason = snapshot.get("last_read_reason")
        if reason in {"no_data", "stale_data"}:
            yield _gauge("ecoflow_last_read_reason", "Reason for the latest read request attempt.",
                         [device, reason], 1, extra_labels=["reason"])

        counters = snapshot.get("counters")
        if not isinstance(counters, dict):
            counters = {}
        definitions = (
            ("ecoflow_mqtt_messages", "MQTT messages received for the configured device.", "messages"),
            ("ecoflow_live_messages", "Decoded non-cached messages containing device fields.", "decoded_live"),
            ("ecoflow_cached_messages", "Cached or reply messages received for the configured device.", "cached"),
            ("ecoflow_decode_errors", "Messages that failed telemetry decoding.", "decode_errors"),
            ("ecoflow_mqtt_reconnects", "MQTT reconnect attempts after the initial connection.", "reconnects"),
            ("ecoflow_auth_errors", "Account or MQTT authentication failures.", "auth_errors"),
            ("ecoflow_read_errors", "Read request attempts that failed to enter the local MQTT publish queue.", "read_errors"),
            ("ecoflow_login_attempts", "EcoFlow account login attempts.", "login_attempts"),
            ("ecoflow_certificate_refreshes", "MQTT certification refresh attempts.", "cert_refreshes"),
            ("ecoflow_mqtt_connection_attempts", "MQTT connection attempts, including the initial connection.", "connection_attempts"),
        )
        for name, help_text, key in definitions:
            family = CounterMetricFamily(name, help_text, labels=["device"])
            family.add_metric(labels, _counter_value(counters.get(key, 0)))
            yield family
        reads = CounterMetricFamily("ecoflow_read_requests", "Read request attempts by trigger reason.", labels=["device", "reason"])
        reads.add_metric([device, "no_data"], _counter_value(counters.get("reads_no_data", 0)))
        reads.add_metric([device, "stale_data"], _counter_value(counters.get("reads_stale", 0)))
        yield reads


def render_metrics(snapshot: dict, config: dict | None = None) -> bytes:
    """Return Prometheus text exposition with a registry scoped to this scrape.

    ``values`` must contain only genuine non-cached live telemetry. Its timestamps
    are local receipt times, not unverified device-provided timestamps. ``config``
    is reserved for caller compatibility and is deliberately never exposed as
    labels, since it may contain account credentials.
    """
    registry = CollectorRegistry(auto_describe=False)
    registry.register(_EcoFlowCollector(snapshot))
    return generate_latest(registry)


__all__ = ["DEVICE_METRICS", "render_metrics"]
