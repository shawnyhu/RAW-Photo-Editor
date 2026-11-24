"""Exercise photo switching and missing RAW inputs with the installed GPU renderer.

blender -b --offline-mode --python-exit-code 1 --python tests/spektrafilm_switch.py
"""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from unittest.mock import patch

import bpy
import numpy as np
import OpenImageIO as oiio

from bl_ext.user_default.raw_photo_editor import catalog, compositor, film_backend, film_node, film_preview


def write(path, rgba):
    with closing(oiio.ImageOutput.create(str(path))) as out:
        assert out.open(str(path), oiio.ImageSpec(32, 24, 4, oiio.FLOAT))
        assert out.write_image(rgba)


def photo_graph(folder, name, level):
    photo = catalog.library(bpy.context).photos.add()
    photo.uid = photo.name = name
    path = folder / (name + '.exr')
    pixels = np.full((24, 32, 4), level, dtype=np.float32)
    pixels[..., 3] = 1
    write(path, pixels)
    image = bpy.data.images.load(str(path))
    image.colorspace_settings.name = 'Linear Rec.709'
    image.alpha_mode = 'PREMUL'
    image['photo_editor_decode_cache'] = name
    photo.image = image
    tree = compositor.ensure_tree(bpy.context, photo)
    developer = next(n for n in tree.nodes if n.bl_idname == compositor.DEVELOPER_TYPE)
    compositor.apply_developer_image(developer, photo, image)
    film = film_node.insert_after(developer)
    film.live_preview = True
    return photo, film, developer, path, pixels


def settle(node, expect='Ready'):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        film_preview._tick()
        if not film_preview._pending and not film_preview._job:
            assert expect in node.status, node.status
            return node.node_tree.nodes['Film Result'].image
        time.sleep(.05)
    raise TimeoutError(node.status)


with TemporaryDirectory() as tmp:
    folder = Path(tmp)
    a, first, developer, path, pixels = photo_graph(folder, 'A', .4)
    b, second, _, _, _ = photo_graph(folder, 'B', .75)
    with patch.object(catalog, 'cache_dir', return_value=folder), patch.object(
            film_preview, '_start', wraps=film_preview._start) as starts:
        compositor.open_photo(bpy.context, a)
        result_a = settle(first)
        compositor.open_photo(bpy.context, b)
        assert settle(second) != result_a
        compositor.open_photo(bpy.context, a)
        assert settle(first) == result_a and starts.call_count == 2, starts.call_count

        # A transparent Viewer contradicts this direct opaque scan. Retry the
        # native compositor at most twice; a user's downstream alpha edit wins.
        scene = bpy.context.scene
        compositor._viewer_retries[scene.session_uid] = (a.uid, None, 0)
        viewer = next(n for n in a.tree.nodes if n.type == 'VIEWER')
        source = viewer.inputs[0].links[0].from_socket
        with patch.object(compositor, '_schedule_viewer_check') as retry:
            compositor.check_viewer_pixels(scene, result_a)
            retry.assert_not_called()
            alpha = a.tree.nodes.new('CompositorNodeSetAlpha')
            a.tree.links.new(source, alpha.inputs['Image'])
            a.tree.links.new(alpha.outputs['Image'], viewer.inputs[0])
            compositor.check_viewer_pixels(scene, None, empty=True)
            retry.assert_not_called()
            a.tree.nodes.remove(alpha)
            a.tree.links.new(source, viewer.inputs[0])
            for _ in range(4):
                compositor.check_viewer_pixels(scene, None, empty=True)
            assert retry.call_count == 2

        # A managed RAW file can disappear even while the same image ID remains bound.
        path.unlink()
        assert settle(first, 'Waiting for RAW') == result_a and starts.call_count == 2
        write(path, np.zeros_like(pixels))
        assert settle(first, 'fully transparent') == result_a and starts.call_count == 3
        developer.exposure = 1
        write(path, pixels)
        fixed = settle(first)
        assert fixed != result_a and starts.call_count == 4

        # A fresh Blender data session has different runtime identities and the same files.
        fixed_key = fixed['photo_editor_decode_cache']
        blend = folder / 'gallery.blend'
        bpy.ops.wm.save_as_mainfile(filepath=str(blend))
        film_preview._stop()
        bpy.ops.wm.open_mainfile(filepath=str(blend))
        first = next(n for n in bpy.context.scene.compositor_effects[0].node_group.nodes
                     if n.bl_idname == film_node.FILM_TYPE)
        film_preview.live_preview_changed(first)  # The interactive load also queues node updates.
        fixed = settle(first)
        assert fixed.get('photo_editor_decode_cache') == fixed_key and starts.call_count == 4

        tree = first.id_data
        extra = tree.nodes.new('CompositorNodeExposure')
        developer = next(n for n in tree.nodes if n.bl_idname == compositor.DEVELOPER_TYPE)
        extra.inputs['Exposure'].default_value = -.5
        tree.links.new(developer.outputs['Image'], extra.inputs['Image'])
        tree.links.new(extra.outputs['Image'], first.inputs['Image'])
        film_preview.live_preview_changed(first)
        film_preview._tick()
        assert film_preview._job and starts.call_count == 5
        a = catalog.lookup_photo(bpy.context.scene, 'A')
        b = catalog.lookup_photo(bpy.context.scene, 'B')
        second = next(n for n in b.tree.nodes if n.bl_idname == film_node.FILM_TYPE)
        compositor.open_photo(bpy.context, b)
        settle(second)
        assert first.node_tree.nodes['Film Result'].image == fixed and starts.call_count == 5
        compositor.open_photo(bpy.context, a)
        fixed = settle(first)
        assert starts.call_count == 6
        compositor.open_photo(bpy.context, b)
        settle(second)
        compositor.open_photo(bpy.context, a)
        assert settle(first) == fixed and starts.call_count == 6

        # Paused scans with missing storage fall back to their live input.
        first.live_preview = False
        Path(bpy.path.abspath(fixed.filepath)).unlink()
        fixed.buffers_free()
        film_preview._tick()
        assert first.node_tree.nodes['Film Result'].image is None
        assert first.node_tree.nodes['Film Output'].inputs[0].links[0].from_node.name == 'Film Input'
        assert starts.call_count == 6

    # Artist-authored RGBA graphs can legitimately be fully transparent.
    write(path, np.zeros_like(pixels))
    output = folder / 'graph.exr'
    film_backend.process(path, output, film_node.recipe(first))
    with closing(oiio.ImageInput.open(str(output))) as reader:
        assert not np.any(reader.read_image(format=oiio.FLOAT))

print('PASS distinct photos, warm return, missing and repaired RAW, retained scan, paused fallback and transparent graphs')
