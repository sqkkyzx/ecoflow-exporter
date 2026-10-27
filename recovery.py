"""Request telemetry only when missing or stale, with a response-wait cooldown."""

CORE_GROUPS = (
    ("cms_batt_soc", "bms_batt_soc", "bms_heartbeat_display_soc", "bms_heartbeat_soc"),
    ("pow_get_ac_in",),
    ("pow_get_ac_out", "pow_get_ac"),
    ("plug_in_info_ac_charger_flag",),
)
REQUIRED_GROUPS = (CORE_GROUPS[0], CORE_GROUPS[3])


def missing_or_stale(snapshot: dict) -> str | None:
    if snapshot.get("broker_connected") is not True:
        return None
    fields = snapshot.get("fields", {})
    # This exporter promises SOC and a native AC-presence flag. Continuous power
    # updates alone must not prevent recovery of a missing required group.
    if any(not any(isinstance(fields.get(name), dict) and "value" in fields[name]
                   for name in group) for group in REQUIRED_GROUPS):
        return "no_data"
    seen_groups = []
    for names in CORE_GROUPS:
        seen = [fields[name] for name in names
                if isinstance(fields.get(name), dict) and "value" in fields[name]]
        if seen:
            seen_groups.append(seen)
    if not seen_groups:
        return "no_data"
    # Only track groups this firmware has actually sent. Missing optional
    # fields must not keep the reader permanently requesting data.
    if any(all(entry.get("stale") is True for entry in group) for group in seen_groups):
        return "stale_data"
    return None


class ReadRecovery:
    def __init__(self, cooldown: float = 30):
        self.cooldown = cooldown
        self.last_attempt: float | None = None

    def request_reason(self, now: float, snapshot: dict) -> str | None:
        reason = missing_or_stale(snapshot)
        if reason is None:
            return None
        if self.last_attempt is not None and now - self.last_attempt < self.cooldown:
            return None
        # Reserve the attempt before publish: a failed local enqueue must also
        # wait before trying again, rather than retrying every event-loop tick.
        self.last_attempt = now
        return reason
