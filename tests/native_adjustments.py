"""Exercise native Scene Effects batch copies, independent caches and persistence.

blender -b --factory-startup --offline-mode --python-exit-code 1 -P tests/native_adjustments.py
Run without -b to also exercise Blender's real-window incremental Undo/Redo.
"""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
from unittest.mock import patch

import bpy
import numpy as np
import OpenImageIO as oiio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from blender_raw import adjustments, catalog, compositor, film_node, gallery, native_effects, spektrafilm_engine


def setup():
    catalog.register()
    compositor.register()
    native_effects.register()
    bpy.utils.register_class(gallery.BR_OT_copy_adjustments)
    bpy.utils.register_class(gallery.BR_OT_paste_adjustments)
    bpy.app.handlers.load_pre.append(adjustments.clear)


def test(folder):
    context = bpy.context
    owner = context.scene
    owner.blender_raw.owner = True
    library = catalog.library(context)

    def photo(name, rgb):
        record = library.photos.add()
        record.name = record.uid = name
        record.filepath = str(folder / (name + ".NEF"))
        image = bpy.data.images.new(name, width=32, height=32, float_buffer=True)
        image.pixels.foreach_set(np.tile(np.asarray([*rgb, 1.0], dtype="float32"), 32 * 32))
        image.pack()
        record.image = image
        return record

    source = photo("Source", (.7, .1, .2))
    first = photo("First", (.02, .04, .08))
    second = photo("Second", (.09, .03, .01))
    untouched = photo("Untouched", (.1, .2, .3))
    first.rating, first.flagged, first.tags = 4, True, "keep"
    destination = compositor.ensure_photo_scene(context, first)
    reader = native_effects.effect_node(destination.compositor_effects[0])
    reader.label = "My original"
    reader_group = reader.node_tree
    native_effects.set_input_value(destination.compositor_effects[1], "Exposure", -3.)
    old_group = destination.compositor_effects[1].node_group
    source_scene = compositor.ensure_photo_scene(context, source)
    developer_effect = source_scene.compositor_effects[1]
    developer = native_effects.effect_node(developer_effect)
    native_effects.set_input_value(developer_effect, "Exposure", 1.)
    compositor.apply_developer_image(developer, source, source.image)

    # A free-form branched compositor effect with interface parameters, curves,
    # an external image/Scene, a shared helper and an intentionally muted wire.
    tree = bpy.data.node_groups.new("Branched adjustment", "CompositorNodeTree")
    for name, direction, kind in (("Image", "INPUT", "NodeSocketColor"),
                                  ("EV", "INPUT", "NodeSocketFloat"),
                                  ("Tint", "INPUT", "NodeSocketColor"),
                                  ("Image", "OUTPUT", "NodeSocketColor")):
        tree.interface.new_socket(name=name, in_out=direction, socket_type=kind)
    group_input = tree.nodes.new("NodeGroupInput")
    output = tree.nodes.new("NodeGroupOutput")
    curves = tree.nodes.new("CompositorNodeCurveRGB")
    curves.mapping.curves[3].points.new(.45, .65)
    curves.mapping.update()
    curves.mute = True
    exposure = tree.nodes.new("CompositorNodeExposure")
    tree.links.new(group_input.outputs["Image"], curves.inputs["Image"])
    tree.links.new(curves.outputs["Image"], exposure.inputs["Image"])
    tree.links.new(group_input.outputs["EV"], exposure.inputs["Exposure"])
    tree.links.new(exposure.outputs["Image"], output.inputs["Image"])
    extra = tree.nodes.new("CompositorNodeExposure")
    tree.links.new(group_input.outputs["Image"], extra.inputs["Image"])
    value = tree.nodes.new("ShaderNodeValue")
    value.outputs[0].default_value = 7.
    tree.links.new(value.outputs[0], extra.inputs["Exposure"]).is_muted = True
    helper = bpy.data.node_groups.new("Auxiliary data", "CompositorNodeTree")
    auxiliary = helper.nodes.new("CompositorNodeImage")
    auxiliary.image = untouched.image
    layers = helper.nodes.new("CompositorNodeRLayers")
    layers.scene = owner
    group = tree.nodes.new("CompositorNodeGroup")
    group.node_tree = helper
    effect = source_scene.compositor_effects.new("Custom branch")
    effect.node_group = tree
    native_effects.set_input_value(effect, "EV", 1.)
    native_effects.set_input_value(effect, "Tint", (.12, .23, .34, 1.))
    effect.show_expanded = False
    film_effect = native_effects.create_effect(source_scene, film_node.FILM_TYPE, photo=source)
    film = native_effects.effect_node(film_effect)
    film.film, film.paper, film.live_preview = "kodak_portra_800", "kodak_2383", False
    film_node.apply_result(film, source.image)
    film_effect.enable_for_preview = False
    film_effect.enable_for_render = False
    source_scene.compositor_effects.active = effect
    initial_scenes = len(bpy.data.scenes)

    for record in (source, first, second):
        record.selected = True
    assert not adjustments.has_targets(library.photos)
    assert bpy.ops.blender_raw.copy_adjustments(uid=source.uid) == {"FINISHED"}
    native_effects.set_input_value(developer_effect, "Exposure", 3.)
    first["edited_thumbnail_path"] = "old.jpg"
    assert bpy.ops.blender_raw.paste_adjustments() == {"FINISHED"}
    assert len(bpy.data.scenes) == initial_scenes + 1
    assert untouched.scene is None
    assert first.scene == destination
    assert native_effects.effect_node(destination.compositor_effects[0]) == reader
    assert reader.node_tree == reader_group and reader.label == "My original"
    assert old_group in bpy.data.node_groups.values(), "Old groups must remain available for Undo"
    assert (first.rating, first.flagged, first.tags) == (4, True, "keep")
    assert "edited_thumbnail_path" not in first
    assert native_effects.get_input_value(developer_effect, "Exposure") == 3.
    assert film.node_tree.nodes["Film Result"].image == source.image
    instance_ids = {developer.instance_uid}
    for record in (first, second):
        effects = list(record.scene.compositor_effects)
        assert len(effects) == 4
        assert [item.name for item in effects] == ["Raw Reader", "Raw Developer", "Custom branch", "Spektrafilm Lite"]
        copied_developer = native_effects.effect_node(effects[1])
        copied_film = native_effects.effect_node(effects[3])
        assert native_effects.get_input_value(effects[1], "Exposure") == 1.
        assert copied_developer.photo_uid == record.uid
        assert compositor.developer_source(copied_developer) == record
        assert copied_developer.node_tree.nodes["Developed RAW"].image is None
        instance_ids.add(copied_developer.instance_uid)
        assert not copied_film.node_tree.nodes["Film Result"].image
        assert copied_film.film == film.film and copied_film.paper == film.paper
        assert not copied_film.live_preview and not effects[3].enable_for_preview and not effects[3].enable_for_render
        assert not effects[2].show_expanded
        assert native_effects.get_input_value(effects[2], "EV") == 1.
        np.testing.assert_allclose(native_effects.get_input_value(effects[2], "Tint"), (.12, .23, .34, 1.))
        copied_tree = effects[2].node_group
        assert len(copied_tree.nodes[curves.name].mapping.curves[3].points) == 3
        assert copied_tree.nodes[extra.name].inputs["Exposure"].links[0].is_muted
        copied_helper = copied_tree.nodes[group.name].node_tree
        assert copied_helper != helper
        assert copied_helper.nodes[auxiliary.name].image == untouched.image
        assert copied_helper.nodes[layers.name].scene == owner
        assert record.scene.compositor_effects.active == effects[2]
    assert len(instance_ids) == 3
    assert first.scene.compositor_effects[1].node_group != second.scene.compositor_effects[1].node_group
    assert first.scene.compositor_effects[3].node_group != second.scene.compositor_effects[3].node_group

    # Use Blender's actual native stack evaluator and two different originals.
    for record in (first, second):
        assert record.scene.camera is None
        scene = compositor.export_scene(record.scene, "OPEN_EXR")
        scene.render.compositor_device = "CPU"
        scene.render.compositor_precision = "FULL"
        scene.render.resolution_x = scene.render.resolution_y = 32
        scene.render.image_settings.file_format = "OPEN_EXR"
        scene.render.image_settings.color_depth = "32"
        path = str(folder / (record.name + ".exr"))
        assert compositor.export_photo(context, record, path, scene) == path
        with closing(oiio.ImageInput.open(path)) as image:
            pixels = image.read_image(format=oiio.FLOAT)
        np.testing.assert_allclose(pixels[15, 15, :3], np.asarray(record.image.pixels[:3]) * 4, atol=1e-5)
        bpy.data.scenes.remove(scene)
        assert record.scene.camera is None

    native_effects.set_input_value(first.scene.compositor_effects[1], "Exposure", -2.)
    assert native_effects.get_input_value(second.scene.compositor_effects[1], "Exposure") == 1.
    assert adjustments.paste(context, (first, first)) == 1
    assert native_effects.get_input_value(first.scene.compositor_effects[1], "Exposure") == 1.

    # A bad destination is rejected before replacing any valid destination.
    foreign_reader = native_effects.create_effect(second.scene, compositor.READER_TYPE, photo=second)
    native_effects.set_input_value(first.scene.compositor_effects[1], "Exposure", -1.)
    try:
        adjustments.paste(context, (first, second))
        raise AssertionError("A second Raw Reader must fail validation")
    except ValueError:
        pass
    assert native_effects.get_input_value(first.scene.compositor_effects[1], "Exposure") == -1.
    second.scene.compositor_effects.remove(foreign_reader)

    if not bpy.app.background:
        bpy.context.preferences.edit.use_global_undo = True
        assert bpy.ops.ed.undo_push(message="Before native batch paste") == {"FINISHED"}
        assert bpy.ops.blender_raw.paste_adjustments('EXEC_DEFAULT', True) == {"FINISHED"}
        assert bpy.ops.ed.undo() == {"FINISHED"}
        restored = catalog.lookup_photo(bpy.context.scene, "First")
        assert native_effects.get_input_value(restored.scene.compositor_effects[1], "Exposure") == -1.
        assert bpy.ops.ed.redo() == {"FINISHED"}
        restored = catalog.lookup_photo(bpy.context.scene, "First")
        assert native_effects.get_input_value(restored.scene.compositor_effects[1], "Exposure") == 1.

    project = str(folder / "native-batch.blend")
    assert bpy.ops.wm.save_as_mainfile(filepath=project) == {"FINISHED"}
    assert bpy.ops.wm.open_mainfile(filepath=project) == {"FINISHED"}
    for record in catalog.library(bpy.context).photos[:3]:
        own_reader = native_effects.effect_node(record.scene.compositor_effects[0])
        assert own_reader.photo_uid == record.uid
        assert own_reader.node_tree.nodes["Decoded Image"].image == record.image
        assert len(record.scene.compositor_effects) == 4
    assert not adjustments.has_targets(catalog.library(bpy.context).photos)
    print("PASS native effect clipboard: branches, instance values, independent RAW/Film caches, batch validation, pixels and reload", flush=True)


with patch.object(spektrafilm_engine, 'backend_error', return_value=''):
    # This structural/paused-cache test never invokes the film backend, but
    # node creation normally polls GPU availability before building the group.
    setup()
    with TemporaryDirectory(prefix="bpe-native-adjustments-") as directory:
        test(Path(directory))
if not bpy.app.background:
    bpy.ops.wm.quit_blender()
