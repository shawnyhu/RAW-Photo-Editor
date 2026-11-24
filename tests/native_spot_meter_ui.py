"""Verify Spot overlay lifecycle and exposure in a real Blender 5.3 window.

blender --factory-startup --enable-event-simulate --python tests/native_spot_meter_ui.py
"""
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback

import bpy
import numpy as np
import OpenImageIO as oiio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from blender_raw import catalog, compositor, metering, native_effects, spot_meter


temporary = tempfile.TemporaryDirectory(prefix="native-spot-meter-ui-")
draws = []
make_batch = spot_meter.batch_for_shader


def record_batch(shader, kind, data):
    draws.append((spot_meter._picker, bpy.context.scene.session_uid, data["pos"]))
    return make_batch(shader, kind, data)


def wait_for(predicate, message):
    until = time.monotonic() + 8
    while not predicate():
        assert time.monotonic() < until, message
        yield


def photo(name):
    result = catalog.library(bpy.context).photos.add()
    result.uid = result.name = name
    y, x = np.mgrid[0:400, 0:640]
    pixels = np.ones((400, 640, 4), dtype=np.float32)
    pixels[:, :, 0] = .02 + x / 1100
    pixels[:, :, 1] = .03 + y / 900
    pixels[:, :, 2] = .06 + (x + y) / 1700
    path = Path(temporary.name) / (name + ".exr")
    writer = oiio.ImageOutput.create(str(path))
    assert writer.open(str(path), oiio.ImageSpec(640, 400, 4, oiio.FLOAT))
    assert writer.write_image(pixels)
    writer.close()
    result.filepath = result.source_absolute = str(path)
    result.image = bpy.data.images.load(str(path))
    result.image.colorspace_settings.name = "Linear Rec.709"
    result.status = "Ready"
    return result


def test():
    catalog.register()
    compositor.register()
    spot_meter.batch_for_shader = record_batch
    window = bpy.context.window_manager.windows[0]
    bpy.context.preferences.view.show_splash = False
    window.event_simulate(type="ESC", value="PRESS")
    window.event_simulate(type="ESC", value="RELEASE")
    yield
    window.scene.blender_raw.owner = True
    original, other = photo("Spot Original"), photo("Spot Other")
    scene = compositor.ensure_photo_scene(bpy.context, original)
    other_scene = compositor.ensure_photo_scene(bpy.context, other)
    window.scene = scene
    node = native_effects.effect_node(scene.compositor_effects[1])
    node.camera_input = "LEGACY"
    node.meter_mode = "SPOT"
    assert node.spot_radius == 30
    assert node.bl_rna.properties["spot_radius"].default == 30
    area = max(window.screen.areas, key=lambda item: item.width * item.height)
    area.type = "IMAGE_EDITOR"
    area.spaces.active.image = original.image
    area.spaces.active.show_region_ui = False
    area.spaces.active.show_region_toolbar = False
    yield
    region = next(item for item in area.regions if item.type == "WINDOW")
    with bpy.context.temp_override(window=window, area=area, region=region):
        bpy.ops.image.view_all(fit_view=True)
    yield
    assert not spot_meter._picker and spot_meter._handle is None
    assert not draws, "Spot mode must not draw a persistent circle"

    x, y = region.view2d.view_to_region(.36, .62, clip=False)
    coordinates = dict(x=x + region.x, y=y + region.y)
    position = compositor._sample_position(area, coordinates["x"], coordinates["y"])
    assert position and all(0 < value < 1 for value in position)

    def invoke():
        with bpy.context.temp_override(window=window, area=area, region=region):
            assert bpy.ops.blender_raw.pick_meter("INVOKE_DEFAULT", tree_name=node.id_data.name,
                                                node_name=node.name) == {"RUNNING_MODAL"}
        assert spot_meter._picker and spot_meter._handle is not None
        window.event_simulate(type="MOUSEMOVE", value="NOTHING", **coordinates)

    def idle():
        count = len(draws)
        assert spot_meter._picker is None and spot_meter._handle is None
        for _ in range(3):
            area.tag_redraw()
            yield
        assert len(draws) == count, "A completed/cancelled picker left its draw callback active"

    saved = tuple(node.spot_center)
    invoke()
    yield from wait_for(lambda: bool(draws), "Active picker did not draw its circle")
    assert all(picker is not None and owner == scene.session_uid for picker, owner, _ in draws)
    window.event_simulate(type="ESC", value="PRESS", **coordinates)
    window.event_simulate(type="ESC", value="RELEASE", **coordinates)
    yield from wait_for(lambda: spot_meter._picker is None, "Esc did not cancel picker")
    assert tuple(node.spot_center) == saved
    yield from idle()

    before = len(draws)
    invoke()
    yield from wait_for(lambda: len(draws) > before, "Second picker did not draw")
    window.event_simulate(type="LEFTMOUSE", value="PRESS", **coordinates)
    window.event_simulate(type="LEFTMOUSE", value="RELEASE", **coordinates)
    yield from wait_for(lambda: spot_meter._picker is None, "Click did not finish picker")
    np.testing.assert_allclose(node.spot_center, position, atol=1e-6)
    assert node.auto_exposure == "SPOT", "Finishing the picker must keep Spot exposure enabled"
    expected_ev = metering.exposure_ev("SPOT", original.image, spot=(*position, 30))
    np.testing.assert_allclose(node.node_tree.nodes["Meter Offset"].inputs[1].default_value, expected_ev, atol=1e-6)
    readout = spot_meter._exposure_readout(node, original)
    assert readout == f"Auto exposure: {round(expected_ev, 2) or 0.0:+.2f} EV", readout
    node.exposure = 1.25
    assert spot_meter._exposure_readout(node, original) == readout
    yield from idle()

    saved = tuple(node.spot_center)
    invoke()
    window.event_simulate(type="RIGHTMOUSE", value="PRESS", **coordinates)
    window.event_simulate(type="RIGHTMOUSE", value="RELEASE", **coordinates)
    yield from wait_for(lambda: spot_meter._picker is None, "Right-click did not cancel")
    assert tuple(node.spot_center) == saved
    yield from idle()

    invoke()
    window.scene = other_scene
    yield from wait_for(lambda: spot_meter._picker is None, "Scene switch did not cancel picker")
    assert tuple(node.spot_center) == saved
    yield from idle()
    window.scene = scene
    area.spaces.active.image = original.image

    invoke()
    area.spaces.active.image = other.image
    yield from wait_for(lambda: spot_meter._picker is None, "Image switch did not cancel picker")
    yield from idle()
    area.spaces.active.image = original.image

    invoke()
    node.auto_exposure = "AS_SHOT"
    yield from wait_for(lambda: spot_meter._picker is None, "Changing meter mode did not cancel picker")
    yield from idle()
    node.auto_exposure = "SPOT"

    invoke()
    area.type = "VIEW_3D"
    yield from wait_for(lambda: spot_meter._picker is None, "Changing editor did not cancel picker")
    yield from idle()
    area.type = "IMAGE_EDITOR"
    area.spaces.active.image = original.image
    yield
    region = next(item for item in area.regions if item.type == "WINDOW")
    invoke()
    spot_meter._cancel_picker()
    yield from idle()
    assert all(spot_meter._cancel_picker in getattr(bpy.app.handlers, name)
               for name in ("load_pre", "undo_pre", "redo_pre"))

    # The button belongs to its own Developer card, independent of the native
    # stack's selected effect. Exercise the real compositor Viewer branch.
    node.exposure = 0
    second_effect = native_effects.create_effect(scene, compositor.DEVELOPER_TYPE, photo=original)
    second_node = native_effects.effect_node(second_effect)
    second_node.camera_input = "LEGACY"
    second_node.meter_mode = "SPOT"
    second_node.spot_center = (.8, .2)
    second_center = tuple(second_node.spot_center)
    area.spaces.active.image = compositor.viewer_image()
    area.spaces.active.image.use_view_as_render = True
    yield from wait_for(lambda: area.spaces.active.image.has_data
                       and tuple(area.spaces.active.image.size) == tuple(original.image.size),
                       "Native compositor Viewer did not receive photo pixels")
    with bpy.context.temp_override(window=window, area=area, region=region):
        bpy.ops.image.view_all(fit_view=True)
    yield
    for active, target_uv in ((scene.compositor_effects[0], (.28, .72)), (second_effect, (.64, .43))):
        scene.compositor_effects.active = active
        x, y = region.view2d.view_to_region(*target_uv, clip=False)
        coordinates = dict(x=x + region.x, y=y + region.y)
        position = compositor._sample_position(area, coordinates["x"], coordinates["y"])
        assert position
        assert spot_meter._matches(area, scene, node)
        invoke()
        window.event_simulate(type="LEFTMOUSE", value="PRESS", **coordinates)
        window.event_simulate(type="LEFTMOUSE", value="RELEASE", **coordinates)
        yield from wait_for(lambda: spot_meter._picker is None, "Non-active Developer picker did not finish")
        np.testing.assert_allclose(node.spot_center, position, atol=1e-6)
        assert tuple(second_node.spot_center) == second_center
        assert scene.compositor_effects.active == active
        yield from idle()
    spot_meter.batch_for_shader = make_batch
    print("PASS native Spot: default 30%, picker-only overlay, click/cancel/scene cleanup, correct EV and non-active Developer targeting", flush=True)


steps = test()
deadline = time.monotonic() + 75


def advance():
    try:
        assert time.monotonic() < deadline, "Spot GUI regression timed out"
        next(steps)
        return .15
    except StopIteration:
        temporary.cleanup()
        bpy.ops.wm.quit_blender()
    except Exception:
        traceback.print_exc()
        sys.stderr.flush()
        os._exit(1)


bpy.app.timers.register(advance, first_interval=1)
