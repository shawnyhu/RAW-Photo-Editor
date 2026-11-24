"""Independent OFX recipes, native duplication, presets and .blend persistence.

blender -b --factory-startup --offline-mode --python-exit-code 1 --python tests/ofx_node.py
This test does not load or render the third-party OFX binary.
"""
import json
import math
import os
from pathlib import Path
import sys
import tempfile

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from blender_raw import adjustments, catalog, compositor, film_node, native_effects, ofx_node, ofx_preset, ofx_schema


def set_parameter(node, name, value):
    setattr(node, ofx_node.parameter_property(name), value)


def assert_values(actual, expected):
    for key, value in expected.items():
        got = actual[key]
        if isinstance(value, (tuple, list)):
            assert len(got) == len(value), key
            assert all(abs(a - b) <= max(1, abs(b)) * 2e-6 for a, b in zip(got, value)), (key, got, value)
        elif isinstance(value, float):
            assert abs(got - value) <= max(1, abs(value)) * 2e-6, (key, got, value)
        else:
            assert got == value, (key, got, value)


def batch_copy(folder):
    """Copy through the real .blend adjustment clipboard, preserving sparse RNA."""
    bpy.context.scene.blender_raw.owner = True
    library = catalog.library(bpy.context)
    photos = []
    for name in ("OFX Copy Source", "OFX Copy First", "OFX Copy Second"):
        photo = library.photos.add()
        photo.name = photo.uid = name
        photo.filepath = str(folder / (name + ".NEF"))
        photo.image = bpy.data.images.new(name, width=16, height=16, float_buffer=True)
        photo.image.pack()
        compositor.ensure_photo_scene(bpy.context, photo)
        photos.append(photo)
    source = photos[0]
    effect = native_effects.create_effect(source.scene, ofx_node.FILM_TYPE, photo=source)
    node = native_effects.effect_node(effect)
    set_parameter(node, "filmExposureEv", 1.75)
    original = film_node.recipe(node)["parameters"]
    film_node.apply_result(node, source.image)
    adjustments.copy(bpy.context, source)
    try:
        assert adjustments.paste(bpy.context, photos[1:]) == 2
        copies = [native_effects.effect_node(photo.scene.compositor_effects[-1]) for photo in photos[1:]]
        for copied in copies:
            assert copied.bl_idname == ofx_node.FILM_TYPE
            assert film_node.recipe(copied)["parameters"] == original
            assert copied.node_tree != node.node_tree and copied.instance_uid != node.instance_uid
            assert copied.node_tree.nodes["Film Result"].image is None
        assert copies[0].node_tree != copies[1].node_tree
        assert copies[0].instance_uid != copies[1].instance_uid
        set_parameter(copies[0], "filmExposureEv", -1.0)
        assert film_node.recipe(copies[1])["parameters"] == original
        assert film_node.recipe(node)["parameters"] == original
        assert node.node_tree.nodes["Film Result"].image == source.image
    finally:
        adjustments.clear()


def run(folder):
    catalog.register()
    compositor.register()
    native_effects.register()
    scene = bpy.context.scene
    lite_effect = native_effects.create_effect(scene, film_node.FILM_TYPE)
    lite = native_effects.effect_node(lite_effect)
    lite_before = film_node.recipe(lite)
    effect = native_effects.create_effect(scene, ofx_node.FILM_TYPE)
    node = native_effects.effect_node(effect)
    assert node.bl_label == "Spektrafilm OFX"
    assert node.bl_idname != lite.bl_idname
    assert node.node_tree != lite.node_tree
    assert len(ofx_node.parameters(node)) == len(ofx_schema.PARAMETERS)
    assert_values(ofx_node.parameters(node), {p.key: p.default for p in ofx_schema.PARAMETERS})
    assert film_node.recipe(node)["engine"] == "ofx"
    assert isinstance(film_node.recipe(node)["plugin_path"], str)
    assert all(key.endswith("Seed") for key in film_node.recipe(node)["parameters"])

    # Native categories change the valid local stock indices and menu labels.
    for stock, category in (("film", "filmCategory"), ("paper", "printCategory")):
        dependency = ofx_schema.CHOICE_DEPENDENCIES[stock]
        for index, choices in dependency["values"].items():
            set_parameter(node, category, index)
            assert ofx_node.parameters(node)[stock] == choices["default"]
            selected = len(choices["options"]) - 1
            set_parameter(node, stock, str(selected))
            assert film_node.recipe(node)["parameters"][stock] == selected
    ofx_node.reset_recipe(node)
    set_parameter(node, "hdrPreset", "1")
    assert ofx_node.parameters(node)["hdrPeakNits"] == 4000
    set_parameter(node, "hdrPeakNits", 2000)
    assert ofx_node.parameters(node)["hdrPreset"] == 3
    ofx_node.reset_recipe(node)

    set_parameter(node, "filmExposureEv", 1.25)
    set_parameter(node, "printExposureEv", -0.75)
    recipe = film_node.recipe(node)
    assert recipe["parameters"]["filmExposureEv"] == 1.25
    assert recipe["parameters"]["printExposureEv"] == -0.75
    assert film_node.recipe(lite) == lite_before

    bpy.ops.scene.duplicate_compositor_effect(name=effect.name)
    native_effects.ensure_independent_effects()
    duplicate = scene.compositor_effects[-1]
    copied = native_effects.effect_node(duplicate)
    assert duplicate.node_group != effect.node_group
    assert copied.node_tree != node.node_tree
    assert copied.instance_uid != node.instance_uid
    assert film_node.recipe(copied)["parameters"] == recipe["parameters"], "Duplication made native defaults explicit"
    set_parameter(copied, "filmExposureEv", -2.0)
    assert ofx_node.parameters(node)["filmExposureEv"] == 1.25

    original_values = ofx_node.parameters(node)
    preset = folder / "Test OFX.spkpreset"
    ofx_preset.write(preset, "Round trip 测试", original_values)
    name, values, notices = ofx_preset.read(preset)
    assert name == "Round trip 测试" and not notices
    assert_values(values, original_values)
    ofx_node.reset_recipe(node)
    assert ofx_node.parameters(node)["filmExposureEv"] == ofx_schema.BY_KEY["filmExposureEv"].default
    ofx_node.load_preset(node, preset)
    assert node.preset_name == name

    # Optional installed-plugin check resolves real bundled-look callbacks and
    # writes portable snapshots containing the resulting native values.
    plugin_path = os.environ.get("BLENDER_PHOTO_SPEKTRAFILM_OFX")
    if plugin_path:
        from blender_raw import spektrafilm_ofx_engine as engine
        ofx_node.reset_recipe(copied)
        set_parameter(copied, "filmCategory", "0")
        set_parameter(copied, "film", "0")
        assert bpy.ops.blender_raw.resolve_spektrafilm_ofx_values(
            tree_name=copied.id_data.name, node_name=copied.name, group_key="dirGammaGroup") == {"FINISHED"}
        assert ofx_node.parameters(copied)["dirGammaSameLayerRgb"][0] > 0.5
        set_parameter(copied, "film", "1")
        assert not copied.is_property_set(ofx_node.parameter_property("dirGammaSameLayerRgb"))
        for category, index in ((0, 0), (1, 0)):
            assert bpy.ops.blender_raw.builtin_spektrafilm_ofx_preset(
                tree_name=copied.id_data.name, node_name=copied.name, preset=f"{category}:{index}") == {"FINISHED"}
            assert copied.preset_name
            effective = engine.effective_parameters(ofx_node.parameters(copied, explicit_only=True),
                                                    plugin_path=plugin_path)
            native_path = folder / f"native-{category}-{index}.spkpreset"
            ofx_preset.write(native_path, "Official look", effective)
            _title, restored_values, _notices = ofx_preset.read(native_path)
            assert_values(restored_values, effective)
        cube = folder / "Native OFX.cube"
        assert bpy.ops.blender_raw.export_spektrafilm_ofx_lut(
            tree_name=node.id_data.name, node_name=node.name, filepath=str(cube), lut_size="33") == {"FINISHED"}
        lines = cube.read_text(encoding="utf-8-sig").splitlines()
        assert "LUT_3D_SIZE 33" in lines
        samples = []
        for line in lines:
            fields = line.split()
            if len(fields) != 3:
                continue
            try:
                rgb = tuple(float(value) for value in fields)
            except ValueError:
                continue
            assert all(math.isfinite(value) for value in rgb)
            samples.append(rgb)
        assert len(samples) == 33 ** 3
        try:
            engine.export_lut(cube, {}, size=33, plugin_path=plugin_path)
        except FileExistsError:
            pass
        else:
            raise AssertionError("CUBE export replaced an existing file without overwrite")
        print("OFX official bundled looks and effective native preset snapshots passed")
        print("OFX native CUBE operator passed: 33^3 finite samples")
    assert_values(ofx_node.parameters(node), original_values)

    # A malformed native snapshot must fail before altering any node field.
    document = json.loads(preset.read_text(encoding="utf-8"))
    payload = ofx_preset._transform(bytes.fromhex(document["payload_hex"]))
    document["payload_hex"] = ofx_preset._transform(payload + b"filmExposureEv 3 1 nan\n").hex()
    bad = folder / "bad.spkpreset"
    bad.write_text(json.dumps(document), encoding="utf-8")
    try:
        ofx_node.load_preset(node, bad)
    except ValueError:
        pass
    else:
        raise AssertionError("Malformed snapshot was accepted")
    assert_values(ofx_node.parameters(node), original_values)
    assert node.preset_name == name
    batch_copy(folder)

    node_name, group_name = node.name, node.id_data.name
    blend = folder / "OFX and Lite.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(blend))
    bpy.ops.wm.open_mainfile(filepath=str(blend), load_ui=False)
    restored = bpy.data.node_groups[group_name].nodes[node_name]
    assert restored.bl_idname == ofx_node.FILM_TYPE
    assert restored.preset_name == name
    assert_values(ofx_node.parameters(restored), original_values)
    assert restored.node_tree.nodes.get("Film Result")
    print(f"OFX node regression passed: {len(ofx_schema.PARAMETERS)} settings; independent Lite/OFX, duplication, native presets, save/reopen")


with tempfile.TemporaryDirectory(prefix="bpe-ofx-node-") as directory:
    run(Path(directory))
