"""Verify the film node's play/pause control and GPU previews in a Blender window.

Run with an isolated BLENDER_USER_RESOURCES and GPU access:
blender --factory-startup --offline-mode --python-exit-code 1 --python tests/spektrafilm_ui.py
"""
from contextlib import closing
from pathlib import Path
import os
import sys
import tempfile
import time
import traceback

import bpy
import numpy as np
import OpenImageIO as oiio

installed = "bl_ext.user_default.raw_photo_editor" in bpy.context.preferences.addons
if installed:
    from bl_ext.user_default import raw_photo_editor as addon
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import blender_raw as addon
film_node, film_preview, gallery = addon.film_node, addon.film_preview, addon.gallery


def run(folder):
    if not installed:
        addon.register()
    if error := film_node.spektrafilm_engine.backend_error():
        raise RuntimeError(error)
    scene = bpy.context.window_manager.windows[0].scene
    window = bpy.context.window_manager.windows[0]
    area = max(window.screen.areas, key=lambda item: item.width * item.height)
    area.ui_type = "CompositorNodeTree"
    yield
    region = next(region for region in area.regions if region.type == "WINDOW")
    scene.render.resolution_x, scene.render.resolution_y = 64, 48
    scene.render.resolution_percentage = 100
    scene.compositor_effects.clear()
    graph = bpy.data.node_groups.new("Film Preview Test", "CompositorNodeTree")
    graph.interface.new_socket(name="Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    scene.compositor_effects.new("Photo").node_group = graph
    path = str(folder / "source.exr")
    with closing(oiio.ImageOutput.create(path)) as writer:
        assert writer.open(path, oiio.ImageSpec(64, 48, 3, oiio.FLOAT))
        pixels = np.full((48, 64, 3), [0.1, 0.45, 0.25], dtype=np.float32)
        pixels[15:30, 20:34] = [2.0, 4.0, 6.0]
        assert writer.write_image(pixels)
    source = graph.nodes.new("CompositorNodeImage")
    source.image = bpy.data.images.load(path)
    source.image.colorspace_settings.name = "Linear Rec.709"
    source.location.x = -300
    dev = graph.nodes.new(addon.compositor.DEVELOPER_TYPE)
    output = graph.nodes.new("NodeGroupOutput")
    output.location.x = 350
    graph.links.new(source.outputs["Image"], dev.inputs["Image"])
    graph.links.new(dev.outputs["Image"], output.inputs["Image"])
    space = area.spaces.active
    space.pin = True
    space.path.start(graph)
    graph.nodes.active = dev

    drew_button = []
    handler = film_node.draw_insert
    def record_button(layout, developer):
        handler(layout, developer)
        if developer == dev and "blender_raw.add_spektrafilm" in str(layout.introspect()):
            drew_button.append(True)
    film_node.draw_insert = record_button
    area.tag_redraw()
    with bpy.context.temp_override(window=window, area=area, region=region):
        bpy.ops.node.view_all()
    while not drew_button:
        yield
    film_node.draw_insert = handler

    layouts = {}
    icons = set()
    draw_controls = film_node.BR_NODE_spektrafilm.draw_buttons
    def record_layout(node, context, layout):
        # Observe the icon on the actual property draw for the selected node.
        class LayoutProbe:
            def __getattr__(self, name):
                return getattr(layout, name)

            def prop(self, owner, prop, **kwargs):
                if prop == "live_preview":
                    icons.add((context.region.type, node.live_preview, kwargs["icon"]))
                return layout.prop(owner, prop, **kwargs)
        if node == graph.nodes.active:
            draw_controls(node, context, LayoutProbe())
            layouts[context.region.type] = str(layout.introspect())
        else:
            draw_controls(node, context, layout)
    film_node.BR_NODE_spektrafilm.draw_buttons = record_layout
    overlay = set()
    label = gallery._label
    def record_overlay(text, *args, **kwargs):
        overlay.add(text)
        return label(text, *args, **kwargs)
    gallery._label = record_overlay
    space.show_region_ui = True
    ui_region = next(region for region in area.regions if region.type == "UI")
    ui_region.active_panel_category = "Node"

    starts = []
    start = film_preview._start
    def record_worker(*args, **kwargs):
        job = start(*args, **kwargs)
        starts.append(job.key)
        return job
    film_preview._start = record_worker
    with bpy.context.temp_override(window=window, area=area, region=region):
        assert bpy.ops.blender_raw.add_spektrafilm(tree_name=graph.name, node_name=dev.name) == {"FINISHED"}
        bpy.ops.node.view_all()
    film = graph.nodes.active
    assert film.bl_idname == film_node.FILM_TYPE and output.inputs[0].links[0].from_node == film
    area.tag_redraw()

    def ready(previous):
        while True:
            image = film.node_tree.nodes["Film Result"].image
            if image and image.filepath != previous and film.status == "Ready":
                return image.filepath
            if film.status and film.status not in {"Ready", "Rendering film preview"}:
                raise AssertionError(film.status)
            yield

    def assert_paused(seconds, filepath, started):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            image = film.node_tree.nodes["Film Result"].image
            assert (image.filepath if image else None) == filepath
            expected = "Paused · Holding last scan" if filepath else "Preview paused"
            assert film_preview.preview_status(film) == ("IDLE", expected)
            assert len(starts) == started, starts
            yield

    assert not film.live_preview
    film.film, film.paper = "kodak_portra_800", "kodak_2383"
    yield from assert_paused(1.5, None, 0)
    assert "Preview paused" in overlay, overlay
    film.live_preview = True
    film.live_preview = False
    yield from assert_paused(1.0, None, 0)
    sibling = graph.nodes.new(film_node.FILM_TYPE)
    sibling.location = (-250, -450)
    graph.links.new(dev.outputs["Image"], sibling.inputs["Image"])
    graph.nodes.active = film
    film.live_preview = True
    first = yield from ready(None)
    while "Live preview · Ready" not in overlay or {"WINDOW", "UI"} - layouts.keys():
        yield
    for region_type, drawn in layouts.items():
        assert "Camera Diffusion" in drawn and "Particle Area" not in drawn
        assert drawn.index("Live Preview") < drawn.index("Import .sfpreset")
        assert {(region_type, False, "PLAY"), (region_type, True, "PAUSE")} <= icons, icons

    # Gallery navigation replaces the scene effect and the graph shown in the editor.
    other = bpy.data.node_groups.new("Other Photo", "CompositorNodeTree")
    effect = scene.compositor_effects[0]
    started = len(starts)
    for tree in (other, graph):
        effect.node_group = tree
        space.path.start(tree)
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            yield
        assert (film_preview._key(film) in film_preview._active_nodes()) == (tree == graph)
    assert film_preview.preview_status(film)[0] == "READY"
    assert len(starts) == started and film.node_tree.nodes["Film Result"].image.filepath == first

    for key, *_ in film_node.FLOW_SECTIONS:
        setattr(film, "show_" + key, True)
    dev.exposure = 1.0
    second = yield from ready(first)
    assert first != second

    # Pause a real in-flight worker and hold the previous completed image.
    film.scannerEnabled = True
    while not film_preview._job:
        yield
    assert film_preview._job.key == film_preview._key(film)
    film.live_preview = False
    started = len(starts)
    film.scannerWhiteCorrection = True
    film.scannerWhiteLevel = 0.3
    dev.exposure = 0.75
    yield from assert_paused(1.5, second, started)
    film.live_preview = True
    film.scannerWhiteLevel = 0.8
    third = yield from ready(second)
    assert len(starts) == started + 1 and set(starts) == {film_preview._key(film)}, starts
    assert not sibling.live_preview and not sibling.node_tree.nodes["Film Result"].image
    # The resumed scan must include the newest upstream and film parameters.
    from importlib import import_module
    native = import_module(addon.__name__ + ".spektrafilm_engine")
    expected = native.render(pixels * 2**dev.exposure, **film_node.recipe(film), frame=scene.frame_current)
    with closing(oiio.ImageInput.open(third)) as reader:
        np.testing.assert_allclose(reader.read_image(format=oiio.FLOAT)[..., :3],
                                   expected, rtol=1e-3, atol=4e-4)
    visible = ("Grain Model", "Saturation", "Particle Area", "Strength RGB", "Boost EV",
               "Camera Diffusion", "Halo Warmth", "White Correction", "Black Level",
               "Live Preview")
    hidden = ("Film Exposure", "Print Exposure", "Print Diffusion", "Unsharp Amount",
              "Glare Roughness", "Spatial Scale", "Refresh Preview", "Live preview · Ready")
    for drawn in layouts.values():
        assert all(name in drawn for name in visible) and not any(name in drawn for name in hidden), drawn
    assert {"Live preview · Rendering", "Live preview · Ready", "Preview paused"} <= overlay, overlay
    film_node.BR_NODE_spektrafilm.draw_buttons = draw_controls
    gallery._label = label
    settled = len(starts)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        yield
    assert len(starts) == settled, ("Repeated film preview", starts)
    assert not film_preview._job and not film_preview._pending
    print(f"PASS play/pause UI, disabled edits, cancellation, held scan, latest settings and {settled} GPU workers",
          flush=True)


bpy.context.preferences.view.show_splash = False
temporary = tempfile.TemporaryDirectory(prefix="photo-editor-film-ui-")
steps = run(Path(temporary.name))
deadline = time.monotonic() + 90


def advance():
    try:
        assert time.monotonic() < deadline, "Timed out waiting for the film preview UI"
        next(steps)
        return 0.15
    except StopIteration:
        if not installed:
            addon.unregister()
        temporary.cleanup()
        bpy.ops.wm.quit_blender()
    except Exception:
        traceback.print_exc()
        sys.stderr.flush()
        os._exit(1)


bpy.app.timers.register(advance, first_interval=1.0)
