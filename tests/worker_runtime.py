"""Run with either platform's Blender: blender -b --python-exit-code 1 --python tests/worker_runtime.py -- PHOTO.dng"""
from contextlib import closing
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch

import OpenImageIO as oiio

from bl_ext.user_default.raw_photo_editor import jobs


source = Path(sys.argv[sys.argv.index("--") + 1]).resolve()
with tempfile.TemporaryDirectory(prefix="Photo Editor ") as scratch:
    work = Path(scratch)
    photo = work / ("相片 01" + source.suffix)
    shutil.copyfile(source, photo)
    for mode, suffix in (("thumb", ".jpg"), ("preview", ".exr")):
        output = work / ("预览 01" + suffix)
        request = {"mode": mode, "source": str(photo), "output": str(output), "settings": {"preview": True}}
        # A macOS installation may have only a versioned Python binary. A
        # guessed <prefix>/bin/python3 must not replace Blender's executable.
        with patch.object(sys, "prefix", str(work / "no unversioned interpreter")):
            child, workspace = jobs._start(request)
        try:
            assert child.wait(timeout=180) == 0, jobs._worker_error(workspace)
            reader = oiio.ImageInput.open(str(output))
            assert reader, output
            with closing(reader):
                assert min(reader.spec().width, reader.spec().height) > 1
                print(mode, reader.format_name(), reader.spec().width, reader.spec().height, flush=True)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            workspace.cleanup()
