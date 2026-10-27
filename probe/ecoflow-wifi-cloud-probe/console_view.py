"""Privacy-preserving Traditional Chinese console text for probe events.

The JSONL recorder remains the detailed record. This view deliberately ignores
unknown keys and free-form service text, and never interprets power as mains state.
"""
from __future__ import annotations

import math


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _display_number(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _age(value) -> str:
    age = _number(value)
    if age is None:
        return "接收時間未知"
    return f"{_display_number(max(0, age))} 秒前收到"


def _cached(data: dict) -> bool:
    return bool(data.get("cached") or data.get("retained"))


def _field(fields: dict, names: tuple[str, ...]) -> dict | None:
    # Delta pushes give each field its own clock. Pick the freshest current SOC,
    # rather than allowing an old CMS value to hide a newer BMS heartbeat.
    choices = [fields[name] for name in names if isinstance(fields.get(name), dict)]
    choices = [entry for entry in choices if _number(entry.get("value")) is not None]
    def rank(entry):
        age = _number(entry.get("age_seconds"))
        return (bool(entry.get("stale")) or _cached(entry), _cached(entry),
                max(0, age) if age is not None else math.inf)
    return min(choices, key=rank) if choices else None


def _field_age(entry: dict, *, connected: bool, cached: bool) -> str:
    parts = [_age(entry.get("age_seconds"))]
    if cached or _cached(entry):
        parts.append("快取，非即時")
    if not connected or entry.get("stale"):
        parts.append("已過期")
    return "；".join(parts)


def _metric(label: str, unit: str, entry: dict | None, *, connected: bool, cached: bool,
            absolute: bool = False) -> str:
    if entry is None:
        return f"{label}：尚未收到"
    value = _number(entry["value"])
    if absolute:
        value = abs(value)
    return (f"{label}：{_display_number(value)}{unit}"
            f"（{_field_age(entry, connected=connected, cached=cached)}）")


def _safe_code(data: dict) -> int | None:
    value = data.get("code")
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 65535:
        return value
    return None


def _code_suffix(data: dict) -> str:
    code = _safe_code(data)
    labels = {128: "連線結束", 133: "用戶端識別碼遭拒", 134: "帳號或密碼遭拒", 135: "未獲授權"}
    if code is None:
        return ""
    label = labels.get(code)
    return f"（代碼 {code}" + (f"：{label}" if label else "") + "）"


def _count(value) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return str(value)
    return "未知"


def format_event(event: str, data: dict) -> str | None:
    """Render one safe console entry, or None for high-volume/internal events.

    Message events (including cached and retained messages) are recorded in JSONL
    but suppressed here. Snapshots are supplied by the caller every ten seconds.
    """
    if not isinstance(data, dict):
        return None
    if event == "mqtt_connect":
        if data.get("success") is True:
            return "已連上 EcoFlow 雲端，正在訂閱設備資料。"
        return "EcoFlow 雲端連線失敗" + _code_suffix(data) + "；已停止重試。"
    if event == "mqtt_subscribe":
        if data.get("success") is True:
            return "設備資料訂閱成功，正在等待指標。"
        return "設備資料訂閱失敗；未能開始採集。"
    if event == "mqtt_disconnect":
        return "EcoFlow 雲端連線已中斷" + _code_suffix(data) + "。"
    if event == "quota_read_requested":
        if data.get("accepted_locally") is False:
            return "本次讀取請求未能排入傳送；繼續觀察連線。"
        reason = {"no_data": "尚未收到有效指標", "stale_data": "重要資料已過期"}.get(data.get("trigger"))
        prefix = reason + "，" if reason else ""
        return prefix + "已嘗試唯讀資料請求；等待回覆，資料正常後停止請求。"
    if event == "read_reply":
        values = data.get("fields", {})
        if not isinstance(values, dict):
            return None
        fields = {name: {"value": value, "age_seconds": 0, "cached": True}
                  for name, value in values.items()}
        kwargs = {"connected": True, "cached": True}
        return "收到讀取回覆（設備採樣時間未確認）：" + "｜".join([
            _metric("電量", "%", _field(fields, ("cms_batt_soc", "bms_batt_soc", "bms_heartbeat_display_soc", "bms_heartbeat_soc")), **kwargs),
            _metric("AC 輸入", " W", _field(fields, ("pow_get_ac_in",)), **kwargs),
            _metric("AC 輸出", " W", _field(fields, ("pow_get_ac_out", "pow_get_ac")), absolute=True, **kwargs),
        ])
    if event == "decode_error":
        return "有一筆訊息未能解析；仍繼續接收。"
    if event == "summary":
        return (f"採集結束：共收到 {_count(data.get('messages'))} 筆訊息，"
                f"其中 {_count(data.get('decoded_live_messages'))} 筆含非快取指標。"
                f"另有 {_count(data.get('decoded_reply_messages', 0))} 筆讀取／快取回覆。"
                "接收時間不代表設備採樣時間。")
    if event != "snapshot":
        return None

    fields = data.get("fields")
    if not isinstance(fields, dict):
        fields = {}
    connected = data.get("broker_connected") is True
    cached = _cached(data)
    status = "雲端已連線" if connected else "雲端未連線"
    if cached:
        status += "；快取資料，非即時"
    age = data.get("last_telemetry_receive_age_seconds")
    header = status + (f"；最近指標於{_age(age)}。" if _number(age) is not None else "；尚未收到非快取指標。")
    kwargs = {"connected": connected, "cached": cached}
    metrics = [
        _metric("電量", "%", _field(fields, ("cms_batt_soc", "bms_batt_soc", "bms_heartbeat_display_soc", "bms_heartbeat_soc")), **kwargs),
        _metric("AC 輸入", " W", _field(fields, ("pow_get_ac_in",)), **kwargs),
        _metric("AC 輸出", " W", _field(fields, ("pow_get_ac_out", "pow_get_ac")), absolute=True, **kwargs),
    ]
    entry = fields.get("plug_in_info_ac_charger_flag")
    raw_flag = entry.get("value") if isinstance(entry, dict) else None
    if isinstance(raw_flag, bool):
        raw_flag = int(raw_flag)
    if _number(raw_flag) in (0, 1):
        text = "1／設備回報已連接" if raw_flag == 1 else "0／設備回報未連接"
        flag = f"AC 連接旗標：{text}（{_field_age(entry, **kwargs)}）。"
        if not connected or entry.get("stale") or cached or _cached(entry):
            flag += "這是最後回報值，目前狀態未確認。"
    else:
        flag = "AC 連接旗標：尚未收到有效值。"
    flag += "尚未實機斷電驗證，不能保證市電正常。"
    return "\n".join([header, "｜".join(metrics), flag])
