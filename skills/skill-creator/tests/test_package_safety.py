"""Regression tests for portable skill archive boundaries."""
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import package_skill


class PackageSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.skill = self.root / "sample-skill"
        self.skill.mkdir()
        (self.skill / "SKILL.md").write_text('---\nname: sample-skill\ndescription: A valid test skill\n---\nExample body\n')
        self.output = self.root / "out"
        self.external = self.root / "private-note.txt"
        self.external.write_text("DUMMY OUTSIDE CONTENT")

    def tearDown(self):
        self.temp.cleanup()

    def package(self, source=None):
        with contextlib.redirect_stdout(io.StringIO()):
            return package_skill.package_skill(source or self.skill, self.output)

    def test_normal_files_are_packaged_and_eval_directory_is_excluded(self):
        (self.skill / "references").mkdir()
        (self.skill / "references/note.md").write_text("public")
        (self.skill / "evals").mkdir()
        (self.skill / "evals/private.md").write_text("not distributed")
        with zipfile.ZipFile(self.package()) as archive:
            self.assertEqual({"sample-skill/SKILL.md", "sample-skill/references/note.md"}, set(archive.namelist()))
            self.assertEqual(b"public", archive.read("sample-skill/references/note.md"))

    def test_external_file_link_is_rejected_before_output_creation(self):
        (self.skill / "reference.txt").symlink_to(self.external)
        self.assertIsNone(self.package())
        self.assertFalse(self.output.exists())

    def test_executable_permissions_are_preserved(self):
        script = self.skill / "run.sh"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
        with zipfile.ZipFile(self.package()) as archive:
            self.assertEqual(0o755, (archive.getinfo("sample-skill/run.sh").external_attr >> 16) & 0o777)

    def test_internal_file_link_is_also_rejected(self):
        (self.skill / "reference.txt").symlink_to(self.skill / "SKILL.md")
        self.assertIsNone(self.package())
        self.assertFalse(self.output.exists())

    def test_directory_and_dangling_links_are_rejected(self):
        for target in (self.root, self.root / "missing"):
            with self.subTest(target=target):
                link = self.skill / "linked"
                link.symlink_to(target, target_is_directory=True)
                self.assertIsNone(self.package())
                link.unlink()

    def test_symlinked_root_is_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.skill, target_is_directory=True)
        self.assertIsNone(self.package(alias))

    def test_symlinked_skill_metadata_is_not_read(self):
        (self.skill / "SKILL.md").unlink()
        (self.skill / "SKILL.md").symlink_to(self.external)
        with patch.object(package_skill, "validate_skill", side_effect=AssertionError("must not read metadata")):
            self.assertIsNone(self.package())

    def test_invalid_candidate_preserves_existing_archive(self):
        self.output.mkdir()
        archive = self.output / "sample-skill.skill"
        archive.write_bytes(b"previous archive")
        (self.skill / "leak.txt").symlink_to(self.external)
        self.assertIsNone(self.package())
        self.assertEqual(b"previous archive", archive.read_bytes())

    def test_changed_link_after_preflight_is_rejected_and_archive_preserved(self):
        candidate = self.skill / "note.txt"
        candidate.write_text("public")
        self.output.mkdir()
        previous = self.output / "sample-skill.skill"
        previous.write_bytes(b"previous archive")
        original = package_skill.read_package_file
        def swap(file_path, root):
            if file_path == candidate:
                candidate.unlink()
                candidate.symlink_to(self.external)
            return original(file_path, root)
        with patch.object(package_skill, "read_package_file", swap):
            self.assertIsNone(self.package())
        self.assertEqual(b"previous archive", previous.read_bytes())
        self.assertEqual([previous], list(self.output.iterdir()))

    def test_output_inside_source_is_rejected(self):
        with contextlib.redirect_stdout(io.StringIO()):
            result = package_skill.package_skill(self.skill, self.skill / "dist")
        self.assertIsNone(result)
        self.assertFalse((self.skill / "dist").exists())


if __name__ == "__main__":
    unittest.main()
