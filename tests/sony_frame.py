"""Check ARW crop alignment, then develop the four Sony ILCE-7M5 fixtures.

blender -b --python-exit-code 1 --python tests/sony_frame.py -- RAW_DIRECTORY
"""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

import numpy as np
import OpenImageIO as oiio
import pyexiv2

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender_raw"))
import raw_backend


def orient(image, flip):
    if flip & 1:
        image = np.fliplr(image)
    if flip & 2:
        image = np.flipud(image)
    return image.swapaxes(0, 1) if flip & 4 else image


# Non-zero optical margins and an asymmetric crop catch offset/direction errors.
frame = np.arange(24 * 36).reshape((24, 36))
for flip in range(8):
    sizes = SimpleNamespace(width=36, height=24, left_margin=6, top_margin=8,
                            crop_left_margin=10, crop_top_margin=14,
                            crop_width=22, crop_height=14, flip=flip)
    for step in (1, 2):
        oriented = orient(frame[::step, ::step], flip)
        expected = orient(frame[6:20:step, 4:26:step], flip)
        actual = raw_backend._sony_frame(oriented, "sample.arw", sizes, step == 2)
        np.testing.assert_array_equal(actual, expected)


# The target window must scale the full source, including its far corner.
y, x = np.indices((120, 160), dtype=np.float32)
ramp = np.stack((x, y, x + y), axis=-1)
for percent in (50, 75):
    resized = raw_backend._resized(ramp, percent=percent)
    for u, v in ((.1, .2), (.4, .7), (.9, .9)):
        row, col = round(v * (resized.shape[0] - 1)), round(u * (resized.shape[1] - 1))
        expected = ((col + .5) * 100 / percent - .5, (row + .5) * 100 / percent - .5)
        np.testing.assert_allclose(resized[row, col, :2], expected, atol=.2)


def check_edges(path):
    image = oiio.ImageBuf(str(path))
    width, height = image.spec().width, image.spec().height
    for x0, x1, y0, y1 in ((0, width, 0, 1), (0, width, height - 1, height),
                           (0, 1, 0, height), (width - 1, width, 0, height)):
        edge = image.get_pixels(oiio.FLOAT, oiio.ROI(x0, x1, y0, y1, 0, 1, 0, 3))
        assert np.isfinite(edge).all() and np.quantile(edge, .99) > .001, (path, x0, y0)


folder = Path(sys.argv[sys.argv.index("--") + 1])
paths = sorted(p for p in folder.iterdir() if p.suffix.lower() == ".arw")
assert paths
with tempfile.TemporaryDirectory(prefix="BlenderRaw-Sony-frame-") as output:
    for source in paths:
        with pyexiv2.Image(str(source)) as original:
            metadata = original.read_exif()
        width, height = map(int, metadata["Exif.SubImage1.DefaultCropSize"].split())
        if metadata["Exif.Image.Orientation"] in {"5", "6", "7", "8"}:
            width, height = height, width
        for scale in ("proxy", 50, 75, 100):
            settings = {"preview": True} if scale == "proxy" else {"preview_percent": scale}
            factor = min(1, 1600 / max(width, height)) if scale == "proxy" else scale / 100
            expected = (round(width * factor), round(height * factor))
            path = Path(output) / f"{source.stem}-{scale}.exr"
            assert raw_backend.decode(source, path, settings) == expected, (source, scale)
            check_edges(path)
            print(source.name, scale, expected, "four populated edges", flush=True)
            path.unlink()
