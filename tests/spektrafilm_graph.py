"""Run the installed film node through Blender's compositor (or use the source tree).

blender -b --factory-startup --python-exit-code 1 --python tests/spektrafilm_graph.py
Use -- --graph-only to exercise the node and its controls without a GPU backend.
"""
from contextlib import closing, nullcontext
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

import bpy
import numpy as np
import OpenImageIO as oiio

installed = "bl_ext.user_default.raw_photo_editor" in bpy.context.preferences.addons
if installed:
    from bl_ext.user_default.raw_photo_editor import catalog, compositor, film_bake, film_effects, film_node, spektrafilm_engine
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from blender_raw import catalog, compositor, film_bake, film_effects, film_node, spektrafilm_engine


def write_image(path, pixels):
    height, width, channels = pixels.shape
    spec = oiio.ImageSpec(width, height, channels, oiio.FLOAT)
    spec.attribute("oiio:ColorSpace", "Linear Rec.709")
    with closing(oiio.ImageOutput.create(str(path))) as writer:
        assert writer.open(str(path), spec) and writer.write_image(pixels), writer.geterror()
    image = bpy.data.images.load(str(path))
    image.colorspace_settings.name = "Linear Rec.709"
    return image


def read_image(path):
    with closing(oiio.ImageInput.open(str(path))) as reader:
        return reader.read_image(format=oiio.FLOAT)


def render(scene, path):
    scene.render.filepath = str(path)
    assert "FINISHED" in bpy.ops.render.render(write_still=True, scene=scene.name)
    return read_image(path)


def check_paused_chain(scene, tree, lead, folder):
    inner = bpy.data.node_groups.new('Nested film', 'CompositorNodeTree')
    for direction in ('INPUT', 'OUTPUT'):
        inner.interface.new_socket(name='Image', in_out=direction, socket_type='NodeSocketColor')
    hold = inner.nodes.new(film_node.FILM_TYPE)
    inner.links.new(inner.nodes.new('NodeGroupInput').outputs[0], hold.inputs[0])
    inner.links.new(hold.outputs[0], inner.nodes.new('NodeGroupOutput').inputs[0])
    group = tree.nodes.new('CompositorNodeGroup')
    group.node_tree = inner
    tail = tree.nodes.new(film_node.FILM_TYPE)
    lead.live_preview = tail.live_preview = True
    tree.links.new(lead.outputs[0], group.inputs[0])
    tree.links.new(group.outputs[0], tail.inputs[0])
    tree.links.new(tail.outputs[0], film_bake.group_output(tree).inputs[0])
    output = folder / 'chain.exr'

    # With no scan, the paused film passes through the active upstream node.
    with patch.object(film_bake, '_process', wraps=film_bake._process) as process:
        film_bake.preview(scene, tree, tail, output)
        assert [c.args[2] for c in process.call_args_list] == [lead, tail]

    held_rgb = np.full((48, 64, 3), (0.1, 0.25, 0.4), dtype=np.float32)
    held_image = write_image(folder / 'held.exr', held_rgb)
    film_node.apply_result(hold, held_image)
    hold.scannerEnabled = hold.scannerWhiteCorrection = True
    hold.scannerWhiteLevel = .75
    with patch.object(film_bake, '_process', wraps=film_bake._process) as process:
        film_bake.preview(scene, tree, tail, output)
        assert [c.args[2] for c in process.call_args_list] == [tail]
    expected = spektrafilm_engine.render(held_rgb, **film_node.recipe(tail), frame=scene.frame_current)
    np.testing.assert_allclose(read_image(output)[:, :, :3], expected, rtol=8e-4, atol=2e-4)

    with patch.object(film_bake, '_process', wraps=film_bake._process) as process:
        with film_bake.prepared(scene, tree, folder):
            exported = render(scene, folder / 'chain-final.exr')
        assert [c.args[2] for c in process.call_args_list] == [lead, hold, tail]
    assert np.max(np.abs(exported[:, :, :3] - expected)) > .01
    assert hold.node_tree.nodes['Film Result'].image == held_image
    print('PASS paused upstream film holds its scan and final export evaluates the full nested chain', flush=True)


def run(folder, graph_only):
    scene = bpy.context.scene
    scene.render.resolution_x, scene.render.resolution_y = 64, 48
    scene.render.resolution_percentage = 100
    scene.render.compositor_device = "CPU"
    scene.render.compositor_precision = "FULL"
    scene.render.image_settings.file_format = "OPEN_EXR"
    scene.render.image_settings.color_depth = "32"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.use_sequencer = False
    scene.render.use_compositing = True
    scene.compositor_effects.clear()
    x = np.linspace(0.05, 1.5, 64, dtype=np.float32)
    rgb = np.broadcast_to(np.stack((x**2, x * 0.4, x[::-1] * 0.2), axis=-1), (48, 64, 3)).copy()
    alpha = np.ones((48, 64, 1), dtype=np.float32)
    alpha[:, :12], alpha[:, 12:24] = 0.0, 0.5
    rgba = np.concatenate((rgb * alpha, alpha), axis=2)
    source = write_image(folder / "input.exr", rgba)

    photo = scene.blender_raw.photos.add()
    photo.name, photo.uid, photo.image = "Film test", "film-test", source
    tree = compositor.ensure_tree(bpy.context, photo)
    scene.compositor_effects.new("Photo").node_group = tree
    assert not any(n.bl_idname == film_node.FILM_TYPE for n in tree.nodes)
    dev = next(n for n in tree.nodes if n.bl_idname == compositor.DEVELOPER_TYPE)
    junction = next(n for n in tree.nodes if n.type == "REROUTE")
    extra = tree.nodes.new("CompositorNodeExposure")
    tree.links.new(dev.outputs[0], extra.inputs[0]).is_muted = True
    for _ in range(2):
        assert bpy.ops.blender_raw.add_spektrafilm(tree_name=tree.name, node_name=dev.name) == {"FINISHED"}
    films = [n for n in tree.nodes if n.bl_idname == film_node.FILM_TYPE]
    assert len(films) == 1
    node = films[0]
    assert node == tree.nodes.active and not node.live_preview
    default_effects = film_node.recipe(node)["effects"]
    for key, effect in film_effects.BY_KEY.items():
        assert not node.is_property_set(key)
        np.testing.assert_allclose(default_effects[key], effect.default)
    assert node.inputs[0].links[0].from_node == dev
    assert junction.inputs[0].links[0].from_node == node
    assert extra.inputs[0].links[0].from_node == node and extra.inputs[0].links[0].is_muted
    assert {link.to_node.type for link in junction.outputs[0].links} == {"GROUP_OUTPUT", "VIEWER"}

    # The same node can be connected to an ordinary Blender image and arbitrary grading nodes.
    image_node = tree.nodes.new("CompositorNodeImage")
    image_node.image = source
    ev = tree.nodes.new("CompositorNodeExposure")
    ev.inputs["Exposure"].default_value = 1
    tree.links.new(image_node.outputs["Image"], ev.inputs["Image"])
    tree.links.new(ev.outputs["Image"], node.inputs["Image"])
    seed_image = write_image(folder / "previous-preview.exr", np.full_like(rgb, 0.15))
    film_node.apply_result(node, seed_image)
    np.testing.assert_allclose(render(scene, folder / "cache.exr")[:, :, :3], 0.15, atol=3e-6)
    node.mute = True
    bypass = render(scene, folder / "bypass.exr")
    np.testing.assert_allclose(bypass[:, :, :3], rgba[:, :, :3] * 2, atol=3e-6)
    np.testing.assert_array_equal(bypass[:, :, 3:], alpha)
    node.mute = False
    window = bpy.context.window
    area = max(window.screen.areas, key=lambda item: item.width * item.height)
    area.ui_type = "CompositorNodeTree"
    region = next(region for region in area.regions if region.type == "WINDOW")
    with bpy.context.temp_override(window=window, area=area, region=region):
        space = area.spaces.active
        space.pin = True
        space.path.start(tree)
        for item in tree.nodes:
            item.select = item == node
        tree.nodes.active = node
        assert bpy.ops.node.duplicate() == {"FINISHED"}
    duplicate = tree.nodes.active
    assert duplicate.instance_uid != node.instance_uid and duplicate.node_tree != node.node_tree
    duplicate.scannerEnabled = True
    duplicate.printDiffusionFamily = "Pro-Mist"
    film_node.invalidate(duplicate)
    assert not node.scannerEnabled and node.node_tree.nodes["Film Result"].image == seed_image
    assert node.printDiffusionFamily == "Black Pro-Mist"
    tree.nodes.remove(duplicate)

    node.print_exposure = 0.25
    assert node.print_exposure == 0.0
    node.cameraDiffusionEnabled = True
    node.cameraDiffusionFamily = "CineBloom"
    node.cameraDiffusionStrength = 1.2
    node.printDiffusionEnabled = True
    node.printDiffusionFamily = "Glimmerglass"
    node.printDiffusionHaloWarmth = -0.5
    node.scannerEnabled = True
    node.scannerWhiteCorrection = True
    node.scannerWhiteLevel = 0.87
    persisted = film_node.recipe(node)
    node.live_preview = True
    assert film_node.recipe(node) == persisted
    assert persisted["effects"]["cameraDiffusionFamily"] == 3
    assert persisted["effects"]["printDiffusionFamily"] == 0
    node_name, tree_name, uid = node.name, tree.name, node.instance_uid
    project = str(folder / "roundtrip.blend")
    assert "FINISHED" in bpy.ops.wm.save_as_mainfile(filepath=project)
    assert "FINISHED" in bpy.ops.wm.open_mainfile(filepath=project, load_ui=False)
    scene = bpy.context.scene
    tree = bpy.data.node_groups[tree_name]
    node = tree.nodes[node_name]
    assert node.instance_uid == uid and node.print_exposure == 0.0 and node.live_preview
    assert film_node.recipe(node) == persisted
    assert node.inputs[0].links[0].from_node.bl_idname == "CompositorNodeExposure"
    print("PASS Spektrafilm insertion, branches, native mute, copy, manual wiring and project round-trip",
          flush=True)
    if graph_only:
        return

    straight = np.where(alpha > 0, rgb * 2, 0.0)
    direct = spektrafilm_engine.render(straight, **film_node.recipe(node), frame=scene.frame_current)
    neutral = spektrafilm_engine.render(straight, frame=scene.frame_current)
    assert np.max(np.abs(direct - neutral)) > 0.01
    expected = np.concatenate((direct * alpha, alpha), axis=2)
    path = folder / "preview.exr"
    film_bake.preview(scene, tree, node, path)
    np.testing.assert_allclose(read_image(path), expected, rtol=8e-4, atol=2e-4)
    # A stale saved preview must never leak into the full-resolution export.
    with film_bake.prepared(scene, tree, folder):
        final = render(scene, folder / "final.exr")
    np.testing.assert_allclose(final, expected, rtol=8e-4, atol=2e-4)
    assert np.max(np.abs(final - bypass)) > 0.01
    print("PASS preview and full-resolution graph output match the native film engine", flush=True)
    check_paused_chain(scene, tree, node, folder)


graph_only = "--graph-only" in sys.argv
if not graph_only and (error := spektrafilm_engine.backend_error()):
    raise RuntimeError(error)
if not installed:
    catalog.register()
    compositor.register()
try:
    with tempfile.TemporaryDirectory(prefix="photo-editor-film-test-") as temporary, (
        patch.object(spektrafilm_engine, "backend_error", return_value="") if graph_only else nullcontext()
    ):
        run(Path(temporary), graph_only)
finally:
    if not installed:
        compositor.unregister()
        catalog.unregister()
