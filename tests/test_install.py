from __future__ import annotations

import importlib.util
import pathlib
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = ROOT / "install.py"
SPEC = importlib.util.spec_from_file_location("fuxam_local_install", INSTALL_SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Could not load the installer for tests.")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def test_install_copies_canonical_skill_and_creates_compatibility_aliases(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            result = installer.install(home)
            canonical = home / ".agents/skills/fuxam-local"
            aliases = (
                home / ".claude/skills/fuxam-local",
                home / ".codex/skills/fuxam-local",
            )

            self.assertTrue(result["ok"])
            self.assertTrue((canonical / "SKILL.md").is_file())
            self.assertTrue((canonical / "scripts/fuxam.py").is_file())
            for alias in aliases:
                with self.subTest(alias=alias):
                    self.assertTrue(alias.is_symlink())
                    self.assertEqual(alias.resolve(), canonical.resolve())

    def test_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            result = installer.install(home, dry_run=True)

            self.assertTrue(result["dryRun"])
            self.assertEqual(list(home.iterdir()), [])

    def test_existing_target_is_refused_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            canonical = home / ".agents/skills/fuxam-local"
            canonical.mkdir(parents=True)
            marker = canonical / "owned-by-user"
            marker.write_text("preserve me")

            with self.assertRaisesRegex(installer.InstallError, "Refused"):
                installer.install(home)

            self.assertEqual(marker.read_text(), "preserve me")
            self.assertFalse((home / ".claude").exists())

    def test_replace_preserves_conflict_as_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            canonical = home / ".agents/skills/fuxam-local"
            canonical.mkdir(parents=True)
            (canonical / "owned-by-user").write_text("preserve me")

            result = installer.install(home, replace=True)

            self.assertEqual(len(result["backups"]), 1)
            backup = pathlib.Path(result["backups"][0])
            self.assertEqual((backup / "owned-by-user").read_text(), "preserve me")
            self.assertTrue((canonical / "SKILL.md").is_file())
            self.assertTrue((home / ".claude/skills/fuxam-local").is_symlink())

    def test_failed_install_restores_every_conflicting_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            canonical = home / ".agents/skills/fuxam-local"
            claude = home / ".claude/skills/fuxam-local"
            canonical.mkdir(parents=True)
            claude.mkdir(parents=True)
            (canonical / "canonical-marker").write_text("canonical")
            (claude / "claude-marker").write_text("claude")

            with (
                mock.patch.object(
                    pathlib.Path,
                    "symlink_to",
                    side_effect=OSError("synthetic alias failure"),
                ),
                self.assertRaisesRegex(installer.InstallError, "restored"),
            ):
                installer.install(home, replace=True)

            self.assertEqual((canonical / "canonical-marker").read_text(), "canonical")
            self.assertEqual((claude / "claude-marker").read_text(), "claude")
            self.assertFalse((home / ".codex/skills/fuxam-local").exists())


if __name__ == "__main__":
    unittest.main()
