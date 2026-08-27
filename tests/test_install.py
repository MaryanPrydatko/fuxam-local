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

    def test_install_supports_skill_roots_symlinked_to_canonical_parent(self) -> None:
        for replace in (False, True):
            with (
                self.subTest(replace=replace),
                tempfile.TemporaryDirectory() as directory,
            ):
                home = pathlib.Path(directory).resolve()
                canonical = home / ".agents/skills/fuxam-local"
                canonical.parent.mkdir(parents=True)
                alias_roots = (home / ".claude/skills", home / ".codex/skills")
                for root in alias_roots:
                    root.parent.mkdir()
                    root.symlink_to(canonical.parent, target_is_directory=True)
                if replace:
                    canonical.mkdir()
                    (canonical / "owned-by-user").write_text("preserve me")

                result = installer.install(home, replace=replace)

                self.assertTrue((canonical / "SKILL.md").is_file())
                self.assertEqual(
                    result["replaced"], [str(canonical)] if replace else []
                )
                self.assertEqual(len(result["backups"]), int(replace))
                if replace:
                    backup = pathlib.Path(result["backups"][0])
                    self.assertEqual(
                        (backup / "owned-by-user").read_text(), "preserve me"
                    )
                for root in alias_roots:
                    self.assertTrue(root.is_symlink())
                    self.assertTrue((root / "fuxam-local/SKILL.md").is_file())
                    self.assertFalse((root / "fuxam-local").is_symlink())

    def test_dry_run_with_parent_aliases_preserves_every_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory).resolve()
            canonical = home / ".agents/skills/fuxam-local"
            canonical.mkdir(parents=True)
            (canonical / "owned-by-user").write_text("canonical")
            legacy = canonical.with_name("fuxam-local.backup-20260824T120000Z")
            legacy.mkdir()
            (legacy / "owned-by-user").write_text("legacy")
            for relative in (".claude/skills", ".codex/skills"):
                root = home / relative
                root.parent.mkdir()
                root.symlink_to(canonical.parent, target_is_directory=True)
            before = sorted(home.rglob("*"))

            result = installer.install(home, replace=True, dry_run=True)

            self.assertEqual(sorted(home.rglob("*")), before)
            self.assertEqual(result["replaced"], [str(canonical)])
            self.assertEqual(result["legacyBackups"], [str(legacy)])
            self.assertEqual(result["backups"], [])
            self.assertEqual((canonical / "owned-by-user").read_text(), "canonical")
            self.assertEqual((legacy / "owned-by-user").read_text(), "legacy")
            self.assertFalse((home / ".agents/backups").exists())

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

    def test_replace_preserves_a_shared_alias_root_conflict_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory).resolve()
            canonical = home / ".agents/skills/fuxam-local"
            shared = home / "shared-skills/fuxam-local"
            for path, content in ((canonical, "canonical"), (shared, "shared")):
                path.mkdir(parents=True)
                (path / "owned-by-user").write_text(content)
            aliases = (
                home / ".claude/skills/fuxam-local",
                home / ".codex/skills/fuxam-local",
            )
            for alias in aliases:
                alias.parent.parent.mkdir()
                alias.parent.symlink_to(shared.parent, target_is_directory=True)

            result = installer.install(home, replace=True)

            self.assertEqual(result["replaced"], [str(canonical), str(aliases[0])])
            self.assertEqual(len(result["backups"]), 2)
            self.assertEqual(
                [
                    (pathlib.Path(path) / "owned-by-user").read_text()
                    for path in result["backups"]
                ],
                ["canonical", "shared"],
            )
            for alias in aliases:
                self.assertTrue(alias.parent.is_symlink())
                self.assertEqual(alias.resolve(), canonical)

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

    def test_replace_preserves_distinct_leaf_symlinks_sharing_a_target(self) -> None:
        for linked_canonical in (False, True):
            with (
                self.subTest(linked_canonical=linked_canonical),
                tempfile.TemporaryDirectory() as directory,
            ):
                home = pathlib.Path(directory).resolve()
                outside = home / "outside-skill"
                outside.mkdir()
                (outside / "owned-by-user").write_text("outside")
                canonical = home / ".agents/skills/fuxam-local"
                canonical.parent.mkdir(parents=True)
                if linked_canonical:
                    canonical.symlink_to(outside, target_is_directory=True)
                else:
                    canonical.mkdir()
                    (canonical / "owned-by-user").write_text("canonical")
                aliases = (
                    home / ".claude/skills/fuxam-local",
                    home / ".codex/skills/fuxam-local",
                )
                for alias in aliases:
                    alias.parent.mkdir(parents=True)
                    alias.symlink_to(outside, target_is_directory=True)

                result = installer.install(home, replace=True)

                self.assertEqual(
                    result["replaced"],
                    [str(canonical), *(str(alias) for alias in aliases)],
                )
                self.assertEqual(len(result["backups"]), 3)
                backups = [pathlib.Path(path) for path in result["backups"]]
                for backup in backups[int(not linked_canonical) :]:
                    self.assertTrue(backup.is_symlink())
                    self.assertEqual(backup.readlink(), outside)
                self.assertEqual((outside / "owned-by-user").read_text(), "outside")
                self.assertTrue((canonical / "SKILL.md").is_file())
                for alias in aliases:
                    self.assertEqual(alias.resolve(), canonical)

    def test_replace_keeps_backups_outside_discoverable_skill_directories(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            installer.install(home)

            result = installer.install(home, replace=True)

            canonical = home / ".agents/skills/fuxam-local/SKILL.md"
            discovered = sorted((home / ".agents/skills").glob("*/SKILL.md"))
            self.assertEqual(discovered, [canonical])
            self.assertEqual(len(result["backups"]), 1)
            backup = pathlib.Path(result["backups"][0])
            self.assertTrue((backup / "SKILL.md").is_file())
            self.assertEqual(
                backup.parent.resolve(),
                (home / ".agents/backups/fuxam-local").resolve(),
            )

    def test_replace_migrates_legacy_backups_out_of_every_skill_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            installer.install(home)
            legacy_backups = (
                home / ".agents/skills/fuxam-local.backup-20260824T120000Z",
                home / ".claude/skills/fuxam-local.backup-20260824T120001Z",
                home / ".codex/skills/fuxam-local.backup-20260824T120002Z",
            )
            for index, backup in enumerate(legacy_backups):
                backup.mkdir(parents=True)
                (backup / "SKILL.md").write_text(f"legacy {index}")

            result = installer.install(home, replace=True)

            for backup in legacy_backups:
                with self.subTest(backup=backup):
                    self.assertFalse(installer.path_exists(backup))
            migrated = [pathlib.Path(path) for path in result["migratedLegacyBackups"]]
            self.assertEqual(len(migrated), 3)
            for index, backup in enumerate(migrated):
                with self.subTest(backup=backup):
                    self.assertEqual(
                        backup.parent.resolve(),
                        (home / ".agents/backups/fuxam-local").resolve(),
                    )
                    self.assertEqual(
                        (backup / "SKILL.md").read_text(), f"legacy {index}"
                    )

            for relative in (
                ".agents/skills",
                ".claude/skills",
                ".codex/skills",
            ):
                discovered = list(
                    (home / relative).glob("fuxam-local.backup-*/SKILL.md")
                )
                self.assertEqual(discovered, [])

    def test_replace_migrates_shared_legacy_backups_once(self) -> None:
        for target_root, reported_root in (
            (".agents/skills", ".agents/skills"),
            ("shared-skills", ".claude/skills"),
        ):
            with (
                self.subTest(target_root=target_root),
                tempfile.TemporaryDirectory() as directory,
            ):
                home = pathlib.Path(directory).resolve()
                shared = home / target_root
                shared.mkdir(parents=True)
                legacy_name = "fuxam-local.backup-20260824T120000Z"
                legacy = shared / legacy_name
                legacy.mkdir()
                (legacy / "owned-by-user").write_text("legacy")
                for relative in (".claude/skills", ".codex/skills"):
                    root = home / relative
                    root.parent.mkdir()
                    root.symlink_to(shared, target_is_directory=True)

                result = installer.install(home, replace=True)

                self.assertEqual(
                    result["legacyBackups"], [str(home / reported_root / legacy_name)]
                )
                self.assertEqual(len(result["migratedLegacyBackups"]), 1)
                backup = pathlib.Path(result["migratedLegacyBackups"][0])
                self.assertEqual((backup / "owned-by-user").read_text(), "legacy")
                self.assertFalse(legacy.exists())
                self.assertTrue(
                    (home / ".agents/skills/fuxam-local/SKILL.md").is_file()
                )

    def test_replace_preserves_distinct_legacy_symlinks_sharing_a_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory).resolve()
            outside = home / "outside-skill"
            outside.mkdir()
            (outside / "owned-by-user").write_text("outside")
            legacy_paths = []
            for relative in (".agents/skills", ".claude/skills", ".codex/skills"):
                legacy = home / relative / "fuxam-local.backup-20260824T120000Z"
                legacy.parent.mkdir(parents=True)
                legacy.symlink_to(outside, target_is_directory=True)
                legacy_paths.append(legacy)

            result = installer.install(home, replace=True)

            self.assertEqual(
                result["legacyBackups"], [str(path) for path in legacy_paths]
            )
            self.assertEqual(len(result["migratedLegacyBackups"]), 3)
            for path in result["migratedLegacyBackups"]:
                backup = pathlib.Path(path)
                self.assertTrue(backup.is_symlink())
                self.assertEqual(backup.readlink(), outside)
            for path in legacy_paths:
                self.assertFalse(path.is_symlink())
            self.assertEqual((outside / "owned-by-user").read_text(), "outside")

    def test_failed_install_restores_every_conflicting_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            canonical = home / ".agents/skills/fuxam-local"
            claude = home / ".claude/skills/fuxam-local"
            canonical.mkdir(parents=True)
            claude.mkdir(parents=True)
            (canonical / "canonical-marker").write_text("canonical")
            (claude / "claude-marker").write_text("claude")
            legacy = home / ".agents/skills/fuxam-local.backup-20260824T120000Z"
            legacy.mkdir()
            (legacy / "legacy-marker").write_text("legacy")

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
            self.assertEqual((legacy / "legacy-marker").read_text(), "legacy")
            self.assertFalse((home / ".codex/skills/fuxam-local").exists())

    def test_failed_install_restores_shared_parent_and_normal_root_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory).resolve()
            canonical = home / ".agents/skills/fuxam-local"
            codex = home / ".codex/skills/fuxam-local"
            for path, content in ((canonical, "canonical"), (codex, "codex")):
                path.mkdir(parents=True)
                (path / "owned-by-user").write_text(content)
            claude_root = home / ".claude/skills"
            claude_root.parent.mkdir()
            claude_root.symlink_to(canonical.parent, target_is_directory=True)
            legacy = canonical.with_name("fuxam-local.backup-20260824T120000Z")
            legacy.mkdir()
            (legacy / "owned-by-user").write_text("legacy")

            with (
                mock.patch.object(
                    pathlib.Path,
                    "symlink_to",
                    side_effect=OSError("synthetic alias failure"),
                ),
                self.assertRaisesRegex(installer.InstallError, "restored"),
            ):
                installer.install(home, replace=True)

            self.assertEqual((canonical / "owned-by-user").read_text(), "canonical")
            self.assertEqual((codex / "owned-by-user").read_text(), "codex")
            self.assertEqual((legacy / "owned-by-user").read_text(), "legacy")
            self.assertTrue(claude_root.is_symlink())
            self.assertEqual(claude_root.readlink(), canonical.parent)
            self.assertEqual(list((home / ".agents/backups/fuxam-local").iterdir()), [])
            self.assertEqual(list(canonical.parent.glob(".fuxam-local-install-*")), [])


if __name__ == "__main__":
    unittest.main()
