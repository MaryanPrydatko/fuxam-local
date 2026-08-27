"""Exercise an OS store on a disposable CI runner, never a student account."""

from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import unittest
import uuid

SCRIPTS = (
    pathlib.Path(__file__).resolve().parents[1] / ".agents/skills/fuxam-local/scripts"
)
for name in ("fuxam_errors", "fuxam_credentials"):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
credentials = sys.modules["fuxam_credentials"]


@unittest.skipUnless(
    os.environ.get("GITHUB_ACTIONS") == "true" and sys.platform in {"linux", "win32"},
    "Native storage tests require a disposable Linux/Windows GitHub runner.",
)
class NativeCredentialTests(unittest.TestCase):
    def test_round_trip_update_and_remove(self) -> None:
        service = f"fuxam-local-test-{uuid.uuid4().hex}"
        backend = (
            credentials.SecretService
            if sys.platform == "linux"
            else credentials.WindowsCredentialManager
        )
        store = backend(service=service)
        limit = (
            credentials.LINUX_MAX_BYTES
            if sys.platform == "linux"
            else credentials.WINDOWS_MAX_BYTES
        )
        try:
            for value in ("synthetic-cookie", "a" * limit):
                store.set(value)
                self.assertEqual(backend(service=service).get(), value)
        finally:
            self.assertTrue(store.clear())
        if sys.platform == "linux":
            with self.assertRaises(credentials.FuxamError):
                store.get()
        else:
            self.assertIsNone(store.get())


if __name__ == "__main__":
    unittest.main()
