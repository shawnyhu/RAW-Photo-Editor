"""Installed Mac extension, Portra 800 / 2383 reference export.

Pass the original NEF, standalone .sfpreset, reference sRGB TIFF and an empty
output directory after --, optionally followed by the standalone quality-100
JPEG. Uses the same operators and isolated worker as the UI.
"""
from contextlib import closing
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import time

import bpy
import numpy as np
import OpenImageIO as oiio

from bl_ext.user_default.raw_photo_editor import catalog, compositor, film_node, jobs, worker


# Measured on the original DSC_9065 using the standalone preset's Median meter.
# The reference TIFF includes this exposure. Preset import leaves our meter alone.
REFERENCE_EV = 1.4772372246


def verify_export(actual, reference, *, jpeg=False):
    # Difference histogram over full-resolution RGB. Alpha in the reference is opaque.
    histogram = np.zeros(65536, dtype=np.int64)
    with closing(oiio.ImageInput.open(str(actual))) as out, closing(oiio.ImageInput.open(str(reference))) as ref:
        width, height = ref.spec().width, ref.spec().height
        assert (out.spec().width, out.spec().height) == (width, height)
        storage = oiio.UINT8 if jpeg else oiio.UINT16
        assert out.spec().format == storage and ref.spec().format == storage
        assert ref.spec().nchannels >= 3 and out.spec().nchannels >= 3
        profile = out.spec().getattribute("ICCProfile")
        assert profile is not None and len(profile) > 128
        if jpeg:
            assert out.spec().get_string_attribute("jpeg:subsampling") == "4:4:4"
        for y in range(0, height, 64):
            args = (0, 0, y, min(y + 64, height), 0, 0, 3, oiio.UINT16)
            diff = np.abs(out.read_scanlines(*args).astype(np.int32) - ref.read_scanlines(*args))
            histogram += np.bincount(diff.ravel(), minlength=len(histogram))
    count = histogram.sum()
    avg = histogram @ np.arange(len(histogram), dtype=np.float64) / count / 257
    p99 = np.searchsorted(histogram.cumsum(), 0.99 * count) / 257
    print(f"{actual.suffix} {width} x {height}; mean={avg:.5f}, P99={p99:.5f}, "
          f"max={np.flatnonzero(histogram)[-1]/257:.3f} steps / 255; "
          f"fraction >1 step={histogram[258:].sum()/count:.6%}", flush=True)
    if jpeg:
        assert avg < 1 and p99 <= 4
    else:
        assert avg < 0.2 and p99 < 1.0


def verify_preview(photo, developer):
    for recipe in (None, developer):
        request = catalog.request(photo, "preview", recipe=recipe)
        if not Path(request["output"]).is_file():
            worker.process_image(request)
        if recipe is None:
            catalog.apply_image(photo, request)
        else:
            image = catalog.apply_developer_image(photo, developer, request)
            assert image.get("photo_editor_profile")
    exposure = developer.node_tree.nodes["Exposure"].inputs["Exposure"].default_value
    print(f"Developer preview {tuple(image.size)}, {developer.auto_exposure} {exposure:+.5f} EV", flush=True)
    assert developer.auto_exposure == "AS_SHOT" and abs(exposure - REFERENCE_EV) < 1e-6


def export(output):
    assert bpy.ops.blender_raw.export_photos(directory=str(output)) == {"FINISHED"}
    deadline = time.monotonic() + 600
    while jobs.export_running() and time.monotonic() < deadline:
        jobs.tick()
        time.sleep(.2)
    assert not jobs.export_running() and not jobs.export_details(), (jobs.export_status(), jobs.export_details())


source, preset, ref, output, *jpeg_reference = (Path(p).resolve() for p in sys.argv[sys.argv.index('--') + 1:])
output.mkdir(parents=True, exist_ok=True)
assert not any(output.iterdir()), "Choose an empty output directory"
photo, _ = catalog.add_photo(bpy.context, source)
photo.selected = True
tree = compositor.open_photo(bpy.context, photo)
dev = next(n for n in tree.nodes if n.bl_idname == compositor.DEVELOPER_TYPE)
assert photo.camera_input == dev.camera_input == "AUTO" and dev.auto_exposure == "AS_SHOT"
assert catalog.request(photo, "preview", dev)["key"] == catalog.request(photo, "preview")["key"]
dev.auto_exposure = "GLOBAL"
assert bpy.ops.blender_raw.add_spektrafilm(tree_name=tree.name, node_name=dev.name) == {"FINISHED"}
film = tree.nodes.active
with TemporaryDirectory() as tmp:
    local_preset = Path(tmp) / preset.name
    shutil.copyfile(preset, local_preset)
    for decoder in ("AUTO", "APPLE"):
        # The user chooses the decoder. The standalone comparison uses Apple RAW.
        dev.camera_input = decoder
        film_node.load_preset(film, local_preset)
        assert (dev.camera_input, film.film, film.paper) == (decoder, "kodak_portra_800", "kodak_2383")
        assert dev.auto_exposure == "GLOBAL" and dev.exposure == 0
    dev.auto_exposure, dev.exposure = "AS_SHOT", REFERENCE_EV
    assert film.scannerEnabled and film.cameraDiffusionEnabled and film.grain and film.grain_model == "Production"
    verify_preview(photo, dev)
    scene = bpy.context.scene
    assert scene.view_settings.view_transform == "Standard" and scene.display_settings.display_device == "sRGB"
    fmt = scene.render.image_settings
    fmt.file_format, fmt.color_mode, fmt.color_depth, fmt.tiff_codec = "TIFF", "RGB", "16", "DEFLATE"
    assert fmt.color_management == "FOLLOW_SCENE"
    blend = str(Path(tmp) / 'roundtrip.blend')
    uid = photo.uid
    assert bpy.ops.wm.save_as_mainfile(filepath=blend) == {"FINISHED"}
    local_preset.unlink()
    assert bpy.ops.wm.open_mainfile(filepath=blend) == {"FINISHED"}
    tree = catalog.lookup_photo(bpy.context.scene, uid).tree
    dev = next(n for n in tree.nodes if n.bl_idname == compositor.DEVELOPER_TYPE)
    assert dev.camera_input == "APPLE" and dev.auto_exposure == "AS_SHOT" and abs(dev.exposure - REFERENCE_EV) < 1e-6
    export(output)
    actual, = output.iterdir()
    verify_export(actual, ref)
    if jpeg_reference:
        fmt = bpy.context.scene.render.image_settings
        fmt.file_format, fmt.quality = "JPEG", 100
        export(output)
        actual, = output.glob('*.jpg')
        verify_export(actual, jpeg_reference[0], jpeg=True)
