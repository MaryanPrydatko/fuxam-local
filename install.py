#!/usr/bin/env python3
"""Install Fuxam Local as a portable Agent Skill with compatibility aliases."""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import shutil
import sys
import tempfile
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent
SOURCE = ROOT / ".agents" / "skills" / "fuxam-local"
CANONICAL_RELATIVE = pathlib.Path(".agents/skills/fuxam-local")
BACKUP_RELATIVE = pathlib.Path(".agents/backups/fuxam-local")
ALIAS_RELATIVES = (
    pathlib.Path(".claude/skills/fuxam-local"),
    pathlib.Path(".codex/skills/fuxam-local"),
)
LEGACY_SKILL_ROOTS = (
    pathlib.Path(".agents/skills"),
    pathlib.Path(".claude/skills"),
    pathlib.Path(".codex/skills"),
)
LEGACY_BACKUP_PREFIX = f"{CANONICAL_RELATIVE.name}.backup-"


class InstallError(RuntimeError):
    pass


def path_exists(path: pathlib.Path) -> bool:
    return path.exists() or path.is_symlink()


def points_to(path: pathlib.Path, target: pathlib.Path) -> bool:
    return path.resolve(strict=False) == target.resolve(strict=False)


def unique_entries(paths: list[pathlib.Path]) -> list[pathlib.Path]:
    # Resolve parent aliases, but preserve distinct leaf symlinks to the same target.
    entries: dict[pathlib.Path, pathlib.Path] = {}
    for path in sorted(paths):
        entries.setdefault(path.parent.resolve(strict=False) / path.name, path)
    return list(entries.values())


def next_backup_path(home: pathlib.Path, path: pathlib.Path) -> pathlib.Path:
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    relative = path.relative_to(home)
    source = "-".join(part.removeprefix(".") for part in relative.parts)
    base = home / BACKUP_RELATIVE / f"{source}.backup-{timestamp}"
    candidate = base
    counter = 1
    while path_exists(candidate):
        candidate = base.with_name(f"{base.name}-{counter}")
        counter += 1
    return candidate


def legacy_backup_paths(home: pathlib.Path) -> list[pathlib.Path]:
    backups: list[pathlib.Path] = []
    for relative in LEGACY_SKILL_ROOTS:
        root = home / relative
        if not root.is_dir():
            continue
        backups.extend(
            sorted(
                path
                for path in root.iterdir()
                if path.name.startswith(LEGACY_BACKUP_PREFIX) and path_exists(path)
            )
        )
    return unique_entries(backups)


def next_legacy_backup_path(home: pathlib.Path, path: pathlib.Path) -> pathlib.Path:
    relative = path.relative_to(home)
    source = "-".join(part.removeprefix(".") for part in relative.parts)
    base = home / BACKUP_RELATIVE / source
    candidate = base
    counter = 1
    while path_exists(candidate):
        candidate = base.with_name(f"{base.name}-{counter}")
        counter += 1
    return candidate


def install(
    home: pathlib.Path,
    *,
    replace: bool = False,
    aliases: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    source = SOURCE.resolve()
    if not (source / "SKILL.md").is_file():
        raise InstallError("The canonical skill source is missing.")

    home = home.expanduser().resolve(strict=False)
    canonical = home / CANONICAL_RELATIVE
    alias_paths = [home / relative for relative in ALIAS_RELATIVES] if aliases else []
    canonical_is_source = path_exists(canonical) and canonical.resolve() == source
    replacing_canonical_link = canonical.is_symlink() and not canonical_is_source

    conflicts: list[pathlib.Path] = []
    if path_exists(canonical) and not canonical_is_source:
        conflicts.append(canonical)
    conflicts.extend(
        alias
        for alias in alias_paths
        if path_exists(alias)
        and (replacing_canonical_link or not points_to(alias, canonical))
    )
    conflicts = unique_entries(conflicts)
    legacy_backups = legacy_backup_paths(home)
    paths_to_preserve = [*conflicts, *legacy_backups]
    if paths_to_preserve and not replace:
        paths = ", ".join(str(path) for path in paths_to_preserve)
        raise InstallError(
            f"Refused to overwrite existing paths: {paths}. "
            "Re-run with --replace to preserve them as timestamped backups."
        )

    report: dict[str, Any] = {
        "ok": True,
        "dryRun": dry_run,
        "canonical": str(canonical),
        "aliases": [str(path) for path in alias_paths],
        "replaced": [str(path) for path in conflicts],
        "legacyBackups": [str(path) for path in legacy_backups],
        "migratedLegacyBackups": [],
        "backups": [],
    }
    if dry_run:
        return report

    staged_root: pathlib.Path | None = None
    staged_skill: pathlib.Path | None = None
    backups: list[tuple[pathlib.Path, pathlib.Path]] = []
    created_aliases: list[pathlib.Path] = []
    installed_canonical = False
    try:
        if not canonical_is_source:
            canonical.parent.mkdir(parents=True, exist_ok=True)
            staged_root = pathlib.Path(
                tempfile.mkdtemp(prefix=".fuxam-local-install-", dir=canonical.parent)
            )
            staged_skill = staged_root / "fuxam-local"
            shutil.copytree(
                source,
                staged_skill,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
            )

        for conflict in conflicts:
            backup = next_backup_path(home, conflict)
            backup.parent.mkdir(parents=True, exist_ok=True)
            conflict.rename(backup)
            backups.append((conflict, backup))

        migrated_legacy_backups: list[pathlib.Path] = []
        for legacy_backup in legacy_backups:
            backup = next_legacy_backup_path(home, legacy_backup)
            backup.parent.mkdir(parents=True, exist_ok=True)
            legacy_backup.rename(backup)
            backups.append((legacy_backup, backup))
            migrated_legacy_backups.append(backup)

        if staged_skill is not None:
            staged_skill.rename(canonical)
            installed_canonical = True
            staged_root.rmdir()
            staged_root = None

        for alias in alias_paths:
            if points_to(alias, canonical):
                continue
            alias.parent.mkdir(parents=True, exist_ok=True)
            alias.symlink_to(canonical, target_is_directory=True)
            created_aliases.append(alias)
    except Exception as exc:
        for alias in reversed(created_aliases):
            if alias.is_symlink():
                alias.unlink()
        if installed_canonical and canonical.is_dir():
            shutil.rmtree(canonical)
        for original, backup in reversed(backups):
            if path_exists(backup) and not path_exists(original):
                backup.rename(original)
        if staged_root is not None and staged_root.exists():
            shutil.rmtree(staged_root)
        raise InstallError(
            "Installation failed; previous paths were restored."
        ) from exc

    report["backups"] = [str(backup) for _, backup in backups]
    report["migratedLegacyBackups"] = [
        str(backup) for backup in migrated_legacy_backups
    ]
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--home",
        type=pathlib.Path,
        default=pathlib.Path.home(),
        help="home directory to install into (defaults to the current user's home)",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="back up and replace conflicting skill installations",
    )
    parser.add_argument(
        "--no-aliases",
        action="store_true",
        help="install only to ~/.agents/skills without Claude/Codex aliases",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="show the install plan without writing"
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = install(
            args.home,
            replace=args.replace,
            aliases=not args.no_aliases,
            dry_run=args.dry_run,
        )
    except InstallError as exc:
        json.dump({"ok": False, "error": str(exc)}, sys.stderr)
        sys.stderr.write("\n")
        return 1
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
