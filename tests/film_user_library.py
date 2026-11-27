# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise the persistent preset library with Python or Blender's bundled Python."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender_raw"))
import film_user_library as library


class PresetLibraryTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name) / "library"
        self.directory = patch.object(library, "_directory", return_value=self.root)
        self.directory.start()
        self.addCleanup(self.directory.stop)

    def test_library_and_stable_identity(self):
        settings = {"film": "kodak_portra_800", "grain": False, "vector": [0.5, 1.0, 0.25]}
        self.assertEqual(library.list_presets(), [])
        self.assertFalse(self.root.exists())
        first = library.save("  夜景 / Portra  ", settings)
        second = library.save("Portrait", {})
        self.assertEqual(library.list_presets(), [(second, "Portrait"), (first, "夜景 / Portra")])
        library.rename(first, "A 卷")
        self.assertEqual(library.list_presets()[0], (first, "A 卷"))
        self.assertEqual(library.load(first), settings)
        library.delete(second)
        self.assertEqual(library.list_presets(), [(first, "A 卷")])
        with self.assertRaises(FileNotFoundError):
            library.load(second)

    def test_invalid_name_collision_and_ids(self):
        first = library.save("Café", {})
        second = library.save("Other", {})
        for invalid in [" ", "bad\x00name", "bad\nname", "Cafe\u0301"]:
            with self.subTest(name=invalid), self.assertRaises(ValueError):
                library.save(invalid, {})
        with self.assertRaises(ValueError):
            library.rename(second, "CAFÉ")
        with self.assertRaises(ValueError):
            library.delete("../outside")
        self.assertEqual(library.list_presets(), [(first, "Café"), (second, "Other")])

    def test_builtin_presets_and_same_named_user_copy(self):
        self.assertEqual(len(library.BUILTIN_PRESETS), 4)
        for key, name in library.BUILTIN_PRESETS.items():
            settings = library.load(key)
            user_id = library.save(name, {"film": "custom"})
            self.assertEqual(library.load(user_id), {"film": "custom"})
            with self.assertRaisesRegex(ValueError, "read-only"):
                library.rename(key, "Renamed")
            with self.assertRaisesRegex(ValueError, "read-only"):
                library.delete(key)
            library.delete(user_id)
            self.assertEqual(library.load(key), settings)
        self.assertFalse(library.list_presets())

    def test_failed_replacement_preserves_committed_file(self):
        first = library.save("Original", {"grain": True})
        with patch.object(os, "replace", side_effect=OSError("interrupted")), self.assertRaises(OSError):
            library.rename(first, "Updated")
        (self.root / ("f" * 32 + ".json")).write_text("{", encoding="utf-8")
        self.assertEqual(library.list_presets(), [(first, "Original")])
        self.assertEqual(library.load(first), {"grain": True})
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_simultaneous_writer_leaves_library_consistent(self):
        first = library.save("Original", {})
        with library._writing(), self.assertRaises(OSError):
            library.rename(first, "Second instance")
        self.assertEqual(library.list_presets(), [(first, "Original")])
        library.rename(first, "Retried")
        self.assertEqual(library.list_presets(), [(first, "Retried")])

    def test_multiple_blender_profiles_share_the_same_user_location(self):
        self.directory.stop()
        home = Path(self.scratch.name)
        profiles = [str(home / "Blender" / version / "config") for version in ("5.3", "6.0")]
        suffix = Path("RAW Photo Editor", "Spektrafilm Lite")
        with patch.object(Path, "home", return_value=home):
            for platform, expected in [
                ("darwin", home / "Library" / "Application Support" / "Blender" / suffix),
                ("win32", home / "Roaming" / "Blender Foundation" / "Blender" / suffix),
            ]:
                for profile in profiles:
                    with patch.object(sys, "platform", platform), patch.dict(os.environ, {
                        "BLENDER_USER_CONFIG": profile, "APPDATA": str(home / "Roaming"),
                    }):
                        self.assertEqual(library._directory(), expected)


if __name__ == "__main__":
    unittest.main()
