"""Check clipped lamp cores and measured colors through the installed LibRaw decoder.

Run with the extension's Python wheels available:
blender -b --python-exit-code 1 --python tests/raw_highlights.py

The fixture is a synthetic uncompressed 12-bit RGGB DNG (stored as uint16 with
WhiteLevel 4095). Twelve equal-width patches span its 3456 x 96 sensor. Its D65
matrix is diag(1/2, 1, 5/8) times the XYZ-to-Rec.709 matrix. This makes the
expected Rec.709 colors independent of a manufacturer's profile or a JPEG.
"""
import gzip
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import numpy as np
import OpenImageIO as oiio
import bpy

if "bl_ext.user_default.raw_photo_editor" in bpy.context.preferences.addons:
    from bl_ext.user_default.raw_photo_editor import raw_backend
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender_raw"))
    import raw_backend


# Three pairs at the end are colored emitters. One sensor channel rises just
# two ADC codes to its ceiling. The other two channels stay at low, fixed levels.
NEAR = 4093 / 4095
CAMERA_PATCHES = np.array([
    (.5, 1, .625), (1, 1, 1), (.09, .18, .1125), (.185, .37, .23125),
    (.25, .10, .02), (.03, .06, .375), (NEAR, .12, .04), (1, .12, .04),
    (.04, .10, NEAR), (.04, .10, 1), (.04, NEAR, .09), (.04, 1, .09),
])


def decode_patches(source, output, settings, size):
    assert raw_backend.decode(source, output, settings) == size
    reader = oiio.ImageInput.open(str(output))
    assert reader, oiio.geterror()
    try:
        frame = reader.read_image(format=oiio.FLOAT)
    finally:
        reader.close()
    assert frame.shape == (size[1], size[0], 3) and np.isfinite(frame).all()
    # Sample the interiors, away from demosaic and resizing footprints.
    x = (np.arange(len(CAMERA_PATCHES)) + .5) * size[0] / len(CAMERA_PATCHES)
    return frame[size[1] // 2, x.astype(int)]


def check(source, folder, name, settings, size):
    current = decode_patches(source, folder / (name + ".exr"), dict(camera_input="AUTO", look="NONE", **settings), size)
    legacy = decode_patches(source, folder / (name + "-legacy.exr"), settings, size)
    core = current[1]
    assert np.ptp(core) < .005 * np.max(core), (name, "clipped core acquired a tint", core)
    if settings.get("white_balance", "CAMERA") == "CAMERA":
        # The light is brighter than scene white. Keep the available headroom.
        assert core.min() > 1.2, (name, "core lost scene-linear headroom", core)
        np.testing.assert_allclose(current[[0, 2, 3, 4, 5]],
                                   CAMERA_PATCHES[[0, 2, 3, 4, 5]] * (2, 1, 1.6),
                                   atol=.001, rtol=0, err_msg=name)
    # The gray ramp and the two genuinely warm/cool patches have no clipped
    # samples. Their existing values include the user's chosen white balance.
    np.testing.assert_allclose(current[2:6], legacy[2:6], rtol=0, atol=2e-6, err_msg=name)
    for i in (6, 8, 10):
        before, at_white = current[i:i + 2]
        np.testing.assert_allclose(before, legacy[i], rtol=.015, atol=.01,
                                   err_msg=f"{name}: colored emitter approaching saturation")
        np.testing.assert_allclose(at_white, before, rtol=.025, atol=.015,
                                   err_msg=f"{name}: color discontinuity at sensor saturation")
    print(f"PASS {name}, {size}, scene-linear lamp core {core.round(5)}", flush=True)


with TemporaryDirectory(prefix="bpe-raw-highlights-") as temporary:
    source = Path(temporary) / "highlight-chart.dng"
    source.write_bytes(gzip.decompress(Path(__file__).with_name("fixtures")
                                       .joinpath("highlight-chart.dng.gz").read_bytes()))
    for name, settings, size in (
        ("as shot", {}, (3456, 96)),
        ("half-size demosaic and proxy resize", {"preview": True}, (1600, 44)),
        ("50 percent", {"preview_percent": 50}, (1728, 48)),
        ("75 percent", {"preview_percent": 75}, (2592, 72)),
        ("auto WB", {"white_balance": "AUTO"}, (3456, 96)),
        ("custom WB", {"white_balance": "CUSTOM", "temperature": 3800, "tint": -7}, (3456, 96)),
        ("sampled gray WB", {"white_balance": "SAMPLE", "neutral_x": 2.5/12, "neutral_y": .5}, (3456, 96)),
    ):
        check(source, Path(temporary), name, settings, size)
