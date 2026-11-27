# SPDX-License-Identifier: GPL-3.0-or-later
"""Compare scopes with actual native inputs after source color/alpha conversion."""
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

import bpy
import numpy as np
import OpenImageIO as oiio

if '--installed' in sys.argv:
    from bl_ext.user_default.raw_photo_editor import levels_math, levels_node, native_effects
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import blender_raw
    from blender_raw import levels_math, levels_node, native_effects
    blender_raw.register()
scene = bpy.context.scene
scene.render.engine = 'BLENDER_WORKBENCH'
scene.display.render_aa = 'OFF'
scene.render.compositor_device = 'CPU'
scene.render.compositor_precision = 'FULL'
scene.render.resolution_percentage = 100
scene.render.resolution_x, scene.render.resolution_y = 17, 11
scene.render.image_settings.file_format = 'OPEN_EXR'
scene.render.image_settings.color_depth = '32'
scene.render.image_settings.color_mode = 'RGBA'
scene.render.image_settings.color_management = 'OVERRIDE'
tree = bpy.data.node_groups.new('Color Input', 'CompositorNodeTree')
tree.interface.new_socket(name='Image', in_out='OUTPUT', socket_type='NodeSocketColor')
source = tree.nodes.new('CompositorNodeImage')
tree.links.new(source.outputs['Image'], tree.nodes.new('NodeGroupOutput').inputs['Image'])
scene.compositor_effects.new('Color Input').node_group = tree
effect = native_effects.create_effect(scene, levels_node.TYPE)
node = native_effects.effect_node(effect)
array = np.ones((11, 17, 4), np.float32)
array[..., :3] = np.linspace(.04, .8, 11 * 17 * 3).reshape(11, 17, 3)
array[1:3, 2:6, 3] = .25
array[:1, :, 3] = 0

with TemporaryDirectory(prefix='levels-colorspace-') as tmp:
    tmp = Path(tmp)
    for working, source_space, alpha_mode, ext in (
            ('ACEScg', 'Linear Rec.709', 'PREMUL', 'exr'),
            ('ACEScg', 'Linear Rec.709', 'STRAIGHT', 'exr'),
            ('Linear Rec.709', 'sRGB', 'STRAIGHT', 'png')):
        bpy.ops.wm.set_working_color_space(working_space=working, convert_colors=False)
        scene.render.image_settings.linear_colorspace_settings.name = working
        path = tmp / ('source.' + ext)
        data = array.copy()
        if alpha_mode == 'PREMUL':
            data[..., :3] *= data[..., 3:4]
        writer = oiio.ImageOutput.create(str(path))
        assert writer.open(str(path), oiio.ImageSpec(17, 11, 4, oiio.FLOAT if ext == 'exr' else oiio.UINT8))
        assert writer.write_image(data)
        writer.close()
        image = bpy.data.images.load(str(path), check_existing=False)
        image.colorspace_settings.name = source_space
        image.alpha_mode = alpha_mode
        source.image = image
        measured = levels_node.capture(scene, node)
        node.mute = True
        scene.render.filepath = str(tmp / 'actual-input.exr')
        assert bpy.ops.render.render(scene=scene.name, write_still=True) == {'FINISHED'}
        node.mute = False
        reader = oiio.ImageInput.open(scene.render.filepath)
        actual = reader.read_image(format=oiio.FLOAT)
        reader.close()
        expected = levels_math.analyze(actual)
        np.testing.assert_allclose((measured.low, measured.high), (expected.low, expected.high), rtol=2e-6, atol=2e-6)
        np.testing.assert_allclose(np.sort(measured.values), np.sort(expected.values), rtol=2e-6, atol=2e-6)
        assert measured.count == expected.count
        print('PASS', working, source_space, alpha_mode, ext, 'scope equals actual card input', flush=True)
        bpy.data.images.remove(image)

print('PASS working-space and alpha semantics for scopes', flush=True)
