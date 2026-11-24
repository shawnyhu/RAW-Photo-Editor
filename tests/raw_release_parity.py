"""Compare installed camera-default output against a reference build on the same host."""
from pathlib import Path
import sys

import bpy
import numpy as np
import OpenImageIO as oiio

mode, fixtures, output = sys.argv[sys.argv.index("--") + 1:]
assert mode in {"reference", "compare"}
if mode == "reference" and "bl_ext.user_default.blender_raw" in bpy.context.preferences.addons:
    from bl_ext.user_default.blender_raw import raw_backend
else:
    from bl_ext.user_default.raw_photo_editor import raw_backend
output = Path(output)
output.mkdir(exist_ok=True)
for name in ("EOS-R5.CR3", "Z8-HE.NEF", "D700.NEF", "Z-f.NEF"):
    for preview in (True, False):
        step = "preview" if preview else "full-sensor"
        expected = output / f"{name}-{step}.exr"
        actual = expected if mode == "reference" else output / "actual.exr"
        settings = dict(camera_input="AUTO", look="NONE", white_balance="CAMERA",
                        preview=preview, preview_percent=100 if preview else 25)
        raw_backend.decode(Path(fixtures) / name, actual, settings)
        if mode == "compare":
            old = oiio.ImageBuf(str(expected)).get_pixels(oiio.FLOAT)
            new = oiio.ImageBuf(str(actual)).get_pixels(oiio.FLOAT)
            assert new.shape == old.shape and np.isfinite(new).all()
            assert raw_backend.camera_input_name(expected) == raw_backend.camera_input_name(actual)
            delta = np.abs(new - old)
            print(name, step, old.shape, "maximum absolute pixel difference", float(delta.max()), flush=True)
            np.testing.assert_allclose(new, old, rtol=2e-5, atol=2e-5)
