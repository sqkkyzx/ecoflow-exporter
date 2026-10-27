#!/usr/bin/env python3
"""Read-only RIVER 3 cloud/LAN connectivity experiment. No device controls."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from zoneinfo import ZoneInfo

from console_view import format_event
from credential_store import CredentialStoreError, get_password, set_password
from profile_store import load_profile, normalize_phone, save_profile
from read_recovery import ReadRecovery

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "vendor"))
FIELDS = {
    "cms_batt_soc", "bms_batt_soc", "pow_in_sum_w", "pow_out_sum_w",
    "pow_get_ac_in", "pow_get_ac_out", "pow_get_ac", "pow_get_pv",
    "plug_in_info_ac_charger_flag", "plug_in_info_ac_in_flag",
    "plug_in_info_ac_in_vol", "plug_in_info_ac_in_feq", "flow_info_ac_in",
    "flow_info_ac_out", "bms_dsg_rem_time", "cms_dsg_rem_time",
    "bms_chg_dsg_state", "cms_chg_dsg_state", "dev_sleep_state", "ups_alram",
    "bms_heartbeat_soc", "bms_heartbeat_display_soc", "bms_heartbeat_remaining_minutes",
}


class ProbeError(Exception):
    pass


def visible_input(prompt: str) -> str:
    if not sys.stdin.isatty():
        raise ProbeError("請在互動式 Terminal 執行，以便核對輸入。")
    value = input(prompt)
    if not value:
        raise ProbeError("輸入不可為空。")
    return value


def device_serial(value: str) -> str:
    value = value.strip().upper()
    if not (8 <= len(value) <= 40 and value.isascii() and value.isalnum()):
        raise ProbeError("設備 SN 格式不符：請輸入 App 中的完整英數字序號。")
    return value


def safe_error_code(value) -> str:
    value = str(value)
    return value if len(value) <= 16 and all(c.isalnum() or c in "_-" for c in value) else "unknown"


def request_json(host: str, path: str, *, data=None, headers=None, query=None):
    # Neither credentials nor raw service responses are printed or persisted.
    if host not in {"api-cn.ecoflow.com", "api.ecoflow.com", "api-e.ecoflow.com"}:
        raise ProbeError("不允許的 API 主機。")
    url = "https://" + host + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    hdr = {"Accept": "application/json", "lang": "zh_CN"}
    if data is not None:
        hdr["Content-Type"] = "application/json"
    hdr.update(headers or {})
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, headers=hdr)
    # Reject redirects instead of forwarding Authorization or login credentials.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            return None
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    try:
        with opener.open(req, timeout=20) as response:
            raw = response.read(2_000_001)
    except urllib.error.HTTPError as exc:
        raise ProbeError(f"HTTP {exc.code}，階段 {path}；已停止，未自動重試登入。") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ProbeError(f"HTTPS 連線或憑證驗證失敗，階段 {path}；未停用 TLS 驗證。") from None
    if len(raw) > 2_000_000:
        raise ProbeError("API 回覆超出大小限制。")
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ProbeError(f"API 非 JSON 回覆，階段 {path}。") from None
    if not isinstance(result, dict) or str(result.get("code")) != "0":
        code = safe_error_code(result.get("code")) if isinstance(result, dict) else "unknown"
        raise ProbeError(f"API 拒絕請求，階段 {path}，code={code}；已停止。")
    return result.get("data", {})


def cn_login_payload(phone: str, password: str) -> dict:
    phone = phone.replace(" ", "").removeprefix("+86")
    if len(phone) != 11 or not phone.isascii() or not phone.isdigit():
        raise ProbeError("中國區請輸入 11 位手機號，可含 +86 前綴。")
    return {"phone": phone, "password": base64.b64encode(password.encode()).decode(),
            "scene": "IOT_APP", "appVersion": "1.0.0", "userType": "ECOFLOW",
            "oauth": {"bundleId": "com.ef.EcoFlow"}}


def broker_settings(data: dict) -> tuple[str, int, str, str]:
    host = str(data.get("url", "")).lower().rstrip(".")
    if "://" in host:
        parsed = urllib.parse.urlparse(host)
        if parsed.scheme not in {"mqtts", "ssl", "tls"} or parsed.path not in {"", "/"} or parsed.username or parsed.query:
            raise ProbeError("MQTT 回傳的 broker URL 格式不符。")
        host = parsed.hostname or ""
    if not host.endswith(".ecoflow.com") or not all(c.isalnum() or c in ".-" for c in host):
        raise ProbeError("MQTT broker 不是 EcoFlow 網域，已停止。")
    try:
        port = int(data.get("port", 8883))
    except (TypeError, ValueError):
        raise ProbeError("MQTT port 格式不符。") from None
    if not 1 <= port <= 65535 or port in {80, 1883}:
        raise ProbeError("MQTT port 不符合 TLS 連線要求。")
    username = data.get("certificateAccount") or data.get("username")
    password = data.get("certificatePassword") or data.get("password")
    if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
        raise ProbeError("MQTT certification 缺少帳號／密碼欄位。")
    return host, port, username, password


def cloud_inputs(edit_profile=False):
    profile = None if edit_profile else load_profile()
    if profile:
        sn, phone = profile["sn"], profile["phone"]
        print(f"已載入設備 SN：{sn}\n已載入中國區帳號：{phone}")
        print("需要更換時，可在主選單選 4。")
        try:
            password = get_password(phone)
        except CredentialStoreError as exc:
            print(str(exc))
            password = None
        if password:
            print("密碼已從 macOS 鑰匙圈載入，不需重新輸入。")
            return sn, phone, password, False
        print("尚未取得已保存密碼；本次輸入成功登入後會保存到鑰匙圈。")
    else:
        sn = device_serial(visible_input("完整設備 SN（可在 App 設備資訊中查看）："))
        try:
            phone = normalize_phone(visible_input("中國區手機號："))
        except ValueError:
            raise ProbeError("中國區手機號格式不符。") from None
    password = visible_input("EcoFlow App 密碼（非短信驗證碼，可見輸入，登入成功後保存）：")
    return sn, phone, password, True


def cloud_credentials(phone: str, password: str, sn: str, save_password: bool):
    payload = cn_login_payload(phone, password)
    print("驗證中國區 App 登入……")
    data = request_json("api-cn.ecoflow.com", "/auth/login", data=payload)
    del payload
    try:
        token, uid = data["token"], str(data["user"]["userId"])
    except (KeyError, TypeError):
        raise ProbeError("登入回覆未包含 token／userId，協議可能變更。") from None
    if not isinstance(token, str) or not token or not uid.isascii() or not uid.isdigit():
        raise ProbeError("登入憑證欄位格式不符。")
    try:
        save_profile(phone, sn)
        if save_password:
            set_password(phone, password)
            print("帳號與設備 SN 已保存；密碼已保存到 macOS 鑰匙圈。")
    except CredentialStoreError as exc:
        print(f"帳號與 SN 已保存，但密碼未保存：{exc}")
    except (OSError, ValueError):
        print("本機設定保存失敗；本次繼續採集，下次可能仍需輸入。")
    del password, phone
    print("App 登入成功；取得 MQTT 憑證……")
    cert = request_json("api-cn.ecoflow.com", "/iot-auth/app/certification",
                        headers={"Authorization": "Bearer " + token}, query={"userId": uid})
    del token, data
    return uid, broker_settings(cert)


def filtered(data: dict) -> dict:
    if not isinstance(data, dict):
        return {}
    result = {}
    for key, value in data.items():
        key = re.sub(r"([A-Z])", r"_\1", key).lower() if isinstance(key, str) else ""
        if key not in FIELDS:
            continue
        if isinstance(value, str) and re.fullmatch(r"-?\d+(?:\.\d+)?", value):
            value = float(value) if "." in value else int(value)
        if isinstance(value, (int, float, bool)) and math.isfinite(value):
            result[key] = value
    return result


def decode_message(payload: bytes) -> tuple[dict, list[dict], str]:
    """Only decode telemetry; missing optional fields remain missing, including zero/False."""
    import ef_river3_pb2 as pb
    from google.protobuf.json_format import MessageToDict
    if len(payload) > 2_000_000:
        return {}, [], "oversize"
    try:
        obj = json.loads(payload)
    except (ValueError, UnicodeError):
        obj = None
    if isinstance(obj, dict):
        if obj.get("operateType") == "latestQuotas":
            data = obj.get("data", {})
            return filtered(data.get("quotaMap", {})), [], "cached_snapshot"
        return filtered(obj.get("params", {})), [], "json"
    try:
        raw = base64.b64decode(payload, validate=True)
    except (ValueError, base64.binascii.Error):
        raw = payload
    packet = pb.River3HeaderMessage()
    try:
        packet.ParseFromString(raw)
    except Exception:
        return {}, [], "unrecognised"
    merged, headers = {}, []
    message_types = {(254, 21): pb.River3DisplayPropertyUpload,
                     (254, 22): pb.River3RuntimePropertyUpload,
                     (32, 2): pb.River3CMSHeartBeatReport}
    bms_pairs = {(3, 1), (3, 2), (3, 30), (3, 50), (32, 1), (32, 3), (32, 50),
                 (32, 51), (32, 52), *((254, n) for n in range(24, 31))}
    for header in packet.header:
        kind = message_types.get((header.cmd_func, header.cmd_id))
        if (header.cmd_func, header.cmd_id) in bms_pairs:
            kind = pb.River3BMSHeartBeatReport
        if kind is None or not header.pdata:
            continue
        if header.enc_type not in {0, 1}:
            continue
        pdata = header.pdata
        if header.enc_type == 1 and header.src != 32:
            pdata = bytes((value ^ header.seq) & 255 for value in pdata)
        message = kind()
        try:
            message.ParseFromString(pdata)
        except Exception:
            continue
        decoded = MessageToDict(message, preserving_proto_field_name=True)
        if kind is pb.River3BMSHeartBeatReport:
            # Keep BMS schema fields distinct from display/CMS values.
            decoded = {"bms_heartbeat_" + {"soc": "soc", "f32_show_soc": "display_soc", "remain_time": "remaining_minutes"}[key]: value
                       for key, value in decoded.items() if key in {"soc", "f32_show_soc", "remain_time"}}
        merged.update(filtered(decoded))
        metadata = {"cmd_func": header.cmd_func, "cmd_id": header.cmd_id}
        if header.HasField("time_snap"):
            metadata["device_time_raw"] = header.time_snap
        headers.append(metadata)
    return merged, headers, "protobuf"


class Telemetry:
    def __init__(self):
        self.values = {}
        self.last_live = None

    def update(self, values: dict, now: float):
        if values:
            self.last_live = now
        for key, value in values.items():
            self.values[key] = (value, now)

    def snapshot(self, now: float, connected: bool, stale: float) -> dict:
        fields = {key: {"value": value, "age_seconds": round(max(0, now - ts), 1),
                        "stale": not connected or now - ts > stale}
                  for key, (value, ts) in self.values.items()}
        # Never infer grid status from watts, voltage, or an absent flag.
        flag = fields.get("plug_in_info_ac_charger_flag")
        ac = "unknown"
        if flag and not flag["stale"] and flag["value"] in (0, 1):
            ac = "flag_on" if flag["value"] else "flag_off"
        return {"broker_connected": connected, "ac_charger_flag_state": ac,
                "last_telemetry_receive_age_seconds": None if self.last_live is None else round(now - self.last_live, 1),
                "fields": fields}


class Recorder:
    def __init__(self, path: Path, verbose: bool = False):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.file = os.fdopen(fd, "w")
        self.path = path
        self.lock = threading.Lock()
        self.verbose = verbose

    def emit(self, event: str, **data):
        row = {"time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, **data}
        with self.lock:
            line = json.dumps(row, ensure_ascii=False, allow_nan=False)
            self.file.write(line + "\n")
            self.file.flush()
            if self.verbose:
                print(line, flush=True)
            else:
                rendered = format_event(event, data)
                if rendered:
                    clock = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%H:%M:%S")
                    print(f"\n[{clock}] {rendered}", flush=True)

    def close(self):
        self.file.close()


def read_request(request_format: str = "json") -> str | bytes:
    if request_format == "protobuf":
        import ef_river3_pb2 as pb
        packet = pb.River3HeaderMessage()
        header = packet.header.add(src=32, dest=32, seq=int(time.time() * 1000) & 0x7fffffff)
        setattr(header, "from", "Android")
        # Empty, read-only envelope used by River 3's upstream /get path.
        # Do not add pdata, cmd_func, cmd_id, or any device setting.
        return packet.SerializeToString()
    if request_format != "json":
        raise ProbeError("未知的唯讀請求格式。")
    return json.dumps({"from": "HomeAssistant", "id": str(secrets.randbelow(90000) + 999910000),
                       "version": "1.1", "moduleType": 0,
                       "operateType": "latestQuotas", "params": {}})


def app_client_id(user_id: str) -> str:
    # This is the format assigned by tolwi's actual login path. Its longer
    # gen_client_id helper is unused and must not be substituted here.
    return f"ANDROID_{uuid.uuid4().hex.upper()}_{user_id}"


def cloud_probe(args):
    import paho.mqtt.client as mqtt
    sn, phone, password, should_save = cloud_inputs(args.edit_profile)
    if not sn.startswith(("R654", "R655")):
        print("提示：此 SN 前綴不是已核查的 R654／R655；僅嘗試 RIVER 3 schema。")
    uid, (host, port, username, mqtt_password) = cloud_credentials(phone, password, sn, should_save)
    del phone, password
    client_id = app_client_id(uid)
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                         client_id=client_id, protocol=mqtt.MQTTv311, reconnect_on_failure=False)
    client.username_pw_set(username, mqtt_password)
    client.tls_set_context(ssl.create_default_context())
    client.connect_timeout = 20
    telemetry, lock = Telemetry(), threading.Lock()
    state = {"connected": False, "ready": False, "fatal": False, "messages": 0,
             "decoded": 0, "replies": 0, "read_requests": 0}
    data_topic = f"/app/device/property/{sn}"
    get_topic = f"/app/{uid}/{sn}/thing/property/get"
    reply_topic = get_topic + "_reply"
    topics = [(data_topic, 1), (reply_topic, 1)]
    out = Path(args.output) if args.output else ROOT / "results" / (time.strftime("cloud-%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2) + ".jsonl")
    recorder = Recorder(out, verbose=args.verbose)

    def on_connect(c, userdata, flags, reason_code, properties):
        with lock:
            state["connected"] = not reason_code.is_failure
            state["fatal"] = reason_code.is_failure
        recorder.emit("mqtt_connect", success=not reason_code.is_failure,
                      code=int(reason_code.value), reason=str(reason_code), auth_profile="android-simple")
        if not reason_code.is_failure:
            c.subscribe(topics)

    def on_subscribe(c, userdata, mid, reason_codes, properties):
        with lock:
            state["ready"] = bool(reason_codes) and all(not code.is_failure for code in reason_codes)
            state["fatal"] = not state["ready"]
        recorder.emit("mqtt_subscribe", success=state["ready"])

    def on_disconnect(c, userdata, flags, reason_code, properties):
        with lock:
            state["connected"] = False
            state["ready"] = False
        recorder.emit("mqtt_disconnect", code=int(reason_code.value), reason=str(reason_code))

    def on_message(c, userdata, msg):
        if msg.topic not in {data_topic, reply_topic}:
            return
        try:
            values, headers, encoding = decode_message(msg.payload)
            cached = bool(msg.retain) or msg.topic == reply_topic or encoding == "cached_snapshot"
            with lock:
                state["messages"] += 1
                if values and not cached:
                    telemetry.update(values, time.monotonic())
                    state["decoded"] += 1
                elif values and cached:
                    state["replies"] += 1
            recorder.emit("message", topic_kind="property" if msg.topic == data_topic else "get_reply",
                          encoding=encoding, retained=bool(msg.retain), cached=cached,
                          fields=values, headers=headers, payload_bytes=len(msg.payload))
            if values and cached:
                recorder.emit("read_reply", fields=values, cached=True,
                              freshness="unverified")
        except Exception as exc:
            # Exception text could contain raw payload or account identifiers.
            recorder.emit("decode_error", error_type=type(exc).__name__)

    client.on_connect, client.on_subscribe = on_connect, on_subscribe
    client.on_disconnect, client.on_message = on_disconnect, on_message
    print(f"連接 EcoFlow broker {host}:{port}；持續 {args.seconds} 秒。Ctrl+C 可停止。")
    print("本工具只記錄指標；不將旗標直接判定為停電。App 可先退出，觀察是否仍收到更新。")
    if args.request_snapshot:
        print(f"本輪按需讀取：沒有有效資料或重要資料過期時，才發送 {args.request_format} 唯讀 GET；每次嘗試後等待 {args.read_cooldown} 秒。資料正常時不發送。")
        print("請完全退出手機 App，期間不要打開設備頁。")
    recovery = ReadRecovery(args.read_cooldown)
    started, next_snapshot = time.monotonic(), 0.0
    loop_started = False
    try:
        client.connect(host, port, keepalive=30)
        client.loop_start()
        loop_started = True
        while time.monotonic() - started < args.seconds:
            now = time.monotonic()
            with lock:
                fatal, connected, ready = state["fatal"], state["connected"], state["ready"]
                snapshot = telemetry.snapshot(now, connected, args.stale)
            if fatal:
                raise ProbeError("MQTT 連接／訂閱被拒絕；已停止，未反覆重試。")
            if not connected and now - started > 25:
                raise ProbeError("MQTT 連線中斷或逾時；請查看結果檔中的階段資訊。")
            request_reason = recovery.request_reason(now, snapshot) if ready and args.request_snapshot else None
            if request_reason:
                # Only this fixed read topic and fixed read-only payload can be published.
                result = client.publish(get_topic, read_request(args.request_format), qos=1, retain=False)
                if result.rc == mqtt.MQTT_ERR_SUCCESS:
                    state["read_requests"] += 1
                recorder.emit("quota_read_requested", request_format=args.request_format,
                              trigger=request_reason, response_wait_seconds=args.read_cooldown,
                              accepted_locally=result.rc == mqtt.MQTT_ERR_SUCCESS)
            if now >= next_snapshot:
                recorder.emit("snapshot", **snapshot)
                next_snapshot = now + 10
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("已停止採集。")
    finally:
        client.disconnect()
        if loop_started:
            client.loop_stop()
        recorder.emit("summary", messages=state["messages"], decoded_live_messages=state["decoded"],
                      decoded_reply_messages=state["replies"], read_requests=state["read_requests"],
                      note="訊息接收時間不等於設備採樣時間；旗標需實機斷電對照。")
        recorder.close()
        print("結果檔：", out)


def lan_probe(args):
    value = args.ip or input("設備區網 IP（只檢查這一台的 TCP 8055）：").strip()
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise ProbeError("請輸入設備的數字 IP；不接受網址或網段。") from None
    if address.is_global or address.is_multicast or address.is_unspecified or address.is_loopback or address.is_reserved:
        raise ProbeError("此入口只接受區網設備地址。")
    try:
        with socket.create_connection((str(address), 8055), timeout=4):
            print("TCP 8055 可連接，但尚未證明協議支援 RIVER 3；未發任何指令。")
    except OSError:
        print("TCP 8055 無法連接。可能是未提供服務、網路隔離或設備不在線；不能僅憑此確認原因。")


def official_signature(access: str, secret: str, params: dict, nonce: str, timestamp: str):
    # This probe only uses flat GET parameters (none, or sn).
    parts = [f"{key}={params[key]}" for key in sorted(params)]
    parts += [f"accessKey={access}", f"nonce={nonce}", f"timestamp={timestamp}"]
    signature = hmac.new(secret.encode(), "&".join(parts).encode(), hashlib.sha256).hexdigest()
    return {"accessKey": access, "nonce": nonce, "timestamp": timestamp, "sign": signature}


def official_probe(args):
    print("官方 AK/SK 模式不使用 App 密碼；中國區 iot-open 可能回覆 503。")
    access, secret = visible_input("AccessKey："), visible_input("SecretKey（可見輸入）：")
    headers = official_signature(access, secret, {}, str(secrets.randbelow(900000) + 100000), str(int(time.time() * 1000)))
    devices = request_json(args.host, "/iot-open/sign/device/list", headers=headers)
    if not isinstance(devices, list):
        raise ProbeError("官方設備清單格式與預期不符，未列印原始回覆。")
    print("官方接口可列出設備數量：", len(devices))
    sn = device_serial(visible_input("要讀取的 RIVER 3 完整 SN："))
    params = {"sn": sn}
    headers = official_signature(access, secret, params, str(secrets.randbelow(900000) + 100000), str(int(time.time() * 1000)))
    quota = request_json(args.host, "/iot-open/sign/device/quota/all", headers=headers, query=params)
    if not isinstance(quota, dict):
        raise ProbeError("官方 quota 格式不符。")
    # Output only whitelisted metrics; keep account and device metadata private.
    metrics = filtered(quota)
    print(json.dumps({"official_quota_fields": metrics, "total_quota_fields": len(quota),
                      "river3_schema_matched": bool(metrics),
                      "note": "HTTP 成功不代表最新採樣；未匹配 schema 也不表示指標為 0。"}, ensure_ascii=False))


def show_status(args):
    files = sorted((ROOT / "results").glob("cloud-*.jsonl"))
    path = Path(args.file) if args.file else (files[-1] if files else None)
    if path is None:
        raise ProbeError("尚無採集結果。")
    last_signature = None
    try:
        while True:
            rows = []
            try:
                for line in path.read_text().splitlines():
                    try:
                        row = json.loads(line)
                        if isinstance(row, dict):
                            rows.append(row)
                    except ValueError:
                        pass  # The collector may be in the middle of writing a line.
            except OSError:
                raise ProbeError("無法讀取採集結果檔。") from None
            snapshots = [row for row in rows if row.get("event") == "snapshot"]
            summaries = [row for row in rows if row.get("event") == "summary"]
            if snapshots:
                snapshot = snapshots[-1]
                stamp = snapshot.get("time_utc", "")
                # Continue aging the view even if the collector stops writing.
                signature = (stamp, bool(summaries), int(time.monotonic() // 10))
                if signature != last_signature:
                    print("\n目前採集中文摘要（詳細資料仍保存在 JSONL）")
                    try:
                        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                        extra_age = max(0, (datetime.now(timezone.utc) - when).total_seconds())
                        print("摘要時間：" + when.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"))
                    except (ValueError, TypeError):
                        extra_age = 0
                    view = dict(snapshot)
                    view["fields"] = {key: {**entry, "age_seconds": entry.get("age_seconds", 0) + extra_age,
                                            "stale": entry.get("stale", False) or entry.get("age_seconds", 0) + extra_age > 120}
                                      for key, entry in snapshot.get("fields", {}).items() if isinstance(entry, dict)}
                    if view.get("last_telemetry_receive_age_seconds") is not None:
                        view["last_telemetry_receive_age_seconds"] += extra_age
                    if summaries or extra_age > 30:
                        view["broker_connected"] = False
                    print(format_event("snapshot", view), flush=True)
                    if summaries:
                        print(format_event("summary", summaries[-1]), flush=True)
                    last_signature = signature
            elif not args.follow:
                print("尚未收到第一筆摘要；請稍候。")
            if not args.follow or summaries:
                break
            time.sleep(3)
    except KeyboardInterrupt:
        print("已關閉摘要檢視；原採集程序繼續運行。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("wizard")
    lan = sub.add_parser("lan")
    lan.add_argument("--ip")
    cloud = sub.add_parser("cloud")
    cloud.add_argument("--seconds", type=int, default=600)
    cloud.add_argument("--stale", type=int, default=120)
    cloud.add_argument("--output")
    cloud.add_argument("--edit-profile", action="store_true", help="重新輸入並保存帳號、密碼和 SN")
    cloud.add_argument("--verbose", action="store_true", help="終端顯示詳細 JSON；結果檔一直保存詳細資料")
    cloud.add_argument("--request-snapshot", action="store_true", help="無有效資料或重要資料過期時，才發出唯讀請求")
    cloud.add_argument("--request-format", choices=["json", "protobuf"], default="json")
    cloud.add_argument("--read-cooldown", type=int, default=30, help="唯讀請求後等待回覆的秒數（非固定輪詢週期）")
    official = sub.add_parser("official")
    official.add_argument("--host", choices=["api-cn.ecoflow.com", "api.ecoflow.com", "api-e.ecoflow.com"], default="api-cn.ecoflow.com")
    status = sub.add_parser("status")
    status.add_argument("--file")
    status.add_argument("--follow", action="store_true")
    args = parser.parse_args()
    if args.mode == "wizard":
        print("EcoFlow 測試工具 v0.5：中文摘要／記住帳號\n1. 中國區 App 雲端 MQTT（純訂閱，10 分鐘）\n2. 區網 TCP 8055 連通檢查\n3. 官方 AccessKey/SecretKey（已持有 Key 才選）\n4. 更換雲端帳號／密碼／設備 SN\n5. 查看最近一次採集的中文摘要\n6. 不開 App 的按需讀取測試（無資料／資料過期才 GET）")
        choice = input("選擇 [1]：").strip() or "1"
        options = {"1": ["cloud"], "2": ["lan"], "3": ["official"],
                   "4": ["cloud", "--edit-profile"], "5": ["status", "--follow"],
                   "6": ["cloud", "--request-snapshot", "--request-format", "protobuf"]}
        if choice not in options:
            raise ProbeError("請選擇 1–6。")
        args = parser.parse_args(options[choice])
    if args.mode == "cloud":
        if not 10 <= args.seconds <= 86400 or not 10 <= args.stale <= 3600 or not 5 <= args.read_cooldown <= 3600:
            raise ProbeError("seconds 須介於 10–86400，stale 須介於 10–3600，read-cooldown 須介於 5–3600。")
        cloud_probe(args)
    elif args.mode == "lan":
        lan_probe(args)
    elif args.mode == "status":
        show_status(args)
    else:
        official_probe(args)


if __name__ == "__main__":
    try:
        main()
    except ProbeError as exc:
        print("測試停止：", exc, file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print("測試停止；未列印原始回覆／敏感資料。錯誤類型：", type(exc).__name__, file=sys.stderr)
        raise SystemExit(3)
