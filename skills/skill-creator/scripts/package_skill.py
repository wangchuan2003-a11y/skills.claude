#!/usr/bin/env python3
"""
Skill Packager - Creates a distributable .skill file of a skill folder

Usage:
    python utils/package_skill.py <path/to/skill-folder> [output-directory]

Example:
    python utils/package_skill.py skills/public/my-skill
    python utils/package_skill.py skills/public/my-skill ./dist
"""

import fnmatch
import os
import stat
import sys
import tempfile
import zipfile
from pathlib import Path
from scripts.quick_validate import validate_skill

# Patterns to exclude when packaging skills.
EXCLUDE_DIRS = {"__pycache__", "node_modules"}
EXCLUDE_GLOBS = {"*.pyc"}
EXCLUDE_FILES = {".DS_Store"}
# Directories excluded only at the skill root (not when nested deeper).
ROOT_EXCLUDE_DIRS = {"evals"}


def should_exclude(rel_path: Path) -> bool:
    """Check if a path should be excluded from packaging."""
    parts = rel_path.parts
    if any(part in EXCLUDE_DIRS for part in parts):
        return True
    # rel_path is relative to skill_path.parent, so parts[0] is the skill
    # folder name and parts[1] (if present) is the first subdir.
    if len(parts) > 1 and parts[1] in ROOT_EXCLUDE_DIRS:
        return True
    name = rel_path.name
    if name in EXCLUDE_FILES:
        return True
    return any(fnmatch.fnmatch(name, pat) for pat in EXCLUDE_GLOBS)


def package_files(skill_path: Path) -> list[tuple[Path, Path]]:
    """Preflight every included path before reading metadata or writing an archive."""
    files = []
    for file_path in sorted(skill_path.rglob('*')):
        arcname = file_path.relative_to(skill_path.parent)
        if should_exclude(arcname):
            continue
        if file_path.is_symlink() or not file_path.resolve().is_relative_to(skill_path):
            raise ValueError(f"symlink or external package path: {arcname}")
        if file_path.is_file():
            files.append((file_path, arcname))
    return files


def read_package_file(file_path: Path, skill_path: Path) -> bytes:
    """Reject changed links and non-regular files before reading their bytes."""
    before = file_path.lstat()
    if not stat.S_ISREG(before.st_mode) or not file_path.resolve().is_relative_to(skill_path):
        raise ValueError(f"unsafe package file: {file_path.name}")
    descriptor = os.open(file_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError(f"package file changed during read: {file_path.name}")
        return stream.read()


def package_skill(skill_path, output_dir=None):
    """
    Package a skill folder into a .skill file.

    Args:
        skill_path: Path to the skill folder
        output_dir: Optional output directory for the .skill file (defaults to current directory)

    Returns:
        Path to the created .skill file, or None if error
    """
    skill_path = Path(skill_path)
    if skill_path.is_symlink():
        print("❌ Skill root must not be a symlink")
        return None
    skill_path = skill_path.resolve()

    # Validate skill folder exists
    if not skill_path.exists():
        print(f"❌ Error: Skill folder not found: {skill_path}")
        return None

    if not skill_path.is_dir():
        print(f"❌ Error: Path is not a directory: {skill_path}")
        return None

    # Validate SKILL.md exists
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        print(f"❌ Error: SKILL.md not found in {skill_path}")
        return None

    try:
        files = package_files(skill_path)
    except (ValueError, OSError) as exc:
        print(f"❌ Package preflight failed: {exc}")
        return None

    # Run validation before packaging
    print("🔍 Validating skill...")
    valid, message = validate_skill(skill_path)
    if not valid:
        print(f"❌ Validation failed: {message}")
        print("   Please fix the validation errors before packaging.")
        return None
    print(f"✅ {message}\n")

    # Determine output location
    skill_name = skill_path.name
    if output_dir:
        output_path = Path(output_dir).resolve()
    else:
        output_path = Path.cwd()

    skill_filename = output_path / f"{skill_name}.skill"
    if output_path.is_relative_to(skill_path):
        print("❌ Output directory must be outside the skill directory")
        return None

    # Create the .skill file (zip format)
    temporary_path = None
    try:
        output_path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=output_path, suffix='.skill.tmp', delete=False) as temporary:
            temporary_path = Path(temporary.name)
        with zipfile.ZipFile(temporary_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for file_path, arcname in files:
                payload = read_package_file(file_path, skill_path)
                info = zipfile.ZipInfo.from_file(file_path, arcname.as_posix())
                info.compress_type = zipfile.ZIP_DEFLATED
                zipf.writestr(info, payload)
                print(f"  Added: {arcname}")
        temporary_path.replace(skill_filename)

        print(f"\n✅ Successfully packaged skill to: {skill_filename}")
        return skill_filename

    except Exception as e:
        print(f"❌ Error creating .skill file: {e}")
        return None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def main():
    if len(sys.argv) < 2:
        print("Usage: python utils/package_skill.py <path/to/skill-folder> [output-directory]")
        print("\nExample:")
        print("  python utils/package_skill.py skills/public/my-skill")
        print("  python utils/package_skill.py skills/public/my-skill ./dist")
        sys.exit(1)

    skill_path = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else None

    print(f"📦 Packaging skill: {skill_path}")
    if output_dir:
        print(f"   Output directory: {output_dir}")
    print()

    result = package_skill(skill_path, output_dir)

    if result:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
