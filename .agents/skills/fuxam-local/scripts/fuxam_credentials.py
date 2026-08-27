"""OS-backed credential storage; no plaintext fallback."""

from __future__ import annotations

import ctypes
import shutil
import subprocess
import sys

from fuxam_errors import FuxamError

SERVICE = "codex-fuxam-local"
KEYCHAIN_ACCOUNT = b"__client"
NOT_FOUND = -25300
WINDOWS_MAX_BYTES = 2560
# secret-tool's stdin buffer is 8192 bytes and may truncate at that boundary.
LINUX_MAX_BYTES = 8191


def _decode_secret(value: bytes, limit: int) -> str:
    if (
        not value
        or len(value) > limit
        or any(byte < 0x21 or byte > 0x7E or byte == 0x3B for byte in value)
    ):
        raise FuxamError("Stored credential is invalid. Run auth set locally.")
    return value.decode("ascii")


class MacOSKeychain:
    storage = "macOS Keychain"

    def __init__(self, service: str = SERVICE) -> None:
        self.service = service.encode("utf-8")
        if sys.platform != "darwin":
            raise FuxamError("This skill currently requires macOS Keychain.")
        self.lib = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        self.core = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        self.lib.SecKeychainFindGenericPassword.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.lib.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        self.lib.SecKeychainItemFreeContent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self.lib.SecKeychainItemFreeContent.restype = ctypes.c_int32
        self.lib.SecKeychainAddGenericPassword.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.lib.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        self.lib.SecKeychainItemModifyAttributesAndData.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        self.lib.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
        self.lib.SecKeychainItemDelete.argtypes = [ctypes.c_void_p]
        self.lib.SecKeychainItemDelete.restype = ctypes.c_int32
        self.core.CFRelease.argtypes = [ctypes.c_void_p]
        self.core.CFRelease.restype = None

    def _find(self) -> tuple[int, bytes | None, ctypes.c_void_p]:
        length = ctypes.c_uint32()
        data = ctypes.c_void_p()
        item = ctypes.c_void_p()
        status = self.lib.SecKeychainFindGenericPassword(
            None,
            len(self.service),
            self.service,
            len(KEYCHAIN_ACCOUNT),
            KEYCHAIN_ACCOUNT,
            ctypes.byref(length),
            ctypes.byref(data),
            ctypes.byref(item),
        )
        if status == NOT_FOUND:
            return status, None, item
        if status != 0:
            raise FuxamError(f"Keychain lookup failed with status {status}.")
        try:
            value = ctypes.string_at(data, length.value)
        finally:
            self.lib.SecKeychainItemFreeContent(None, data)
        return status, value, item

    def get(self) -> str | None:
        _, value, item = self._find()
        try:
            return _decode_secret(value, 16 * 1024) if value is not None else None
        finally:
            if item.value:
                self.core.CFRelease(item)

    def set(self, value: str) -> None:
        encoded = value.encode("utf-8")
        status, _, item = self._find()
        try:
            if status == NOT_FOUND:
                result = self.lib.SecKeychainAddGenericPassword(
                    None,
                    len(self.service),
                    self.service,
                    len(KEYCHAIN_ACCOUNT),
                    KEYCHAIN_ACCOUNT,
                    len(encoded),
                    encoded,
                    None,
                )
            else:
                result = self.lib.SecKeychainItemModifyAttributesAndData(
                    item, None, len(encoded), encoded
                )
            if result != 0:
                raise FuxamError(f"Keychain update failed with status {result}.")
        finally:
            if item.value:
                self.core.CFRelease(item)

    def clear(self) -> bool:
        status, _, item = self._find()
        if status == NOT_FOUND:
            return False
        try:
            result = self.lib.SecKeychainItemDelete(item)
            if result != 0:
                raise FuxamError(f"Keychain deletion failed with status {result}.")
            return True
        finally:
            if item.value:
                self.core.CFRelease(item)


class SecretService:
    storage = "Linux Secret Service"

    def __init__(self, service: str = SERVICE) -> None:
        command = shutil.which("secret-tool")
        if not command:
            raise FuxamError(
                "Saved login on Linux requires secret-tool (libsecret) and an unlocked "
                "desktop keyring. Use --auth env for explicit temporary login."
            )
        self.command = command
        self.service = service

    def _run(self, operation: str, *, secret: bytes = b"") -> bytes:
        options = ["--label=Fuxam Local"] if operation == "store" else []
        command = [
            self.command,
            operation,
            *options,
            "service",
            self.service,
            "account",
            "__client",
        ]
        try:
            result = subprocess.run(  # noqa: S603 - fixed OS tool; secret only on stdin.
                command,
                input=secret,
                capture_output=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FuxamError(
                "Desktop keyring unavailable or timed out. Unlock it and try again."
            ) from exc
        # A silent failure can mean a dismissed unlock prompt, not an absent item.
        if result.returncode != 0 or result.stderr:
            if operation == "lookup":
                raise FuxamError(
                    "No accessible credential. Unlock the desktop keyring or run auth set."
                )
            if operation == "clear":
                raise FuxamError(
                    "Credential removal could not be confirmed. Unlock the desktop "
                    "keyring and inspect its Fuxam Local entry."
                )
            raise FuxamError("Could not store credential. Unlock the desktop keyring.")
        return result.stdout

    def get(self) -> str | None:
        return _decode_secret(self._run("lookup"), LINUX_MAX_BYTES)

    def set(self, value: str) -> None:
        encoded = value.encode("ascii")
        if len(encoded) > LINUX_MAX_BYTES:
            raise FuxamError(
                f"Credential exceeds the Linux secret-tool limit of {LINUX_MAX_BYTES} bytes."
            )
        self._run("store", secret=encoded)

    def clear(self) -> bool:
        self._run("clear")
        return True


class WindowsCredential(ctypes.Structure):
    # CREDENTIALW: DWORD is 32-bit even on 64-bit Windows; Attributes is unused.
    _fields_ = (
        ("Flags", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", ctypes.c_uint32 * 2),
        ("CredentialBlobSize", ctypes.c_uint32),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", ctypes.c_uint32),
        ("AttributeCount", ctypes.c_uint32),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    )


class WindowsCredentialManager:
    storage = "Windows Credential Manager"

    def __init__(self, service: str = SERVICE) -> None:
        self.target = f"{service}/__client"
        self.lib = ctypes.WinDLL("advapi32.dll", use_last_error=True)
        self.lib.CredReadW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.POINTER(WindowsCredential)),
        ]
        self.lib.CredReadW.restype = ctypes.c_int32
        self.lib.CredWriteW.argtypes = [
            ctypes.POINTER(WindowsCredential),
            ctypes.c_uint32,
        ]
        self.lib.CredWriteW.restype = ctypes.c_int32
        self.lib.CredDeleteW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        self.lib.CredDeleteW.restype = ctypes.c_int32
        self.lib.CredFree.argtypes = [ctypes.c_void_p]
        self.lib.CredFree.restype = None

    def get(self) -> str | None:
        result = ctypes.POINTER(WindowsCredential)()
        if not self.lib.CredReadW(self.target, 1, 0, ctypes.byref(result)):
            error = ctypes.get_last_error()
            if error == 1168:  # ERROR_NOT_FOUND
                return None
            raise FuxamError(
                f"Credential Manager lookup failed (Windows error {error})."
            )
        if not result:
            raise FuxamError("Credential Manager returned an invalid record.")
        try:
            value = result.contents
            if (
                not value.CredentialBlob
                or not 0 < value.CredentialBlobSize <= WINDOWS_MAX_BYTES
            ):
                raise FuxamError("Credential Manager returned an invalid record.")
            return _decode_secret(
                ctypes.string_at(value.CredentialBlob, value.CredentialBlobSize),
                WINDOWS_MAX_BYTES,
            )
        finally:
            self.lib.CredFree(result)

    def set(self, value: str) -> None:
        encoded = value.encode("ascii")
        if len(encoded) > WINDOWS_MAX_BYTES:
            raise FuxamError(
                f"Credential exceeds the Windows Credential Manager limit of {WINDOWS_MAX_BYTES} bytes."
            )
        buffer = ctypes.create_string_buffer(encoded)
        record = WindowsCredential()
        record.Type = 1  # CRED_TYPE_GENERIC: application-defined bytes, not UTF-16.
        record.TargetName = self.target
        record.UserName = "__client"
        record.CredentialBlobSize = len(encoded)
        record.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        record.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE: this user/device; no roaming.
        if not self.lib.CredWriteW(ctypes.byref(record), 0):
            error = ctypes.get_last_error()
            raise FuxamError(
                f"Credential Manager update failed (Windows error {error})."
            )

    def clear(self) -> bool:
        if self.lib.CredDeleteW(self.target, 1, 0):
            return True
        error = ctypes.get_last_error()
        if error == 1168:
            return False
        raise FuxamError(f"Credential Manager removal failed (Windows error {error}).")


def credential_store() -> MacOSKeychain | SecretService | WindowsCredentialManager:
    try:
        if sys.platform == "darwin":
            return MacOSKeychain()
        if sys.platform == "linux":
            return SecretService()
        if sys.platform == "win32":
            return WindowsCredentialManager()
    except OSError as exc:
        raise FuxamError(
            "Could not open the operating system's credential store."
        ) from exc
    raise FuxamError("Supported platforms are macOS, Linux, and Windows.")
