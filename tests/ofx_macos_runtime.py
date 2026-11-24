"""OFX Mac discovery and executable repair without requiring a Metal device.

Run with Blender's bundled Python on Windows or macOS.
"""
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender_raw"))
import spektrafilm_ofx_engine as engine


class MacRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="ofx-mac-runtime-")
        self.root = Path(self.scratch.name)
        self.previous = engine._configured_path
        engine.configure("")

    def tearDown(self):
        engine.configure(self.previous)
        self.scratch.cleanup()

    def helper(self):
        helper = MagicMock(spec=Path)
        helper.is_file.return_value = True
        helper.stat.return_value = SimpleNamespace(st_mode=stat.S_IFREG | 0o640, st_size=123, st_mtime_ns=1)
        helper.__str__.return_value = str(self.root / "photo_ofx_host")
        return helper

    def bundle(self, base, folder="MacOS"):
        bundle = base / "spektrafilm.ofx.bundle"
        binary = bundle / "Contents" / folder / "spektrafilm.ofx"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"fixture plugin")
        (bundle / "Contents/Resources").mkdir()
        return bundle, binary

    def test_mac_default_search_and_explicit_bundle(self):
        bundle, binary = self.bundle(self.root / "Library/OFX/Plugins")
        with patch.object(engine.sys, "platform", "darwin"), \
             patch.object(engine.platform, "machine", return_value="arm64"), \
             patch.object(engine.Path, "home", return_value=self.root), \
             patch.dict(os.environ, {"BLENDER_PHOTO_SPEKTRAFILM_OFX": ""}), \
             patch.object(engine, "_helper", return_value=self.helper()):
            # Ignore an installed system bundle, so the per-user fallback is deterministic.
            original = engine._bundle_at
            with patch.object(engine, "_bundle_at", side_effect=lambda p: None if p == "/Library/OFX/Plugins" else original(p)):
                self.assertEqual(engine.discover_plugin(), str(bundle.resolve()))
            self.assertEqual(engine.discover_plugin(str(binary)), str(bundle.resolve()))
            self.assertEqual(engine.backend_error(str(bundle)), "")
            self.assertEqual(engine._plugin_binary(bundle), binary)

    def test_native_architecture_folders_and_wrong_platform(self):
        for index, folder in enumerate(("MacOS/arm64", "MacOS/universal")):
            bundle, binary = self.bundle(self.root / str(index), folder)
            with patch.object(engine.sys, "platform", "darwin"), \
                 patch.object(engine, "_helper", return_value=self.helper()):
                self.assertEqual(engine.backend_error(str(bundle)), "")
                self.assertEqual(engine._plugin_binary(bundle), binary)
                stamp = engine.cache_identity(str(bundle))[3][0]
                self.assertEqual(stamp[0], f"Contents/{folder}/spektrafilm.ofx")
            with patch.object(engine.sys, "platform", "win32"), \
                 patch.object(engine, "_helper", return_value=self.helper()):
                self.assertIn("different platform", engine.backend_error(str(bundle)))

    def test_missing_execute_bit_is_repaired_without_rewriting_binary(self):
        helper = self.helper()
        with patch.object(engine.sys, "platform", "darwin"), \
             patch.object(engine, "_helper", return_value=helper), \
             patch.object(engine.os, "access", side_effect=[False, True]):
            self.assertIs(engine._executable_helper(), helper)
        helper.chmod.assert_called_once_with(0o740)
        helper.write_bytes.assert_not_called()
        helper.write_text.assert_not_called()

    def test_executable_mac_and_windows_helpers_are_not_chmodded(self):
        for platform in ("darwin", "win32"):
            helper = self.helper()
            with patch.object(engine.sys, "platform", platform), \
                 patch.object(engine, "_helper", return_value=helper), \
                 patch.object(engine.os, "access", return_value=True):
                self.assertIs(engine._executable_helper(), helper)
            helper.chmod.assert_not_called()

    def test_permission_failure_is_actionable_and_does_not_launch(self):
        helper = self.helper()
        helper.chmod.side_effect = PermissionError("read only")
        with patch.object(engine.sys, "platform", "darwin"), \
             patch.object(engine, "_helper", return_value=helper), \
             patch.object(engine.os, "access", return_value=False), \
             patch.object(engine.tempfile, "gettempdir", return_value=str(self.root)), \
             patch.object(engine.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "writable user directory"):
                engine._run(["describe", "bundle", "metadata.json"])
            run.assert_not_called()

    def test_run_uses_private_config_and_reports_execution_denial(self):
        helper = self.helper()
        before = dict(os.environ)
        result = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(engine.sys, "platform", "darwin"), \
             patch.object(engine, "_helper", return_value=helper), \
             patch.object(engine.os, "access", return_value=True), \
             patch.object(engine.tempfile, "gettempdir", return_value=str(self.root)), \
             patch.object(engine.subprocess, "run", return_value=result) as run:
            engine._run(["describe", "bundle", "metadata.json"], timeout=8)
            args, kwargs = run.call_args
            self.assertEqual(args[0][0], str(helper))
            self.assertNotIn("creationflags", kwargs)
            self.assertEqual(kwargs["timeout"], 8)
            environment = kwargs["env"]
            self.assertEqual(environment["HOME"], environment["APPDATA"])
            self.assertEqual(environment["HOME"], environment["XDG_CONFIG_HOME"])
            self.assertTrue(Path(environment["HOME"]).is_relative_to(self.root))
            self.assertEqual(environment["BPE_OFX_PARENT_PID"], str(os.getpid()))
            self.assertEqual(dict(os.environ), before)
            run.side_effect = PermissionError("execution denied")
            with self.assertRaisesRegex(RuntimeError, "operating-system security notice"):
                engine._run(["describe", "bundle", "metadata.json"])


if __name__ == "__main__":
    unittest.main()
