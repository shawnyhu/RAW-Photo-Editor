"""Export switched and muted Raw Developer branches in a real three-photo batch.

blender -b --factory-startup --python-exit-code 1 --python tests/developer_export.py -- PHOTO.DNG
The extension's decoder wheels must be installed. Inputs, reference EXRs and exports
created by the test live in a temporary directory.
"""
from contextlib import closing
from pathlib import Path
import shutil
import sys
import tempfile
import time

import bpy
import numpy as np
import OpenImageIO as oiio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import blender_raw as addon
from blender_raw import metering, raw_backend


def sampled_rgb(path):
    with closing(oiio.ImageInput.open(str(path))) as reader:
        spec = reader.spec()
        width, height = spec.width, spec.height
        ys, xs = metering._sample_positions(height), metering._sample_positions(width)
        samples = reader.read_image(format=oiio.FLOAT)[ys[:, None], xs, :3]
    return (width, height), samples


def configure(photo, branch, missing_profile):
    graph = addon.compositor.ensure_tree(bpy.context, photo)
    reader = next(n for n in graph.nodes if n.bl_idname == addon.compositor.READER_TYPE)
    a = next(n for n in graph.nodes if n.bl_idname == addon.compositor.DEVELOPER_TYPE)
    b = graph.nodes.new(addon.compositor.DEVELOPER_TYPE)
    graph.links.new(reader.outputs["Image"], b.inputs["Image"])
    assert a.camera_input == b.camera_input == photo.camera_input == "AUTO"
    a.white_balance, a.temperature, a.exposure, a.auto_exposure = "CUSTOM", 3300, 0.75, "GLOBAL"
    if addon.catalog._APPLE_RAW_SUPPORTED:
        b.camera_input = "APPLE"
        b.apple_white_balance = "CUSTOM"
    else:
        b.white_balance = "CUSTOM"
    b.temperature, b.exposure, b.auto_exposure = 8200, -0.5, "HIGHLIGHTS"
    switch = graph.nodes.new("CompositorNodeSwitch")
    switch.inputs["Switch"].default_value = branch != "A"
    graph.links.new(a.outputs["Image"], switch.inputs["Off"])
    graph.links.new(b.outputs["Image"], switch.inputs["On"])
    junction = next(n for n in graph.nodes if n.bl_idname == "NodeReroute")
    graph.links.new(switch.outputs["Image"], junction.inputs[0])
    for node in (a, b):
        if (node == a and branch != "A") or (node == b and branch != "B"):
            node.camera_input = "DCP"
            node.profile_path = str(missing_profile)
    if branch == "Muted":
        b.mute = True
    photo.selected = True


def run(source, folder):
    addon.register()
    settings = bpy.context.scene.render.image_settings
    settings.file_format = "OPEN_EXR"
    settings.color_depth = "32"
    settings.exr_codec = "ZIP"
    references = {
        "A": (3300, 0.75, "GLOBAL"),
        "B": (8200, -0.5, "HIGHLIGHTS"),
        "Muted": (None, 0.0, "AS_SHOT"),
    }
    for branch in references:
        path = folder / (branch + source.suffix)
        shutil.copyfile(source, path)
        photo, _ = addon.catalog.add_photo(bpy.context, path)
        configure(photo, branch, folder / "missing-profile.dcp")
    output = folder / "batch"
    assert bpy.ops.blender_raw.export_photos(directory=str(output)) == {"FINISHED"}
    timeout = time.monotonic() + 600
    while addon.jobs.export_running():
        assert time.monotonic() < timeout, addon.jobs.export_status()
        addon.jobs.tick()
        time.sleep(.1)
    assert not addon.jobs.export_details(), addon.jobs.export_details()
    assert {p.stem for p in output.glob("*.exr")} == set(references), addon.jobs.export_status()

    results = {}
    for branch, (temperature, manual, mode) in references.items():
        reference = folder / (branch + "-reference.exr")
        # Muting passes the as-shot LibRaw Reader through. The active B branch
        # uses Apple RAW on this Mac, with its own pixel dimensions.
        raw_settings = {"preview": False, "white_balance": "CAMERA", "camera_input": "AUTO", "look": "NONE"}
        if branch == "B" and addon.catalog._APPLE_RAW_SUPPORTED:
            raw_settings["camera_input"] = "APPLE"
        if temperature is not None:
            raw_settings.update(white_balance="CUSTOM", temperature=temperature, tint=0.0)
        raw_backend.decode(source, reference, raw_settings)
        size, samples = sampled_rgb(reference)
        compensation = 0.0 if mode == "AS_SHOT" else metering.measure(samples)[mode]
        actual_size, actual = sampled_rgb(output / (branch + ".exr"))
        assert actual_size == size, (branch, actual_size, size)
        if source.name.upper() == "DSC_9065.NEF" and addon.catalog._APPLE_RAW_SUPPORTED:
            assert actual_size == ((6048, 4032) if branch == "B" else (6064, 4040))
        expected = samples * 2**(manual + compensation)
        np.testing.assert_allclose(actual, expected, rtol=2e-5, atol=2e-6, err_msg=branch)
        results[branch] = actual
        print(f"{branch}: {size}, auto {compensation:+.4f} EV, manual {manual:+.2f} EV, "
              f"max RGB error {np.max(np.abs(actual - expected)):.2g}", flush=True)
    assert not np.allclose(results["A"], results["B"])
    assert not np.allclose(results["Muted"], results["B"])
    addon.unregister()
    print("PASS: 3-photo batch, native Switch, unused missing DCP, automatic and manual EV, native mute", flush=True)


source = Path(sys.argv[sys.argv.index("--") + 1]).resolve()
with tempfile.TemporaryDirectory(prefix="photo-editor-developer-export-") as temporary:
    run(source, Path(temporary))
