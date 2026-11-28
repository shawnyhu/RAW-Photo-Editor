# SPDX-License-Identifier: GPL-3.0-or-later
"""Check editable native processing controls and their legacy saved representation.

blender -b --offline-mode --python-exit-code 1 -P tests/spektrafilm_processing.py -- --installed --pixels
Omit --pixels for RNA, preset and persistence checks without a GPU.
"""
from copy import deepcopy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import bpy
import numpy as np

installed = "bl_ext.user_default.raw_photo_editor" in bpy.context.preferences.addons
if "--installed" in sys.argv:
    assert installed, "Enable the final installed ZIP first"
if installed:
    from bl_ext.user_default.raw_photo_editor import catalog, compositor, film_node, film_stock_ui, film_user_settings
    from bl_ext.user_default.raw_photo_editor import spektrafilm_engine as engine
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from blender_raw import catalog, compositor, film_node, film_stock_ui, film_user_settings
    from blender_raw import spektrafilm_engine as engine


METHODS = ("Hanatos 2026", "Hanatos 2025", "Mallett 2019")
FORMATS = ("Standard 8", "Super 8", "Standard 16", "Super 16",
           "Standard 35", "Super 35", "Standard 65", "IMAX 70")
KEYS = ("rgbToRawMethod", "filmFormat", "filmPushPullStops")
LEGACY = dict(rgbToRawMethod=1, filmFormat=6, filmPushPullStops=-.675432198765)


def processing(node):
    return node.rgbToRawMethod, node.filmFormat, node.filmPushPullStops


def assert_defaults(node):
    assert processing(node) == (METHODS[0], FORMATS[4], 0.)
    assert not any(node.is_property_set(key) for key in KEYS)
    assert not set(KEYS).intersection(film_node.recipe(node)["parameters"])
    settings = film_user_settings.snapshot(node)
    assert not set(KEYS).intersection(settings["controls"])
    assert not set(KEYS).intersection(settings["parameters"])


def check_controls(node):
    descriptors = engine._descriptors()
    assert descriptors["rgbToRawMethod"].default_int == 0
    assert descriptors["filmFormat"].default_int == 4
    assert descriptors["filmPushPullStops"].minimum == -2. and descriptors["filmPushPullStops"].maximum == 2.
    assert_defaults(node)
    for key, choices in (("rgbToRawMethod", METHODS), ("filmFormat", FORMATS)):
        assert tuple(item.identifier for item in node.bl_rna.properties[key].enum_items) == choices
        for value, label in enumerate(choices):
            setattr(node, key, label)
            assert film_node.recipe(node)["parameters"][key] == value
            assert film_user_settings.snapshot(node)["parameters"][key] == value
    for value in (-2., -.5, 0., 1., 2.):
        node.filmPushPullStops = value
        assert film_node.recipe(node)["parameters"]["filmPushPullStops"] == value
    for stock in engine.stocks()[0]:
        node.rgbToRawMethod, node.filmFormat, node.filmPushPullStops = METHODS[2], FORMATS[0], 1.5
        node.film = stock
        assert_defaults(node)
    node.rgbToRawMethod, node.filmFormat, node.filmPushPullStops = METHODS[1], FORMATS[7], -1.
    film_node.apply_stock_defaults(node)
    assert_defaults(node)
    print("PASS all native processing choices, push/pull range and 64 Film/Restore defaults", flush=True)


def check_presets(tree, node, folder):
    node.film = "kodak_portra_800"
    native = json.loads(node.preset_parameters)
    native.update(LEGACY)
    node.preset_parameters = json.dumps(native)
    assert processing(node)[:2] == (METHODS[1], FORMATS[6])
    assert abs(node.filmPushPullStops - LEGACY["filmPushPullStops"]) < 1e-6
    legacy = film_user_settings.snapshot(node)
    # Viewing old controls must not round their hidden JSON doubles or insert
    # new control fields into existing version-1 personal presets.
    for key, expected in LEGACY.items():
        assert legacy["parameters"][key] == expected
        assert film_node.recipe(node)["parameters"][key] == expected
    assert not set(KEYS).intersection(legacy["controls"])

    target = tree.nodes.new(film_node.FILM_TYPE)
    for live in (False, True):
        target.live_preview = live
        target.rgbToRawMethod, target.filmFormat, target.filmPushPullStops = METHODS[2], FORMATS[0], 2.
        film_user_settings.apply(target, legacy)
        assert film_user_settings.snapshot(target) == legacy and target.live_preview == live
        assert not any(target.is_property_set(key) for key in KEYS)
        target.rgbToRawMethod, target.filmFormat, target.filmPushPullStops = METHODS[2], FORMATS[7], .75
        edited = film_user_settings.snapshot(target)
        assert {key: edited["parameters"][key] for key in KEYS} == dict(rgbToRawMethod=2, filmFormat=7, filmPushPullStops=.75)
        film_user_settings.apply(node, edited)
        assert film_node.recipe(node) == film_node.recipe(target)
        assert not set(KEYS).intersection(edited["controls"])

    # Recipes saved before these fields existed must clear a target's edits.
    old_absent = deepcopy(legacy)
    for key in KEYS:
        del old_absent["parameters"][key]
    film_user_settings.apply(target, old_absent)
    assert_defaults(target)
    assert film_user_settings.snapshot(target) == old_absent

    path = folder / "legacy-processing.sfpreset"
    path.write_text(json.dumps({"version": 1, "settings": {
        "ints": {"film": engine.stocks()[0].index("kodak_portra_800"), "rgbToRawMethod": 1, "filmFormat": 6},
        "doubles": {"filmPushPullStops": LEGACY["filmPushPullStops"]}}}), encoding="utf-8")
    target.rgbToRawMethod, target.filmFormat, target.filmPushPullStops = METHODS[2], FORMATS[0], 2.
    film_node.load_preset(target, path)
    assert processing(target)[:2] == (METHODS[1], FORMATS[6])
    for key, expected in LEGACY.items():
        assert film_node.recipe(target)["parameters"][key] == expected
    assert not any(target.is_property_set(key) for key in KEYS)
    print("PASS old hidden-parameter precision, personal recipes, sfpreset import and both Live Preview states", flush=True)


class LayoutProbe:
    def __init__(self):
        self.keys = []
        self.active = True

    def prop(self, node, key, **kwargs):
        self.keys.append(key)

    def row(self, **kwargs):
        return self

    def split(self, **kwargs):
        return self

    def label(self, **kwargs):
        pass

    def operator(self, name, **kwargs):
        return SimpleNamespace()


def check_layout(node):
    node.film = "kodak_portra_800"
    probe = LayoutProbe()
    with patch.object(film_stock_ui, "draw_stock", side_effect=lambda layout, node, kind: layout.keys.append(kind)):
        node._draw_stocks(probe)
    assert probe.keys.index("process") < probe.keys.index("rgbToRawMethod") < probe.keys.index("filmFormat") < probe.keys.index("film"), probe.keys
    assert probe.keys.index("paper") < probe.keys.index("filmPushPullStops"), probe.keys
    print("PASS RGB to Raw and Film Format precede Film; Push/Pull follows Print", flush=True)


def check_pixels(node):
    assert not engine.backend_error(), engine.backend_error()
    image = np.empty((64, 96, 3), dtype=np.float32)
    image[:] = np.geomspace(.002, 3., 96)[:, None]
    image[16:32, :, 1:] *= .12
    image[32:48, :, ::2] *= .17
    image[48:, :, :2] *= .23

    def render():
        pixels = engine.render(image, **film_node.recipe(node))
        assert np.isfinite(pixels).all() and np.ptp(pixels[0]) > .01
        return pixels

    def changed(outputs, label):
        for a in range(len(outputs)):
            for b in range(a + 1, len(outputs)):
                delta = float(np.max(np.abs(outputs[a] - outputs[b])))
                assert delta > 1e-5, (label, a, b, delta)

    film_node.apply_stock_defaults(node, "kodak_portra_400")
    outputs = []
    for method in METHODS:
        node.rgbToRawMethod = method
        outputs.append(render())
    changed(outputs, "RGB to Raw")
    film_node.apply_stock_defaults(node)
    outputs = []
    for stops in (-2., 0., 2.):
        node.filmPushPullStops = stops
        outputs.append(render())
    changed(outputs, "Push/Pull")
    film_node.apply_stock_defaults(node)
    node.grain, node.grain_model, node.grainAmount = True, "Production", 1.5
    node.halation, node.halationAmount = True, 2.
    outputs = []
    for format in (FORMATS[0], FORMATS[4], FORMATS[7]):
        node.filmFormat = format
        outputs.append(render())
    changed(outputs, "Film Format with grain and halation")
    print("PASS actual GPU changes for all 3 RGB methods, negative/zero/positive push-pull and 3 film formats", flush=True)


def check_persistence(tree, folder):
    tree.use_fake_user = True
    expected = {}
    for index, method in enumerate(METHODS):
        node = tree.nodes.new(film_node.FILM_TYPE)
        node.name = method
        node.rgbToRawMethod = method
        node.filmFormat = FORMATS[(index * 3) % len(FORMATS)]
        node.filmPushPullStops = index - 1.5
        node.live_preview = bool(index % 2)
        expected[node.name] = film_node.recipe(node), film_user_settings.snapshot(node), node.live_preview
    name = tree.name
    path = str(folder / "processing.blend")
    assert bpy.ops.wm.save_as_mainfile(filepath=path) == {"FINISHED"}
    assert bpy.ops.wm.open_mainfile(filepath=path) == {"FINISHED"}
    tree = bpy.data.node_groups[name]
    for node_name, (recipe, settings, live) in expected.items():
        node = tree.nodes[node_name]
        assert film_node.recipe(node) == recipe and film_user_settings.snapshot(node) == settings
        assert node.live_preview == live
    print("PASS processing edits and Live Preview survive save/reopen", flush=True)


if not installed:
    catalog.register()
    compositor.register()
try:
    with TemporaryDirectory(prefix="film-processing-") as folder:
        folder = Path(folder)
        with patch.object(engine, "backend_error", return_value=""):
            tree = bpy.data.node_groups.new("Lite processing acceptance", "CompositorNodeTree")
            node = tree.nodes.new(film_node.FILM_TYPE)
            check_controls(node)
            check_presets(tree, node, folder)
            check_layout(node)
        if "--pixels" in sys.argv:
            check_pixels(node)
        with patch.object(engine, "backend_error", return_value=""):
            check_persistence(tree, folder)
finally:
    if not installed:
        compositor.unregister()
        catalog.unregister()
