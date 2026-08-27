from __future__ import annotations

import ctypes
import importlib.util
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

SCRIPTS = (
    pathlib.Path(__file__).resolve().parents[1] / ".agents/skills/fuxam-local/scripts"
)
for name in ("fuxam_errors", "fuxam_credentials"):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
credentials = sys.modules["fuxam_credentials"]


class CredentialStoreTests(unittest.TestCase):
    def test_selects_only_the_native_store(self) -> None:
        for platform, backend in (
            ("darwin", "MacOSKeychain"),
            ("linux", "SecretService"),
            ("win32", "WindowsCredentialManager"),
        ):
            with (
                self.subTest(platform=platform),
                mock.patch.object(credentials.sys, "platform", platform),
                mock.patch.object(credentials, backend) as selected,
            ):
                self.assertIs(credentials.credential_store(), selected.return_value)
                selected.assert_called_once_with()

    def test_unsupported_platform_fails_without_fallback(self) -> None:
        with (
            mock.patch.object(credentials.sys, "platform", "freebsd"),
            self.assertRaises(credentials.FuxamError),
        ):
            credentials.credential_store()

    def test_native_loader_errors_are_sanitized(self) -> None:
        with (
            mock.patch.object(credentials.sys, "platform", "win32"),
            mock.patch.object(
                credentials,
                "WindowsCredentialManager",
                side_effect=OSError("private-path"),
            ),
            self.assertRaises(credentials.FuxamError) as raised,
        ):
            credentials.credential_store()
        self.assertNotIn("private", str(raised.exception))


class MacOSKeychainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api, self.core = mock.Mock(), mock.Mock()
        with (
            mock.patch.object(credentials.sys, "platform", "darwin"),
            mock.patch.object(
                credentials.ctypes, "CDLL", side_effect=[self.api, self.core]
            ),
        ):
            self.store = credentials.MacOSKeychain()

    def test_missing_entry_keeps_the_existing_service_and_account(self) -> None:
        self.api.SecKeychainFindGenericPassword.return_value = credentials.NOT_FOUND
        self.assertIsNone(self.store.get())
        args = self.api.SecKeychainFindGenericPassword.call_args.args
        self.assertEqual(args[:5], (None, 17, b"codex-fuxam-local", 8, b"__client"))
        self.api.SecKeychainItemFreeContent.assert_not_called()
        self.core.CFRelease.assert_not_called()

    def test_create_preserves_the_existing_keychain_entry_name(self) -> None:
        self.api.SecKeychainFindGenericPassword.return_value = credentials.NOT_FOUND
        self.api.SecKeychainAddGenericPassword.return_value = 0
        self.store.set("synthetic-cookie")
        self.api.SecKeychainAddGenericPassword.assert_called_once_with(
            None,
            17,
            b"codex-fuxam-local",
            8,
            b"__client",
            16,
            b"synthetic-cookie",
            None,
        )

    def test_read_frees_native_memory_even_when_the_value_is_invalid(self) -> None:
        for raw in (b"synthetic-cookie", b"\xff"):
            buffer = ctypes.create_string_buffer(raw)

            def find(
                _keychain,
                _size,
                _service,
                _account_size,
                _account,
                length,
                data,
                item,
                raw=raw,
                buffer=buffer,
            ):
                ctypes.cast(length, ctypes.POINTER(ctypes.c_uint32))[0] = len(raw)
                ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p))[0] = (
                    ctypes.addressof(buffer)
                )
                ctypes.cast(item, ctypes.POINTER(ctypes.c_void_p))[0] = 123
                return 0

            with self.subTest(raw=raw):
                self.api.SecKeychainFindGenericPassword.side_effect = find
                self.api.SecKeychainItemFreeContent.reset_mock()
                self.core.CFRelease.reset_mock()
                if raw.isascii():
                    self.assertEqual(self.store.get(), raw.decode())
                else:
                    with self.assertRaises(credentials.FuxamError):
                        self.store.get()
                self.api.SecKeychainItemFreeContent.assert_called_once()
                self.core.CFRelease.assert_called_once()

    def test_lookup_denial_is_not_reported_as_absent(self) -> None:
        self.api.SecKeychainFindGenericPassword.return_value = -25293
        with self.assertRaises(credentials.FuxamError):
            self.store.get()


class SecretServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        with mock.patch.object(
            credentials.shutil, "which", return_value="/usr/bin/secret-tool"
        ):
            self.store = credentials.SecretService()

    def test_missing_tool_stops_before_any_subprocess(self) -> None:
        with (
            mock.patch.object(credentials.shutil, "which", return_value=None),
            mock.patch.object(credentials.subprocess, "run") as run,
            self.assertRaisesRegex(credentials.FuxamError, "secret-tool"),
        ):
            credentials.SecretService()
        run.assert_not_called()

    def test_store_sends_exact_secret_on_stdin_only(self) -> None:
        result = subprocess.CompletedProcess([], 0, b"", b"")
        with mock.patch.object(
            credentials.subprocess, "run", return_value=result
        ) as run:
            self.store.set("synthetic-cookie")
        args, kwargs = run.call_args
        self.assertEqual(
            args[0],
            [
                "/usr/bin/secret-tool",
                "store",
                "--label=Fuxam Local",
                "service",
                "codex-fuxam-local",
                "account",
                "__client",
            ],
        )
        self.assertEqual(kwargs["input"], b"synthetic-cookie")
        self.assertEqual(kwargs["timeout"], 30)
        self.assertFalse(kwargs.get("shell", False))
        self.assertNotIn("synthetic-cookie", repr(args))
        self.assertNotIn("env", kwargs)

    def test_lookup_keeps_the_exact_value_private(self) -> None:
        result = subprocess.CompletedProcess([], 0, b"synthetic-cookie", b"")
        with mock.patch.object(credentials.subprocess, "run", return_value=result):
            self.assertEqual(self.store.get(), "synthetic-cookie")

    def test_rejects_input_at_the_secret_tool_truncation_boundary(self) -> None:
        with mock.patch.object(credentials.subprocess, "run") as run:
            with self.assertRaisesRegex(credentials.FuxamError, "8191"):
                self.store.set("a" * 8192)
            run.assert_not_called()

    def test_accepts_largest_safe_value(self) -> None:
        result = subprocess.CompletedProcess([], 0, b"", b"")
        with mock.patch.object(
            credentials.subprocess, "run", return_value=result
        ) as run:
            self.store.set("a" * 8191)
        self.assertEqual(len(run.call_args.kwargs["input"]), 8191)

    def test_silent_lookup_or_clear_failure_is_not_reported_as_absent(self) -> None:
        result = subprocess.CompletedProcess([], 1, b"", b"")
        for operation in (self.store.get, self.store.clear):
            with (
                self.subTest(operation=operation.__name__),
                mock.patch.object(credentials.subprocess, "run", return_value=result),
                self.assertRaises(credentials.FuxamError),
            ):
                operation()

    def test_clear_reports_only_confirmed_removal(self) -> None:
        result = subprocess.CompletedProcess([], 0, b"", b"")
        with mock.patch.object(credentials.subprocess, "run", return_value=result):
            self.assertTrue(self.store.clear())

    def test_errors_and_warnings_never_expose_tool_output(self) -> None:
        for result in (
            subprocess.CompletedProcess([], 1, b"private-cookie", b"private-detail"),
            subprocess.CompletedProcess([], 0, b"private-cookie", b"private-detail"),
        ):
            for operation in (self.store.get, self.store.clear):
                with mock.patch.object(
                    credentials.subprocess, "run", return_value=result
                ):
                    with self.assertRaises(credentials.FuxamError) as raised:
                        operation()
                self.assertNotIn("private", str(raised.exception))

    def test_timeout_and_os_errors_are_sanitized(self) -> None:
        for error in (
            subprocess.TimeoutExpired(["private-path"], 30, output=b"private-cookie"),
            OSError("private-path"),
        ):
            with mock.patch.object(credentials.subprocess, "run", side_effect=error):
                with self.assertRaises(credentials.FuxamError) as raised:
                    self.store.get()
            self.assertNotIn("private", str(raised.exception))

    def test_invalid_stored_values_are_rejected(self) -> None:
        for value in (b"", b"\xff", b"a" * 8192):
            result = subprocess.CompletedProcess([], 0, value, b"")
            with mock.patch.object(credentials.subprocess, "run", return_value=result):
                with self.assertRaises(credentials.FuxamError):
                    self.store.get()


class WindowsCredentialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = mock.Mock()
        with mock.patch.object(
            credentials.ctypes, "WinDLL", return_value=self.api, create=True
        ) as loader:
            self.store = credentials.WindowsCredentialManager()
        loader.assert_called_once_with("advapi32.dll", use_last_error=True)

    def test_credential_layout_uses_fixed_width_windows_types(self) -> None:
        expected = 80 if ctypes.sizeof(ctypes.c_void_p) == 8 else 52
        self.assertEqual(ctypes.sizeof(credentials.WindowsCredential), expected)

    def test_write_uses_exact_bytes_and_non_roaming_persistence(self) -> None:
        def write(pointer, flags):
            value = ctypes.cast(
                pointer, ctypes.POINTER(credentials.WindowsCredential)
            ).contents
            self.assertEqual(value.TargetName, "codex-fuxam-local/__client")
            self.assertEqual(value.UserName, "__client")
            self.assertEqual(value.Type, 1)
            self.assertEqual(value.Persist, 2)
            self.assertEqual(value.Flags, 0)
            self.assertEqual(flags, 0)
            self.assertEqual(
                ctypes.string_at(value.CredentialBlob, value.CredentialBlobSize),
                b"synthetic-cookie",
            )
            return 1

        self.api.CredWriteW.side_effect = write
        self.store.set("synthetic-cookie")
        self.api.CredWriteW.assert_called_once()

    def test_size_limit_is_enforced_before_write(self) -> None:
        with self.assertRaisesRegex(credentials.FuxamError, "2560"):
            self.store.set("a" * 2561)
        self.api.CredWriteW.assert_not_called()
        self.api.CredWriteW.return_value = 1
        self.store.set("a" * 2560)
        self.api.CredWriteW.assert_called_once()

    def test_reads_bytes_and_frees_allocation_even_when_invalid(self) -> None:
        for data, size, valid in (
            (b"synthetic-cookie", 16, True),
            (b"\xff", 1, False),
            (b"", 0, False),
            (b"a", 2561, False),
        ):
            buffer = ctypes.create_string_buffer(data)
            value = credentials.WindowsCredential()
            value.CredentialBlobSize = size
            value.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))

            def read(target, kind, flags, output, value=value):
                self.assertEqual(
                    (target, kind, flags), ("codex-fuxam-local/__client", 1, 0)
                )
                ctypes.cast(
                    output,
                    ctypes.POINTER(ctypes.POINTER(credentials.WindowsCredential)),
                )[0] = ctypes.pointer(value)
                return 1

            with self.subTest(data=data, size=size):
                self.api.CredReadW.side_effect = read
                self.api.CredFree.reset_mock()
                if valid:
                    self.assertEqual(self.store.get(), data.decode())
                else:
                    with self.assertRaises(credentials.FuxamError):
                        self.store.get()
                self.api.CredFree.assert_called_once()

    def test_only_not_found_is_treated_as_absent(self) -> None:
        self.api.CredReadW.return_value = 0
        self.api.CredDeleteW.return_value = 0
        with mock.patch.object(
            credentials.ctypes, "get_last_error", return_value=1168, create=True
        ):
            self.assertIsNone(self.store.get())
            self.assertFalse(self.store.clear())
        for error in (5, 1312):
            with mock.patch.object(
                credentials.ctypes, "get_last_error", return_value=error, create=True
            ):
                with self.assertRaises(credentials.FuxamError):
                    self.store.get()
                with self.assertRaises(credentials.FuxamError):
                    self.store.clear()
        self.api.CredFree.assert_not_called()

    def test_successful_clear_uses_only_our_exact_target(self) -> None:
        self.api.CredDeleteW.return_value = 1
        self.assertTrue(self.store.clear())
        self.api.CredDeleteW.assert_called_once_with("codex-fuxam-local/__client", 1, 0)

    def test_failed_write_reports_only_an_error_code(self) -> None:
        self.api.CredWriteW.return_value = 0
        with (
            mock.patch.object(
                credentials.ctypes, "get_last_error", return_value=5, create=True
            ),
            self.assertRaises(credentials.FuxamError) as raised,
        ):
            self.store.set("synthetic-cookie")
        self.assertIn("5", str(raised.exception))
        self.assertNotIn("synthetic-cookie", str(raised.exception))

    def test_null_blob_is_rejected_and_freed(self) -> None:
        value = credentials.WindowsCredential()
        value.CredentialBlobSize = 1

        def read(_target, _kind, _flags, output):
            ctypes.cast(
                output, ctypes.POINTER(ctypes.POINTER(credentials.WindowsCredential))
            )[0] = ctypes.pointer(value)
            return 1

        self.api.CredReadW.side_effect = read
        with self.assertRaises(credentials.FuxamError):
            self.store.get()
        self.api.CredFree.assert_called_once()


if __name__ == "__main__":
    unittest.main()
