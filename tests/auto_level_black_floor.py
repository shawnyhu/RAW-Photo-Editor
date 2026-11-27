# SPDX-License-Identifier: GPL-3.0-or-later
"""Visible contrast invariants and migration of a real beta.7-generated graph.

blender -b --factory-startup --offline-mode --python-exit-code 1 -P tests/auto_level_black_floor.py
Installed copy: omit --factory-startup and append -- --installed.
GPU FULL: omit -b and append -- --installed --gpu.
"""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import runpy
import sys

import bpy
import numpy as np
import OpenImageIO as oiio

helpers = runpy.run_path(str(Path(__file__).with_name('auto_level.py')), run_name='levels_helpers')
levels_math, levels_node = helpers['levels_math'], helpers['levels_node']
native_effects = helpers['native_effects']
make_photo, render, close = (helpers[name] for name in ('make_photo', 'render', 'close'))


def black_floor(folder):
    pixels = np.ones((17, 33, 4), np.float32)
    pixels[..., :3] = np.linspace(0., .95018, 33, dtype=np.float32)[None, :, None]
    pixels[0, 0, :3] = -.055117
    photo, scene = make_photo(folder, 'negative-film-tail', pixels)
    effect = native_effects.create_effect(scene, levels_node.TYPE)
    node = native_effects.effect_node(effect)
    result = render(folder, 'negative-film-fixed', photo, scene)
    # The former formula maps black to 0.0548266 (~0.26 sRGB). Test the
    # required behavior independently of the implementation's reference helper.
    assert .054 < .055117 / (.95018 + .055117) < .056
    np.testing.assert_array_equal(result[1:, 0, :3], 0.)
    np.testing.assert_array_equal(result[:, -1, :3], 1.)
    before_steps = np.diff(pixels[1, :, :3], axis=0)
    after_steps = np.diff(result[1, :, :3], axis=0)
    assert np.all(after_steps >= before_steps - 2e-6), 'Visible ramp contrast compressed'
    close(result[1, :, :3], pixels[1, :, :3] / .95018, 'shared RGB visible-range gain')
    from importlib import import_module
    ui = import_module(levels_node.__package__ + '.levels_ui')
    sample = levels_node.capture(scene, node)
    close(ui.points(node, sample), (0., .95018), 'histogram markers use bounded endpoints')
    assert ui.domain(sample, *ui.points(node, sample)) == (0., 1.)
    ui._toggle(scene, node)
    assert not node.auto_level
    close((node.black_point, node.white_point), (0., .95018), 'Auto-off keeps corrected endpoints')
    levels_node.apply_points(node, -.055117, .95018)
    manual = render(folder, 'intentional-manual-lift', photo, scene)
    assert manual[1, 0, 0] > .054, 'Explicit manual negative black must remain available'

    # Once visible black/white are already present, changing the magnitude of
    # out-of-range tails cannot move the visible middle tones or white down.
    for name, low, high in (('tails-small', -.01, 1.1), ('tails-large', -2., 16.)):
        rgba = np.ones((17, 33, 4), np.float32)
        rgba[..., :3] = np.linspace(0., 1., 33, dtype=np.float32)[None, :, None]
        rgba[0, 0, :3], rgba[0, -1, :3] = low, high
        p, s = make_photo(folder, name, rgba)
        native_effects.create_effect(s, levels_node.TYPE)
        output = render(folder, 'native-' + name, p, s)
        close(output[1:], rgba[1:], 'negative/super-white tails cannot compress visible range')
    for low, high, expected in ((-.055117, .95018, (0., .95018)),
                                (.12, 7., (.12, 1.)), (-2., 7., (0., 1.)),
                                (-2., -.1, None), (2., 7., None), (.4, .4, None)):
        assert levels_math.auto_points(low, high) == expected
    print('PASS black stays black, white stays white, visible ramp does not compress, manual lift remains intentional', flush=True)


def legacy_file(folder):
    fixture = Path(__file__).with_name('fixtures') / 'auto_level_beta7.blend'
    # This is a real file saved by the published beta.7 installed extension,
    # containing only a synthetic ramp with the reported negative endpoint.
    assert bpy.ops.wm.open_mainfile(filepath=str(fixture)) == {'FINISHED'}
    scene = bpy.context.scene
    auto = native_effects.effect_node(scene.compositor_effects['Legacy Auto'])
    manual = native_effects.effect_node(scene.compositor_effects['Legacy Manual'])
    for node in (auto, manual):
        assert node.node_tree[levels_node.VERSION_TAG] == levels_node.GRAPH_VERSION
        pointer, count = node.node_tree.as_pointer(), len(node.node_tree.nodes)
        assert not levels_node.upgrade_tree(node.node_tree)
        assert pointer == node.node_tree.as_pointer() and count == len(node.node_tree.nodes)
    assert auto.auto_level and not manual.auto_level and auto.node_tree != manual.node_tree
    close((manual.black_point, manual.white_point), (-.1, 1.4), 'legacy manual values retained')
    scene.frame_set(2)
    close(manual.black_point, -.08, 'legacy animation retained')
    scene.frame_set(1)
    scene.render.compositor_device = 'GPU' if '--gpu' in sys.argv else 'CPU'
    scene.render.compositor_precision = 'FULL'
    scene.render.filepath = str(folder / 'migrated.exr')
    assert bpy.ops.render.render(scene=scene.name, write_still=True) == {'FINISHED'}
    with closing(oiio.ImageInput.open(scene.render.filepath)) as reader:
        actual = reader.read_image(format=oiio.FLOAT)
    assert actual.shape == (16, 32, 4) and np.isfinite(actual).all()
    np.testing.assert_array_equal(actual[1:, 0, :3], 0.)
    np.testing.assert_array_equal(actual[:, -1, :3], 1.)
    assert bpy.ops.wm.save_as_mainfile(filepath=str(folder / 'migrated.blend')) == {'FINISHED'}
    assert bpy.ops.wm.open_mainfile(filepath=str(folder / 'migrated.blend')) == {'FINISHED'}
    saved = native_effects.effect_node(bpy.context.scene.compositor_effects['Legacy Auto'])
    assert saved.auto_level and not levels_node.upgrade_tree(saved.node_tree)
    print('PASS actual beta.7 file migrates before first render; manual values, animation and idempotence preserved', flush=True)


with TemporaryDirectory(prefix='levels-black-floor-') as temporary:
    folder = Path(temporary)
    black_floor(folder)
    legacy_file(folder)
print('AUTO_LEVEL_BLACK_FLOOR_PASS', flush=True)
if '--gpu' in sys.argv:
    bpy.ops.wm.quit_blender()
