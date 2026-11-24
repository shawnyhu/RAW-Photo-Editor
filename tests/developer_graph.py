"""Render Reader/Developer branches, native mute, duplication and clipboard transfer in Blender 5.3.

blender -b --factory-startup --python-exit-code 1 --python tests/developer_graph.py
"""
from contextlib import closing
from pathlib import Path
import sys
import tempfile
import uuid

import bpy
import numpy as np
import OpenImageIO as oiio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from blender_raw import catalog, compositor


def image(name, rgb, folder):
    values = np.tile([*rgb, 1.0], (24, 32, 1)).astype(np.float32)
    path = str(folder / (name + ".exr"))
    writer = oiio.ImageOutput.create(path)
    writer.open(path, oiio.ImageSpec(32, 24, 4, oiio.FLOAT))
    writer.write_image(values)
    writer.close()
    result = bpy.data.images.load(path)
    result.colorspace_settings.name = "Linear Rec.709"
    return result


def photo(name, pixels):
    record = bpy.context.scene.blender_raw.photos.add()
    record.uid = uuid.uuid4().hex
    record.name = name
    record.image = pixels
    return record, compositor.ensure_tree(bpy.context, record)


def render(photo, render_scene, folder, name):
    path = folder / (name + ".exr")
    compositor.export_photo(bpy.context, photo, path, render_scene)
    with closing(oiio.ImageInput.open(str(path))) as result:
        return result.read_image(format=oiio.FLOAT)[3, 5, :3].copy()


def of_type(tree, type_name):
    return next(node for node in tree.nodes if node.bl_idname == type_name)


def assert_rgb(actual, expected):
    np.testing.assert_allclose(actual, expected, atol=2e-6, rtol=2e-6)


def run(folder):
    catalog.register()
    compositor.register()
    scene = bpy.context.scene
    aset = scene.render.image_settings
    aset.file_format = "OPEN_EXR"
    aset.color_depth = "32"
    render_scene = compositor.export_scene(scene)
    base = [.0625, .125, .25]
    calibrated = [.03125, .5, 1.25]
    original, a = photo("First", image("first", base, folder))
    second, b = photo("Second", image("second", [.25, .125, .0625], folder))
    reader = of_type(a, compositor.READER_TYPE)
    dev = of_type(a, compositor.DEVELOPER_TYPE)
    link = a.links.new
    junction = of_type(a, "NodeReroute")
    removed = {"contrast", "highlights", "shadows", "whites", "blacks", "vibrance", "saturation"}
    assert removed.isdisjoint(dev.bl_rna.properties.keys())
    assert {"camera_input", "apple_lens_correction", "look", "white_balance", "auto_exposure", "exposure"} <= set(dev.bl_rna.properties.keys())
    stages = dev.node_tree.nodes
    assert stages["Output"].inputs["Image"].links[0].from_node == stages["Exposure"]
    assert stages["Exposure"].inputs["Image"].links[0].from_node == stages["Calibration"]
    assert not reader.inputs and len(reader.outputs) == 1
    assert dev.inputs["Image"].links[0].from_node == reader
    assert junction.inputs[0].links[0].from_node == dev
    assert {l.to_node.type for l in junction.outputs[0].links} == {"GROUP_OUTPUT", "VIEWER"}
    assert a.nodes.active == dev and compositor.developer_source(dev) == original
    assert_rgb(render(original, render_scene, folder, "baseline"), base)
    dev.exposure = 1
    assert_rgb(render(original, render_scene, folder, "one-stop"), np.multiply(base, 2))
    dev.mute = True
    assert_rgb(render(original, render_scene, folder, "muted"), base)
    dev.mute = False

    variant = image("calibrated", calibrated, folder)
    dev.white_balance = "CUSTOM"
    compositor.apply_developer_image(dev, original, variant)
    compositor.refresh_photo(original)
    assert dev.node_tree.nodes["Developed RAW"].image == variant
    assert_rgb(render(original, render_scene, folder, "calibrated-grade"), np.multiply(calibrated, 2))
    dev.mute = True
    assert_rgb(render(original, render_scene, folder, "muted-camera"), base)
    dev.mute = False
    from blender_raw import metering
    dev.auto_exposure = "GLOBAL"
    compensation = metering.exposure_ev("GLOBAL", variant)
    assert abs(compensation) > 0.1
    assert_rgb(render(original, render_scene, folder, "metered"), np.multiply(calibrated, 2**(1+compensation)))

    window = bpy.context.window
    area = max(window.screen.areas, key=lambda area: area.width * area.height)
    area.ui_type = "CompositorNodeTree"
    region = next(r for r in area.regions if r.type == "WINDOW")
    space = area.spaces.active
    with bpy.context.temp_override(window=window, area=area, region=region):
        space.pin = True
        space.path.start(a)
        for n in a.nodes:
            n.select = n == dev
        a.nodes.active = dev
        assert bpy.ops.node.duplicate() == {"FINISHED"}
        duplicate = a.nodes.active
        assert duplicate != dev and duplicate.instance_uid != dev.instance_uid
        assert duplicate.node_tree != dev.node_tree
        link(reader.outputs[0], duplicate.inputs[0])
        duplicate.white_balance = "CAMERA"
        duplicate.auto_exposure = "AS_SHOT"
        duplicate.exposure = -1
        assert dev.white_balance == "CUSTOM" and dev.auto_exposure == "GLOBAL" and dev.exposure == 1
        link(duplicate.outputs[0], junction.inputs[0])
        assert_rgb(render(original, render_scene, folder, "second-branch"), np.multiply(base, .5))

        assert bpy.ops.node.clipboard_copy() == {"FINISHED"}
        space.path.start(b)
        assert bpy.ops.node.clipboard_paste() == {"FINISHED"}
        pasted = next(n for n in b.nodes if n.bl_idname == compositor.NODE_TYPE and n.exposure == -1)
        assert pasted.instance_uid != duplicate.instance_uid and pasted.node_tree != duplicate.node_tree
        b.links.new(of_type(b, compositor.READER_TYPE).outputs[0], pasted.inputs[0])
        b.links.new(pasted.outputs[0], of_type(b, "NodeReroute").inputs[0])
        compositor._sync_developer_sources()
        assert compositor.developer_source(pasted) == second
        assert_rgb(render(second, render_scene, folder, "second-photo"), [.125, .0625, .03125])

    # Changing a linked DCP on disk must invalidate a recipe even if no
    # Blender controls moved. Decoding is covered separately; exercise its key here.
    profile = folder / "linked.dcp"
    profile.write_bytes(b"first profile")
    original.filepath = original.image.filepath
    dev.profile_mode = "CUSTOM"
    dev.profile_path = str(profile)
    initial_key = catalog.request(original, "preview", recipe=dev)["key"]
    cached = image(initial_key, calibrated, folder)
    compositor.apply_developer_image(dev, original, cached)
    compositor._refresh_developer(dev, original, preview=True)
    assert not dev.get("recipe_dirty")
    profile.write_bytes(b"replacement profile")
    compositor._refresh_developer(dev, original, preview=True)
    assert dev.get("recipe_dirty") and dev.node_tree.nodes["Developed RAW"].image == cached
    profile.unlink()
    compositor._refresh_developer(dev, original, preview=True)
    assert "Profile missing" in dev.error and dev.node_tree.nodes["Developed RAW"].image == cached
    print("PASS native reroute, Viewer, HDR camera variant, independent duplication, mute, metering and cross-photo paste")
    compositor.unregister()
    catalog.unregister()


with tempfile.TemporaryDirectory() as work:
    run(Path(work))
