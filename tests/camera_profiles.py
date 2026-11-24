"""Validate the installed offline profiles with the public installer fixtures.

blender -b --offline-mode --python-exit-code 1 --python tests/camera_profiles.py -- FIXTURE_DIR
--write FILE records small scene-linear RGB patch averages. --compare FILE compares a second platform.
"""
import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
from unittest.mock import patch

import bpy
import numpy as np
import OpenImageIO as oiio
import pyexiv2

from bl_ext.user_default.raw_photo_editor import camera_profiles, catalog, compositor, jobs, raw_backend


def audit_catalog():
    directory = camera_profiles.DIRECTORY
    rows = json.loads((directory / "index.json").read_text(encoding="utf-8"))
    local_models = {"Sigma FP2", "LEICA M10-R", "Canon EOS R8 Mark II", "ILCE-7RM6"}
    assert {name.casefold() for name in local_models} == {r["model"].casefold() for r in rows}
    notice = (directory / "NOTICE.md").read_text(encoding="utf-8")
    matrix_license = (directory / "LICENSE.RAWmakase").read_text(encoding="utf-8")
    assert "LICENSE.RAWmakase" in notice and "Permission is hereby granted" in matrix_license
    matrices = set()
    for row in rows:
        assert camera_profiles.match(row["make"], row["model"]) == row
        if row["source"] == "matrix":
            matrices.add(row["model"])
            values = np.array(row["color_matrix"])
            assert values.shape == (9,) and np.isfinite(values).all(), row
            assert np.max(np.abs(values)) < 10 and np.linalg.det(values.reshape(3, 3)) > 1e-6, row
            assert row["illuminant"] == 21, row
            assert row["upstream"] in notice and row["model"] in notice, row
            license_name, copyright_line = row["notice"].split(", ", 1)
            assert license_name == "MIT" and copyright_line in matrix_license, row
    assert not list(directory.glob("*.dcp"))
    assert {"Canon EOS R8 Mark II", "ILCE-7RM6"} <= matrices
    assert camera_profiles.match("Canon", "Canon EOS R5 Unknown Variant") is None
    assert camera_profiles.match("Unknown", "NIKON Z 8") is None
    print(f"CATALOG {len(rows)} camera records, {len(matrices)} matrices verified", flush=True)


def read_pixels(path):
    buf = oiio.ImageBuf(str(path))
    rgb = buf.get_pixels(oiio.FLOAT)
    assert not buf.has_error and rgb.shape[-1] == 3 and np.isfinite(rgb).all(), path
    return rgb


def develop(source, target, **settings):
    raw_backend.decode(str(source), str(target), settings)
    return read_pixels(target)


def patch_means(rgb):
    # A 4x4 grid compares broad color and luminance without assuming bit-identical
    # demosaicing or floating-point reduction on different processors.
    return [patch.mean(axis=(0, 1), dtype=np.float64).round(7).tolist()
            for band in np.array_split(rgb, 4, axis=0)
            for patch in np.array_split(band, 4, axis=1)]


def settled_image(photo, node):
    compositor.refresh_photo(photo, preview=True)
    deadline = time.monotonic() + 240
    while jobs.developing(photo.uid):
        jobs.tick()
        assert time.monotonic() < deadline, (node.error, photo.error)
        time.sleep(.05)
    assert not photo.error and not node.error, (photo.error, node.error)
    image = node.node_tree.nodes["Developed RAW"].image
    req = catalog.request(photo, "preview", recipe=node)
    assert image and Path(image.filepath).stem == req["key"]
    assert photo.image, (photo.name, photo.status, photo.decode_key)
    assert (image == photo.image) == (req["key"] == photo.decode_key), (
        image.name, image.filepath, req["key"], photo.image.name, photo.image.filepath, photo.decode_key)
    return image, read_pixels(bpy.path.abspath(image.filepath))


def check_reset(recipe):
    recipe.camera_input = "DCP"
    recipe.look = "PROFILE"
    # Blender's Reset to Default writes the declared enum number. A dynamic
    # enum has no static items, so read the declaration to resolve that number.
    prop = type(recipe).__annotations__["camera_input"].keywords
    choices = {number: (identifier, label) for identifier, label, _, number in prop["items"](recipe, bpy.context)}
    assert choices[prop["default"]][0] == catalog.DEFAULT_CAMERA_INPUT, (prop["default"], choices)
    recipe.camera_input = choices[prop["default"]][0]
    assert (recipe.camera_input, recipe.look) == (catalog.DEFAULT_CAMERA_INPUT, "NONE")
    assert {item[0] for item in catalog.look_items(recipe, bpy.context)} == {"NONE", "LUT"}


def check_half_green(linear, looked):
    # The fixture preserves red and blue and halves green across the unit cube.
    within_cube = ((linear >= 0) & (linear <= 1)).all(axis=-1)
    assert linear[within_cube, 1].max() > .01
    np.testing.assert_allclose(looked[within_cube], linear[within_cube] * [1, .5, 1], rtol=2e-5, atol=2e-6)


def check_inputs_and_looks(source, target, external_dcp):
    assert bpy.ops.blender_raw.import_photos(filepath=str(source)) == {"FINISHED"}
    photo = catalog.library(bpy.context).photos[-1]
    assert (photo.camera_input, photo.look) == (catalog.DEFAULT_CAMERA_INPUT, "NONE")
    assert bpy.ops.blender_raw.open_photo(uid=photo.uid) == {"FINISHED"}
    node, = compositor.developers_for(photo)
    assert (node.camera_input, node.look) == (catalog.DEFAULT_CAMERA_INPUT, "NONE")
    for recipe in (photo, node):
        check_reset(recipe)
    node.camera_input = "AUTO"  # Explicit LibRaw calibration for this reference.
    off_img, off = settled_image(photo, node)
    # Exercise a second Developer application after both jobs have settled.
    # On Windows the first request can carry a short TEMP path and the next a long path.
    jobs.developer_later(photo, node)
    warm_img, warm = settled_image(photo, node)
    assert warm_img == off_img == photo.image
    np.testing.assert_array_equal(warm, off)
    source_label = off_img["photo_editor_profile"]
    assert "LibRaw" in source_label, source_label
    node.lut_path = str(Path(__file__).resolve().parent / "fixtures" / "half-green.cube")
    node.lut_space = "LINEAR"
    node.look = "LUT"
    on_img, on = settled_image(photo, node)
    assert on_img["photo_editor_profile"] == source_label and off_img != on_img
    assert on.shape == off.shape and np.mean(np.abs(on - off)) > 1e-4
    check_half_green(off, on)

    node.camera_input = "DCP"
    node.profile_path = str(external_dcp)
    assert node.look == "LUT"
    _, dcp_lut = settled_image(photo, node)
    node.look = "NONE"
    _, dcp_linear = settled_image(photo, node)
    check_half_green(dcp_linear, dcp_lut)
    node.look = "PROFILE"
    dcp_img, dcp = settled_image(photo, node)
    assert "Custom DCP" in dcp_img["photo_editor_profile"]
    profile = dict(preview=True, profile_path=node.profile_path, camera_input="DCP", look="PROFILE")
    np.testing.assert_allclose(dcp, develop(source, target, **profile), rtol=2e-5, atol=2e-6)
    old = develop(source, target, preview=True, profile_path=node.profile_path, use_dcp_look=True, use_dcp_curve=True)
    # Supply both generations with unblended camera samples to check all DCP
    # tables and the tone curve, including their behavior in the brightest pixels.
    with patch.object(raw_backend, "_HIGHLIGHT_START", 65535):
        unblended = develop(source, target, **profile)
    np.testing.assert_allclose(unblended, old, rtol=2e-5, atol=2e-6)
    node.profile_mode = "AUTO"
    assert node.camera_input == "LEGACY"
    old_img, old_auto = settled_image(photo, node)
    assert np.isfinite(old_auto).all() and "Bundled DCP" not in old_img["photo_editor_profile"]
    node.property_unset("profile_mode")
    assert node.profile_mode == "EMBEDDED" and not compositor.uses_camera_adjustments(node)
    if not photo.get("apple_raw_reader") and not photo.get("reader_camera_input"):
        assert catalog.request(photo, "preview", recipe=node)["key"] == photo.decode_key
    print("GALLERY verified 3D LUT after Default and Custom DCP", source.name, source_label, flush=True)
    return {"3D LUT": patch_means(on), "None": patch_means(off)}


def check_baseline(source, folder):
    # The public Leica M8 DNG has no profile appearance tables. Changing only
    # BaselineExposure provides a physical one-stop test without distributing a RAW.
    zero, plus = folder / "zero.dng", folder / "plus.dng"
    for copy, exposure in ((zero, "0/1"), (plus, "1/1")):
        shutil.copyfile(source, copy)
        with pyexiv2.Image(str(copy)) as image:
            image.modify_exif({"Exif.Image.BaselineExposure": exposure})
    for preview in (True, False):
        # The full sensor path demosaics at full size before the requested resize.
        settings = {"preview": preview, "preview_percent": 25}
        current = dict(camera_input="AUTO", look="NONE", **settings)
        auto = develop(plus, folder / "auto.exr", **current)
        baseline = develop(zero, folder / "baseline.exr", **current)
        legacy = develop(plus, folder / "legacy.exr", **settings)
        unchanged = develop(source, folder / "original.exr", **settings)
        np.testing.assert_array_equal(legacy, unchanged)
        np.testing.assert_allclose(auto, baseline * 2, rtol=2e-5, atol=2e-6)
    print("DNG +1 EV doubles linear RGB in proxy and full sensor paths; legacy retains original scale", flush=True)


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("fixtures", type=Path)
parser.add_argument("--write", type=Path)
parser.add_argument("--compare", type=Path)
args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
audit_catalog()
photos = catalog.library(bpy.context).photos
old = photos.add()  # A saved 0.9 photo has no Camera Input field.
assert old.camera_input == "LEGACY"
photos.remove(len(photos) - 1)
with tempfile.TemporaryDirectory(prefix="Photo Editor calibration ") as tmp:
    work = Path(tmp)
    samples = {}
    for filename, profile in (("EOS-R5.CR3", "Canon EOS R5.dcp"), ("Z8-HE.NEF", "NIKON Z 8.dcp")):
        samples[filename] = check_inputs_and_looks(args.fixtures / filename, work / "custom.exr", args.fixtures / profile)
    check_baseline(args.fixtures / "Leica-M8.DNG", work)
if args.write:
    args.write.write_text(json.dumps(samples, separators=(",", ":")), encoding="utf-8")
if args.compare:
    reference = json.loads(args.compare.read_text(encoding="utf-8-sig"))
    assert reference.keys() == samples.keys()
    for filename in samples:
        for look, patches in samples[filename].items():
            np.testing.assert_allclose(patches, reference[filename][look], rtol=.002, atol=.002, err_msg=f"{filename} {look}")
    print("CROSS-PLATFORM scene-linear RGB patch averages agree", flush=True)
