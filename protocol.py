"""RIVER 3 cloud telemetry protocol, adapted from the verified local probe.

Schema: tolwi/hassio-ecoflow-cloud de02d680 (Apache-2.0); see NOTICE.
Only the fixed /get read envelope can be encoded; no device controls.
"""
import base64
import json
import math
from pathlib import Path
import re
import secrets
import sys
import time
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parent / "vendor"))
class ProbeError(ValueError):
    pass

FIELDS = {
    "cms_batt_soc", "bms_batt_soc", "pow_in_sum_w", "pow_out_sum_w",
    "pow_get_ac_in", "pow_get_ac_out", "pow_get_ac", "pow_get_pv",
    "plug_in_info_ac_charger_flag", "plug_in_info_ac_in_flag",
    "plug_in_info_ac_in_vol", "plug_in_info_ac_in_feq", "flow_info_ac_in",
    "flow_info_ac_out", "bms_dsg_rem_time", "cms_dsg_rem_time",
    "bms_chg_dsg_state", "cms_chg_dsg_state", "dev_sleep_state", "ups_alram",
    "bms_heartbeat_soc", "bms_heartbeat_display_soc", "bms_heartbeat_remaining_minutes",
}

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
