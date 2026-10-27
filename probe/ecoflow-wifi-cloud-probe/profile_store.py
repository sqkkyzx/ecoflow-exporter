"""Store only the EcoFlow phone and device serial; passwords belong in Keychain."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile

PROFILE_PATH = Path(__file__).resolve().parents[2] / "config" / "profile.json"


def normalize_phone(phone: str) -> str:
    phone = phone.strip().replace(" ", "").removeprefix("+86")
    if not re.fullmatch(r"[0-9]{11}", phone):
        raise ValueError("手機號格式不符。")
    return phone


def normalize_serial(serial: str) -> str:
    serial = serial.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{8,40}", serial):
        raise ValueError("設備 SN 格式不符。")
    return serial


def load_profile(path: Path = PROFILE_PATH) -> dict | None:
    try:
        data = json.loads(path.read_text())
        if data.get("version") != 1 or data.get("region") != "cn":
            return None
        return {"version": 1, "region": "cn", "phone": normalize_phone(data["phone"]),
                "sn": normalize_serial(data["sn"])}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def save_profile(phone: str, serial: str, path: Path = PROFILE_PATH):
    data = {"version": 1, "region": "cn", "phone": normalize_phone(phone),
            "sn": normalize_serial(serial)}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".profile-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
