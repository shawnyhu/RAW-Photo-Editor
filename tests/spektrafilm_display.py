"""Check the darkroom's sRGB display and export independently of film-node connections."""
from contextlib import closing
from pathlib import Path
import sys
import tempfile

import bpy
import numpy as np
import OpenImageIO as oiio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from blender_raw import catalog, compositor, film_display, film_node


def check_sdr(scene):
    assert scene.display_settings.display_device == "sRGB" and scene.display_settings.emulation == "AUTO"
    grade = scene.view_settings
    assert (grade.view_transform, grade.look, grade.exposure, grade.gamma) == ("Standard", "None", 0, 1)
    assert not grade.use_curve_mapping and not grade.use_white_balance and not grade.is_hdr


def run(folder):
    regular = bpy.context.scene
    regular.view_settings.exposure = 1.5
    studio = compositor._studio_scene()
    check_sdr(studio)
    studio.display_settings.display_device = "Display P3"
    grade = studio.view_settings
    grade.view_transform = "AgX"
    grade.exposure, grade.gamma = 2, 1.5
    grade.use_white_balance = grade.use_curve_mapping = True
    studio["photo_editor_film_previous_view"] = "AgX"
    studio["photo_editor_auto_film_view"] = False
    compositor.restore_preview_devices()
    check_sdr(studio)
    assert "photo_editor_film_previous_view" not in studio and "photo_editor_auto_film_view" not in studio

    photo = regular.blender_raw.photos.add()
    photo.name, photo.uid = "Scan", "test-film-view"
    source = bpy.data.images.new("Linear scan", width=32, height=32, float_buffer=True)
    source.pixels.foreach_set(np.tile([0.18, 0.50, 0.825, 1.0], 32 * 32).astype(np.float32))
    source.update()
    photo.image = source
    compositor._set_photo(bpy.context, studio, photo)
    check_sdr(studio)
    tree = photo.tree
    dev = next(n for n in tree.nodes if n.bl_idname == compositor.DEVELOPER_TYPE)
    viewer = next(n for n in tree.nodes if n.type == "VIEWER")
    film = film_node.insert_after(dev)
    film_display.sync(studio)
    check_sdr(studio)
    film_node.apply_result(film, source)
    film_display.sync(studio)
    check_sdr(studio)

    # The output must apply the sRGB transfer exactly once.
    settings = studio.render.image_settings
    settings.file_format, settings.color_depth = "PNG", "16"
    assert settings.color_management == "FOLLOW_SCENE"
    output = str(folder / "display.png")
    source.save_render(output, scene=studio)
    with closing(oiio.ImageInput.open(output)) as reader:
        pixel = reader.read_image(format=oiio.FLOAT)[0, 0, :3]
    linear = np.array([0.18, 0.50, 0.825])
    np.testing.assert_allclose(pixel, 1.055 * linear ** (1 / 2.4) - .055, atol=5e-4)

    tree.links.new(dev.outputs["Image"], viewer.inputs["Image"])
    film.mute = True
    film_display.sync(studio)
    check_sdr(studio)
    settings.color_management = "OVERRIDE"
    batch = compositor.export_scene(studio)
    compositor._set_photo(bpy.context, batch, photo, for_export=True)
    check_sdr(batch)
    second = regular.blender_raw.photos.add()
    second.name, second.uid, second.image = "Unprocessed", "second-photo", source
    compositor._set_photo(bpy.context, batch, second, for_export=True)
    check_sdr(batch)
    assert batch.render.image_settings.color_management == "OVERRIDE"
    film_display.sync(regular)
    assert regular.view_settings.view_transform == "AgX" and regular.view_settings.exposure == 1.5
    print("PASS darkroom SDR, sRGB pixels, saved view migration, bypass, mixed export and explicit output overrides")


catalog.register()
compositor.register()
try:
    with tempfile.TemporaryDirectory(prefix="film-view-") as folder:
        run(Path(folder))
finally:
    compositor.unregister()
    catalog.unregister()
