#!/usr/bin/env python3
"""Background EcoFlow RIVER 3 cloud collector with a vmagent scrape endpoint."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import math
import os
from pathlib import Path
import random
import re
import signal
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import paho.mqtt.client as mqtt

from metrics import render_metrics
from protocol import app_client_id, decode_message, read_request
from recovery import ReadRecovery

VERSION = "0.1.0"
LOG = logging.getLogger("ecoflow_exporter")
API_HOSTS = {"api-cn.ecoflow.com", "api.ecoflow.com", "api-e.ecoflow.com"}
SOC_KEYS = ("cms_batt_soc", "bms_batt_soc", "bms_heartbeat_display_soc", "bms_heartbeat_soc")


class ConfigError(ValueError):
    pass


def env_secret(name: str, env: dict) -> str:
    direct, filename = env.get(name), env.get(name + "_FILE")
    if direct is not None and filename is not None:
        raise ConfigError(f"{name} 和 {name}_FILE 不可同時設定。")
    if filename is not None:
        try:
            with open(filename, "rb") as stream:
                raw = stream.read(65_537)
            if len(raw) > 65_536:
                raise ConfigError(f"{name}_FILE 超出大小限制。")
            direct = raw.decode("utf-8")
        except (OSError, UnicodeError):
            raise ConfigError(f"無法讀取 {name}_FILE。") from None
    if direct is None or direct == "":
        raise ConfigError(f"缺少 {name} 或 {name}_FILE。")
    return direct


def env_int(name: str, default: int, low: int, high: int, env: dict) -> int:
    try:
        value = int(env.get(name, default))
    except (TypeError, ValueError):
        raise ConfigError(f"{name} 必須是整數。") from None
    if not low <= value <= high:
        raise ConfigError(f"{name} 必須介於 {low}–{high}。")
    return value


@dataclass(frozen=True)
class Config:
    account: str = field(repr=False)
    password: str = field(repr=False)
    sn: str = field(repr=False)
    device_name: str = "river3"
    api_host: str = "api-cn.ecoflow.com"
    listen: str = "0.0.0.0"
    port: int = 9098
    stale_seconds: int = 120
    read_cooldown_seconds: int = 30
    reconnect_min_seconds: int = 5
    reconnect_max_seconds: int = 300
    auth_retry_seconds: int = 300
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        account = env_secret("ECOFLOW_ACCOUNT", env).strip().replace(" ", "")
        password = env_secret("ECOFLOW_PASSWORD", env)  # Preserve every password byte.
        sn = env_secret("ECOFLOW_SN", env).strip().upper()
        host = env.get("ECOFLOW_API_HOST", "api-cn.ecoflow.com").strip().lower()
        if host not in API_HOSTS:
            raise ConfigError("ECOFLOW_API_HOST 不在支援的 EcoFlow API 主機清單。")
        if not re.fullmatch(r"[A-Z0-9]{8,40}", sn):
            raise ConfigError("ECOFLOW_SN 必須為 8–40 位英數字。")
        if host == "api-cn.ecoflow.com":
            account = account.removeprefix("+86")
            if not re.fullmatch(r"[0-9]{11}", account):
                raise ConfigError("中國區 ECOFLOW_ACCOUNT 必須為 11 位手機號。")
        elif not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", account):
            raise ConfigError("此區域的 ECOFLOW_ACCOUNT 必須為 email。")
        name = env.get("ECOFLOW_DEVICE_NAME", "river3").strip()
        if not name or len(name) > 64 or any(ord(c) < 32 for c in name):
            raise ConfigError("ECOFLOW_DEVICE_NAME 必須為 1–64 位可顯示文字。")
        level = env.get("LOG_LEVEL", "INFO").upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ConfigError("LOG_LEVEL 必須為 DEBUG／INFO／WARNING／ERROR。")
        result = cls(
            account, password, sn, name, host,
            env.get("EXPORTER_LISTEN", "0.0.0.0"),
            env_int("EXPORTER_PORT", 9098, 1, 65535, env),
            env_int("ECOFLOW_STALE_SECONDS", 120, 10, 3600, env),
            env_int("ECOFLOW_READ_COOLDOWN_SECONDS", 30, 5, 3600, env),
            env_int("ECOFLOW_RECONNECT_MIN_SECONDS", 5, 1, 3600, env),
            env_int("ECOFLOW_RECONNECT_MAX_SECONDS", 300, 1, 86400, env),
            env_int("ECOFLOW_AUTH_RETRY_SECONDS", 300, 30, 86400, env), level,
        )
        if result.reconnect_min_seconds > result.reconnect_max_seconds:
            raise ConfigError("重連最短等待時間不可大於最長等待時間。")
        return result


class CloudError(RuntimeError):
    def __init__(self, stage: str, code: str = "unknown", auth: bool = False):
        # Only controlled stage names and a bounded code may reach logs.
        code = str(code)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", code):
            code = "unknown"
        self.stage, self.code, self.auth = stage, code, auth
        super().__init__(f"stage={stage} code={code}")


class CloudAPI:
    def __init__(self, config: Config):
        self.config = config
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                return None
        self.opener = urllib.request.build_opener(
            NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def request(self, stage: str, path: str, *, data=None, token=None, query=None):
        url = f"https://{self.config.api_host}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Accept": "application/json", "lang": "zh_CN"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=None if data is None else json.dumps(data).encode(), headers=headers)
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read(2_000_001)
        except urllib.error.HTTPError as exc:
            raise CloudError(stage, f"http_{exc.code}", exc.code in {401, 403, 429}) from None
        except (urllib.error.URLError, OSError, TimeoutError):
            raise CloudError(stage, "network") from None
        if len(raw) > 2_000_000:
            raise CloudError(stage, "oversize")
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            raise CloudError(stage, "invalid_json") from None
        if not isinstance(result, dict):
            raise CloudError(stage, "invalid_shape")
        if str(result.get("code")) != "0":
            raise CloudError(stage, result.get("code", "missing_code"), auth=True)
        return result.get("data")

    def login(self) -> tuple[str, str]:
        cfg = self.config
        payload = {"password": base64.b64encode(cfg.password.encode()).decode(),
                   "scene": "IOT_APP", "appVersion": "1.0.0", "userType": "ECOFLOW",
                   "oauth": {"bundleId": "com.ef.EcoFlow"}}
        payload["phone" if cfg.api_host == "api-cn.ecoflow.com" else "email"] = cfg.account
        data = self.request("login", "/auth/login", data=payload)
        try:
            token, uid = data["token"], str(data["user"]["userId"])
        except (KeyError, TypeError):
            raise CloudError("login", "missing_credentials", auth=True) from None
        if not isinstance(token, str) or not token or not uid.isascii() or not uid.isdigit():
            raise CloudError("login", "invalid_credentials", auth=True)
        return token, uid

    def certification(self, token: str, uid: str) -> tuple[str, int, str, str]:
        data = self.request("certification", "/iot-auth/app/certification", token=token, query={"userId": uid})
        if not isinstance(data, dict):
            raise CloudError("certification", "invalid_shape")
        host = str(data.get("url", "")).lower().rstrip(".")
        if "://" in host:
            url = urllib.parse.urlsplit(host)
            if url.scheme not in {"mqtts", "ssl", "tls"} or url.username or url.path not in {"", "/"} or url.query or url.fragment:
                raise CloudError("certification", "invalid_broker")
            host = url.hostname or ""
        if not host.endswith(".ecoflow.com") or not re.fullmatch(r"[a-z0-9.-]+", host):
            raise CloudError("certification", "invalid_broker")
        try:
            port = int(data.get("port", 8883))
        except (TypeError, ValueError):
            raise CloudError("certification", "invalid_port") from None
        username, password = data.get("certificateAccount"), data.get("certificatePassword")
        if not 1 <= port <= 65535 or port in {80, 1883}:
            raise CloudError("certification", "invalid_port")
        if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
            raise CloudError("certification", "missing_mqtt_credentials")
        return host, port, username, password


class Collector:
    def __init__(self, config: Config, *, api=None, clock=time.time, monotonic=time.monotonic,
                 mqtt_factory=mqtt.Client):
        self.config, self.api = config, api or CloudAPI(config)
        self.clock, self.monotonic, self.mqtt_factory = clock, monotonic, mqtt_factory
        self.lock, self.stop_event = threading.RLock(), threading.Event()
        self.connected = self.subscribed = self.login_ok = False
        self.values = {}
        self.last_live_timestamp = self.last_read_timestamp = self.last_read_reason = None
        self.counters = {key: 0 for key in (
            "messages", "decoded_live", "cached", "decode_errors", "reads_no_data",
            "reads_stale", "read_errors", "reconnects", "auth_errors", "login_attempts",
            "cert_refreshes", "connection_attempts")}
        self.client = None
        self.client_id = None
        self.thread = threading.Thread(target=self._run, name="ecoflow-collector", daemon=True)

    def snapshot(self):
        with self.lock:
            return {"device": self.config.device_name, "connected": self.connected,
                    "subscribed": self.subscribed, "login_ok": self.login_ok,
                    "last_live_timestamp": self.last_live_timestamp, "now": self.clock(),
                    "stale_seconds": self.config.stale_seconds,
                    "values": {key: dict(value) for key, value in self.values.items()},
                    "counters": dict(self.counters), "last_read_timestamp": self.last_read_timestamp,
                    "last_read_reason": self.last_read_reason}

    def ready(self):
        snap = self.snapshot()
        if not snap["connected"] or not snap["subscribed"]:
            return False
        def fresh(key):
            value = snap["values"].get(key)
            return value is not None and 0 <= snap["now"] - value["timestamp"] <= snap["stale_seconds"]
        flag = snap["values"].get("plug_in_info_ac_charger_flag", {})
        valid_soc = any(fresh(key) and not isinstance(snap["values"][key]["value"], bool)
                        and isinstance(snap["values"][key]["value"], (int, float))
                        and math.isfinite(snap["values"][key]["value"])
                        and 0 <= snap["values"][key]["value"] <= 100 for key in SOC_KEYS)
        return valid_soc and fresh("plug_in_info_ac_charger_flag") and flag.get("value") in (0, 1)

    def receive(self, payload: bytes, *, retained=False, reply=False):
        with self.lock:
            self.counters["messages"] += 1
        try:
            values, _headers, encoding = decode_message(payload)
        except Exception:
            with self.lock:
                self.counters["decode_errors"] += 1
            return
        if encoding in {"unrecognised", "oversize"}:
            with self.lock:
                self.counters["decode_errors"] += 1
        if retained or reply or encoding == "cached_snapshot":
            with self.lock:
                self.counters["cached"] += 1
            return
        if values:
            now = self.clock()
            with self.lock:
                self.last_live_timestamp = now
                self.counters["decoded_live"] += 1
                self.values.update({key: {"value": value, "timestamp": now} for key, value in values.items()})

    def recovery_snapshot(self):
        snap = self.snapshot()
        return {"broker_connected": snap["connected"] and snap["subscribed"],
                "fields": {key: {"value": value["value"],
                                 "stale": not 0 <= snap["now"] - value["timestamp"] <= snap["stale_seconds"]}
                           for key, value in snap["values"].items()}}

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        with self.lock:
            client = self.client
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass
        if self.thread.is_alive():
            self.thread.join(timeout=25)

    def _reset_connection(self):
        with self.lock:
            self.connected = self.subscribed = False
            # Values from an old MQTT session must never become current merely
            # because a reconnect succeeds. Keep the historical receive gauge.
            self.values.clear()

    def _session(self, uid, certification):
        host, port, username, password = certification
        done, outcome = threading.Event(), {"reason": "network", "established": False}
        if self.client_id is None:
            self.client_id = app_client_id(uid)
        client = self.mqtt_factory(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                                   client_id=self.client_id, protocol=mqtt.MQTTv311,
                                   reconnect_on_failure=False)
        client.username_pw_set(username, password)
        client.tls_set_context(ssl.create_default_context())
        client.connect_timeout = 20
        with self.lock:
            self.client = client
            self.counters["connection_attempts"] += 1
        topic = f"/app/device/property/{self.config.sn}"
        get_topic = f"/app/{uid}/{self.config.sn}/thing/property/get"
        reply_topic = get_topic + "_reply"

        def on_connect(c, userdata, flags, reason, properties):
            if reason.is_failure:
                outcome["reason"] = "auth" if reason.value in {134, 135} else "network"
                LOG.warning("MQTT connection rejected code=%d", reason.value)
                done.set()
                return
            with self.lock:
                self.connected = True
            c.subscribe([(topic, 1), (reply_topic, 1)])

        def on_subscribe(c, userdata, mid, reasons, properties):
            success = bool(reasons) and all(not reason.is_failure for reason in reasons)
            with self.lock:
                self.subscribed = success
            if success:
                outcome["established"] = True
                LOG.info("Cloud subscription ready")
            else:
                outcome["reason"] = "auth"
                LOG.warning("Cloud subscription rejected")
                done.set()

        def on_disconnect(c, userdata, flags, reason, properties):
            self._reset_connection()
            done.set()

        def on_message(c, userdata, message):
            if message.topic == topic:
                self.receive(message.payload, retained=message.retain)
            elif message.topic == reply_topic:
                self.receive(message.payload, retained=message.retain, reply=True)

        client.on_connect, client.on_subscribe = on_connect, on_subscribe
        client.on_disconnect, client.on_message = on_disconnect, on_message
        started, loop_started = self.monotonic(), False
        recovery = ReadRecovery(self.config.read_cooldown_seconds)
        try:
            client.connect(host, port, keepalive=30)
            client.loop_start()
            loop_started = True
            while not self.stop_event.is_set() and not done.is_set():
                now = self.monotonic()
                with self.lock:
                    subscribed = self.subscribed
                if not subscribed and now - started > 30:
                    LOG.warning("MQTT connection/subscription timeout")
                    break
                reason = recovery.request_reason(now, self.recovery_snapshot()) if subscribed else None
                if reason:
                    with self.lock:
                        self.counters["reads_no_data" if reason == "no_data" else "reads_stale"] += 1
                        self.last_read_timestamp, self.last_read_reason = self.clock(), reason
                    result = client.publish(get_topic, read_request("protobuf"), qos=1, retain=False)
                    if result.rc != mqtt.MQTT_ERR_SUCCESS:
                        with self.lock:
                            self.counters["read_errors"] += 1
                        LOG.warning("Read request enqueue failed")
                    else:
                        LOG.info("Read request trigger=%s", reason)
                self.stop_event.wait(0.5)
        except (OSError, TimeoutError, ValueError):
            LOG.warning("MQTT transport failed")
        finally:
            try:
                client.disconnect()
            except Exception:
                pass
            if loop_started:
                client.loop_stop()
            self._reset_connection()
            with self.lock:
                self.client = None
        return outcome["reason"], outcome["established"]

    def _run(self):
        cfg = self.config
        token = uid = certification = None
        delay, last_cert = cfg.reconnect_min_seconds, None
        attempts = 0
        while not self.stop_event.is_set():
            try:
                if certification is None:
                    if token is None:
                        with self.lock:
                            self.counters["login_attempts"] += 1
                        token, uid = self.api.login()
                        with self.lock:
                            self.login_ok = True
                        LOG.info("App login succeeded")
                    with self.lock:
                        self.counters["cert_refreshes"] += 1
                    certification = self.api.certification(token, uid)
                    last_cert = self.monotonic()
                    LOG.info("MQTT credentials obtained")
                if attempts:
                    with self.lock:
                        self.counters["reconnects"] += 1
                attempts += 1
                reason, established = self._session(uid, certification)
                if self.stop_event.is_set():
                    break
                if established:
                    delay = cfg.reconnect_min_seconds
                if reason == "auth":
                    with self.lock:
                        self.counters["auth_errors"] += 1
                    certification = None
                    # Refresh certificates on explicit authorization rejection;
                    # throttle a newly issued certificate that was rejected too.
                    wait = max(0, cfg.auth_retry_seconds - (self.monotonic() - last_cert))
                    LOG.warning("MQTT authorization rejected; credential refresh delayed %.0fs", wait)
                else:
                    wait = delay + random.uniform(0, min(delay * 0.2, 5))
                    delay = min(cfg.reconnect_max_seconds, delay * 2)
                    LOG.warning("Cloud disconnected; reconnect in %.1fs", wait)
            except CloudError as exc:
                LOG.warning("Cloud request failed stage=%s code=%s", exc.stage, exc.code)
                if exc.auth:
                    with self.lock:
                        self.counters["auth_errors"] += 1
                        self.login_ok = False
                    token = uid = certification = None
                    wait = cfg.auth_retry_seconds
                else:
                    wait = delay
                    delay = min(cfg.reconnect_max_seconds, delay * 2)
            except Exception as exc:
                # Never log a raw exception string, response, topic, or traceback.
                LOG.error("Collector failure type=%s", type(exc).__name__)
                self._reset_connection()
                wait = cfg.reconnect_max_seconds
            self.stop_event.wait(wait)


def http_server(collector: Collector, host: str, port: int):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            if path == "/metrics":
                self.reply(200, render_metrics(collector.snapshot()), "text/plain; version=0.0.4; charset=utf-8")
            elif path == "/healthz":
                alive = collector.thread.is_alive() and not collector.stop_event.is_set()
                self.reply(200 if alive else 503, b"ok\n" if alive else b"collector stopped\n")
            elif path == "/readyz":
                ready = collector.ready()
                self.reply(200 if ready else 503, b"ready\n" if ready else b"waiting for fresh SOC and AC flag\n")
            elif path == "/":
                self.reply(200, b"EcoFlow exporter: /metrics /healthz /readyz\n")
            else:
                self.reply(404, b"not found\n")

        def reply(self, status, body, content_type="text/plain; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass  # Never log arbitrary URLs, headers, or request parameters.

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main():
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", flush=True)
        return 2
    logging.basicConfig(level=config.log_level, format="%(asctime)s %(levelname)s %(message)s")
    collector = Collector(config)
    try:
        server = http_server(collector, config.listen, config.port)
    except OSError:
        LOG.error("Cannot bind exporter HTTP port")
        return 2
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda signum, frame: collector.stop_event.set())
    collector.start()
    http_thread = threading.Thread(target=server.serve_forever, name="metrics-http", daemon=True)
    http_thread.start()
    LOG.info("EcoFlow exporter %s listening on port %d", VERSION, config.port)
    try:
        while not collector.stop_event.wait(1):
            pass
    finally:
        collector.stop()
        server.shutdown()
        server.server_close()
        http_thread.join(timeout=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
