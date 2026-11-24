"""Run with Blender's Python and the extension wheels on PYTHONPATH.

Optional arguments are real RAW files. All writes take place in a temporary directory.
"""
import importlib.util
from pathlib import Path
import struct
import sys
import tempfile
from unittest.mock import patch

import numpy as np
import OpenImageIO as oiio
import pyexiv2


spec = importlib.util.spec_from_file_location("br_metadata", Path(__file__).parents[1] / "blender_raw/metadata.py")
metadata = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metadata)


def create_render(path):
    out = oiio.ImageOutput.create(str(path))
    assert out.open(str(path), oiio.ImageSpec(28, 12, 3, oiio.UINT8))
    pixels = np.arange(12 * 28 * 3, dtype=np.uint8).reshape(12, 28, 3)
    assert out.write_image(pixels)
    assert out.close()


def scan(path):
    data = path.read_bytes()
    pos = 2
    while pos < len(data):
        assert data[pos] == 255
        if data[pos + 1] == 0xda:
            return data[pos:]
        pos += 2 + int.from_bytes(data[pos + 2:pos + 4], "big")
    raise AssertionError("No JPEG scan in output")


def run(folder, sources):
    original = folder / "Capture.jpg"
    create_render(original)
    with pyexiv2.Image(str(original)) as src:
        src.modify_exif({
            "Exif.Image.Orientation": "6", "Exif.Image.Make": "Test Camera",
            "Exif.Photo.ExposureTime": "1/125", "Exif.Photo.DateTimeOriginal": "2024:12:10 10:20:30",
            "Exif.Photo.PixelXDimension": "1111", "Exif.Photo.PixelYDimension": "2222",
            "Exif.GPSInfo.GPSLatitudeRef": "N", "Exif.GPSInfo.GPSLatitude": "30/1 12/1 13/1",
            "Exif.GPSInfo.GPSLongitudeRef": "E", "Exif.GPSInfo.GPSLongitude": "110/1 12/1 13/1",
        })
        src.modify_xmp({"Xmp.dc.subject": ["Portrait", "Studio"], "Xmp.xmp.Rating": "5",
                        "Xmp.dc.title": {"lang=\"x-default\"": "Sample"},
                        "Xmp.tiff.Orientation": "6", "Xmp.crs.Exposure2012": "2.0"})
        src.modify_iptc({"Iptc.Application2.Keywords": ["Testing", "Metadata"],
                         "Iptc.Application2.Copyright": "© Studio"})
    target = folder / "Render.jpg"
    for source in [original, *sources]:
        create_render(target)
        before = scan(target)
        # Valid ICC header with no tags; metadata transfer must preserve all bytes.
        icc = bytearray(132)
        struct.pack_into(">I", icc, 0, 132)
        icc[36:40] = b"acsp"
        with pyexiv2.Image(str(target)) as dest:
            dest.modify_icc(bytes(icc))
            dest.modify_exif({"Exif.Image.Orientation": "1", "Exif.Photo.ColorSpace": "1"})
        if sys.platform == "win32" and source == original:
            replace = metadata.os.replace
            attempts = []

            def replace_while_busy(working, path):
                attempts.append(path)
                if len(attempts) == 1:
                    with path.open("rb"):
                        return replace(working, path)
                return replace(working, path)

            with patch.object(metadata.os, "replace", replace_while_busy):
                assert metadata.copy_to_jpeg(source, target) == ""
            assert len(attempts) >= 2 and all(path == target for path in attempts)
        else:
            assert metadata.copy_to_jpeg(source, target) == ""
        assert scan(target) == before, "JPEG image scan changed"
        with pyexiv2.Image(str(source)) as src, pyexiv2.Image(str(target)) as dst:
            source_exif, output_exif = src.read_exif(), dst.read_exif()
            expected = metadata._capture_exif(source_exif)
            assert expected and all(output_exif.get(k) == v for k, v in expected.items())
            assert output_exif["Exif.Image.Orientation"] == "1" and output_exif["Exif.Photo.ColorSpace"] == "1"
            assert output_exif["Exif.Photo.PixelXDimension"] == "28" and output_exif["Exif.Photo.PixelYDimension"] == "12"
            assert (dst.get_pixel_width(), dst.get_pixel_height()) == (28, 12)
            assert dst.read_icc() == bytes(icc) and not dst.read_thumbnail()
            assert not any(k.startswith(("Exif.Nikon", "Exif.Sigma", "Exif.Image.StripOffsets")) for k in output_exif)
            if source == original:
                assert dst.read_xmp()["Xmp.dc.subject"] == ["Portrait", "Studio"]
                assert dst.read_xmp()["Xmp.dc.title"] == src.read_xmp()["Xmp.dc.title"]
                assert "Xmp.crs.Exposure2012" not in dst.read_xmp()
                assert "Xmp.tiff.Orientation" not in dst.read_xmp()
                assert dst.read_iptc()["Iptc.Application2.Keywords"] == ["Testing", "Metadata"]
                assert dst.read_iptc()["Iptc.Application2.Copyright"] == "© Studio"
            print("Preserved capture metadata:", source.name, len(expected), "EXIF/GPS fields")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as folder:
        run(Path(folder), [Path(p) for p in sys.argv[1:]])
