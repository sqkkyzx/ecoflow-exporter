"""Store only the EcoFlow App password in the current user's macOS Keychain.

The service name is fixed; the account is the same normalized phone number used
by the caller's local profile. Importing this module never opens the Keychain.
No subprocess, command-line argument, environment variable, or plaintext file
is used to transport or persist the password.
"""
from __future__ import annotations

import ctypes
from functools import lru_cache
import sys

SERVICE = "ecoflow-wifi-cloud-probe"
_NOT_FOUND = -25300
_DUPLICATE_ITEM = -25299
_MAX_PASSWORD_BYTES = 1_048_576
_STATUS_MESSAGES = {
    -128: "使用者取消了 macOS 鑰匙圈授權。",
    -50: "macOS 鑰匙圈拒絕了請求參數。",
    -25291: "macOS 鑰匙圈目前不可用。",
    -25293: "macOS 鑰匙圈未允許存取。",
    -25299: "macOS 鑰匙圈項目已存在，請稍後再試。",
    -25300: "找不到對應的 macOS 鑰匙圈項目。",
    -25308: "macOS 鑰匙圈需要解鎖或系統授權，目前無法互動。",
}


class CredentialStoreError(RuntimeError):
    """A safe, user-facing failure; messages never include account or password."""

    def __init__(self, message: str, *, operation: str = "", status: int | None = None):
        super().__init__(message)
        self.operation = operation
        self.status = status


def _check_status(status: int, operation: str) -> None:
    if status == 0:
        return
    detail = _STATUS_MESSAGES.get(status, "macOS 鑰匙圈操作失敗。")
    raise CredentialStoreError(
        f"{detail}（操作：{operation}，OSStatus {status}）",
        operation=operation,
        status=status,
    ) from None


@lru_cache(maxsize=1)
def _load_api():
    """Load and type the native ABI lazily; tests replace this without macOS I/O."""
    if sys.platform != "darwin":
        raise CredentialStoreError("保存密碼需要 macOS 鑰匙圈；目前平台不支援。")
    try:
        security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        core = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        status_type = ctypes.c_int32
        length_type = ctypes.c_uint32
        pointer = ctypes.c_void_p

        security.SecKeychainFindGenericPassword.argtypes = [
            pointer, length_type, ctypes.c_char_p, length_type, ctypes.c_char_p,
            ctypes.POINTER(length_type), ctypes.POINTER(pointer), ctypes.POINTER(pointer),
        ]
        security.SecKeychainFindGenericPassword.restype = status_type
        security.SecKeychainAddGenericPassword.argtypes = [
            pointer, length_type, ctypes.c_char_p, length_type, ctypes.c_char_p,
            length_type, pointer, ctypes.POINTER(pointer),
        ]
        security.SecKeychainAddGenericPassword.restype = status_type
        security.SecKeychainItemModifyAttributesAndData.argtypes = [
            pointer, pointer, length_type, pointer,
        ]
        security.SecKeychainItemModifyAttributesAndData.restype = status_type
        security.SecKeychainItemFreeContent.argtypes = [pointer, pointer]
        security.SecKeychainItemFreeContent.restype = status_type
        core.CFRelease.argtypes = [pointer]
        core.CFRelease.restype = None
    except (OSError, AttributeError):
        raise CredentialStoreError("無法載入 macOS 鑰匙圈介面；密碼尚未保存。") from None
    return security, core


def _identity(account: str) -> tuple[bytes, bytes]:
    if not isinstance(account, str) or not account or "\x00" in account:
        raise CredentialStoreError("鑰匙圈帳號格式不符；請使用設定中的手機號。")
    try:
        account_bytes = account.encode("utf-8")
    except UnicodeError:
        raise CredentialStoreError("鑰匙圈帳號編碼不符。") from None
    if len(account_bytes) > 4096:
        raise CredentialStoreError("鑰匙圈帳號長度超出限制。")
    return SERVICE.encode("utf-8"), account_bytes


def get_password(account: str) -> str | None:
    """Read this service/account's password; return None only when not found.

    macOS may show its own Keychain access dialog. A denial, cancellation, or
    locked Keychain raises CredentialStoreError and is never treated as absence.
    """
    service, account_bytes = _identity(account)
    security, _core = _load_api()
    length = ctypes.c_uint32()
    content = ctypes.c_void_p()
    try:
        status = security.SecKeychainFindGenericPassword(
            None, len(service), service, len(account_bytes), account_bytes,
            ctypes.byref(length), ctypes.byref(content), None,
        )
        if status == _NOT_FOUND:
            return None
        _check_status(status, "讀取密碼")
        if length.value > _MAX_PASSWORD_BYTES or (length.value and not content.value):
            raise CredentialStoreError("鑰匙圈密碼資料格式不符；未輸出內容。")
        try:
            return ctypes.string_at(content.value, length.value).decode("utf-8") if length.value else ""
        except UnicodeError:
            raise CredentialStoreError("鑰匙圈密碼不是有效的 UTF-8 文字；未輸出內容。") from None
    finally:
        if content.value:
            security.SecKeychainItemFreeContent(None, content)


def set_password(account: str, password: str) -> None:
    """Add or update only this service/account's App password.

    The existing password is not read during an update. This function never
    stores API tokens. It returns only after Security.framework reports success;
    on failure the caller must not claim the password was saved.
    """
    service, account_bytes = _identity(account)
    if not isinstance(password, str) or not password:
        raise CredentialStoreError("密碼不可為空；未保存。")
    try:
        password_bytes = password.encode("utf-8")
    except UnicodeError:
        raise CredentialStoreError("密碼不是有效的 UTF-8 文字；未保存。") from None
    if len(password_bytes) > _MAX_PASSWORD_BYTES:
        raise CredentialStoreError("密碼長度超出限制；未保存。")
    security, core = _load_api()
    buffer = ctypes.create_string_buffer(password_bytes)
    item = ctypes.c_void_p()
    try:
        status = security.SecKeychainFindGenericPassword(
            None, len(service), service, len(account_bytes), account_bytes,
            None, None, ctypes.byref(item),
        )
        if status == _NOT_FOUND:
            status = security.SecKeychainAddGenericPassword(
                None, len(service), service, len(account_bytes), account_bytes,
                len(password_bytes), buffer, None,
            )
            if status != _DUPLICATE_ITEM:
                _check_status(status, "保存密碼")
                return
            # Another instance may have created the same item after our lookup.
            status = security.SecKeychainFindGenericPassword(
                None, len(service), service, len(account_bytes), account_bytes,
                None, None, ctypes.byref(item),
            )
        _check_status(status, "尋找密碼項目")
        if not item.value:
            raise CredentialStoreError("鑰匙圈未返回有效項目；未保存密碼。")
        status = security.SecKeychainItemModifyAttributesAndData(
            item, None, len(password_bytes), buffer,
        )
        _check_status(status, "更新密碼")
    finally:
        # Python strings cannot be reliably zeroed; clear our native temporary.
        ctypes.memset(buffer, 0, ctypes.sizeof(buffer))
        if item.value:
            core.CFRelease(item)


__all__ = ["SERVICE", "CredentialStoreError", "get_password", "set_password"]
