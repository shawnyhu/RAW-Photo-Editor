# SPDX-License-Identifier: GPL-3.0-or-later
"""Persistent shared RGB levels in Blender's native compositor effect stack."""
import math

import bpy
import numpy as np
from bpy.props import BoolProperty, FloatProperty

from . import levels_math


TYPE = 'BlenderRawAutoLevel'
_applying = False


def make_tree():
    tree = bpy.data.node_groups.new('.Photo Auto Level', 'CompositorNodeTree')
    for direction in ('INPUT', 'OUTPUT'):
        tree.interface.new_socket(name='Image', in_out=direction, socket_type='NodeSocketColor')

    def node(kind, name, x, y):
        value = tree.nodes.new(kind)
        value.name, value.label, value.location = name, name, (x, y)
        return value

    link = tree.links.new

    def math_node(name, operation, a, b=0.0, *, clamp=False, x=500, y=0):
        item = node('ShaderNodeMath', name, x, y)
        item.operation, item.use_clamp = operation, clamp
        for socket, value in zip(item.inputs, (a, b)):
            if isinstance(value, (int, float)):
                socket.default_value = value
            else:
                link(value, socket)
        return item.outputs[0]

    def value(name, default, x, y):
        item = node('ShaderNodeValue', name, x, y)
        item.outputs[0].default_value = default
        return item.outputs[0]

    def mix(name, a, b, factor, data_type='FLOAT', *, x=900, y=0):
        item = node('ShaderNodeMix', name, x, y)
        item.data_type = data_type
        item.clamp_result = False
        for name, source in (('Factor', factor), ('A', a), ('B', b)):
            socket = next(s for s in item.inputs if s.name == name and s.enabled)
            if isinstance(source, (int, float)):
                socket.default_value = source
            else:
                link(source, socket)
        return next(s for s in item.outputs if s.enabled)

    def statistics(name, image, x, y):
        item = node('CompositorNodeLevels', name, x, y)
        item.inputs['Channel'].default_value = 'Red'
        link(image, item.inputs['Image'])
        return item.outputs

    source = node('NodeGroupInput', 'Source', 0, 200)
    output = node('NodeGroupOutput', 'Output', 1900, 200)
    separate = node('CompositorNodeSeparateColor', 'Premultiplied RGBA', 180, 200)
    combine = node('CompositorNodeCombineColor', 'Levels RGBA', 1500, 200)
    link(source.outputs['Image'], separate.inputs['Image'])
    alpha = separate.outputs['Alpha']
    link(alpha, combine.inputs['Alpha'])
    straight = [math_node(channel + ' Straight', 'DIVIDE', separate.outputs[channel], alpha,
                          x=360, y=200-index*180)
                for index, channel in enumerate(('Red', 'Green', 'Blue'))]
    visible = math_node('Visible Pixels', 'GREATER_THAN', alpha, levels_math.ALPHA_EPSILON,
                        x=360, y=-400)
    statistics_alpha = mix('Measurement Alpha', 1.0, alpha, visible, x=360, y=-650)
    measured = []
    for index, channel in enumerate(('Red', 'Green', 'Blue')):
        masked = math_node(channel + ' Visible RGB', 'MULTIPLY', separate.outputs[channel], visible,
                           x=360, y=-850-index*180)
        measured.append(math_node(channel + ' Measured RGB', 'DIVIDE', masked, statistics_alpha,
                                  x=500, y=-850-index*180))
    pixel_min = math_node('Minimum RG', 'MINIMUM', measured[0], measured[1], y=500)
    pixel_min = math_node('Minimum RGB', 'MINIMUM', pixel_min, measured[2], x=640, y=500)
    pixel_max = math_node('Maximum RG', 'MAXIMUM', measured[0], measured[1], y=700)
    pixel_max = math_node('Maximum RGB', 'MAXIMUM', pixel_max, measured[2], x=640, y=700)
    # Bound masked pixels with extrema already present in this input. Fixed
    # float-max sentinels overflow GPU half-precision intermediate textures.
    # The opposite global bound cannot expand the visible min/max interval.
    unmasked_min = statistics('Unmasked Minimum', pixel_min, 800, 1000)['Minimum']
    unmasked_max = statistics('Unmasked Maximum', pixel_max, 800, 1200)['Maximum']
    minimum = mix('Mask Minimum', unmasked_max, pixel_min, visible, x=800, y=500)
    maximum = mix('Mask Maximum', unmasked_min, pixel_max, visible, x=800, y=700)
    auto_black = statistics('Input Minimum', minimum, 1000, 500)['Minimum']
    auto_white = statistics('Input Maximum', maximum, 1000, 700)['Maximum']
    auto_range = math_node('Automatic Range', 'SUBTRACT', auto_white, auto_black, x=1170, y=700)
    valid = math_node('Nonconstant Input', 'GREATER_THAN', auto_range, 0.0, x=1310, y=700)
    automatic = value('Automatic', 1.0, 650, -650)
    manual_black = value('Manual Black', 0.0, 650, -850)
    manual_white = value('Manual White', 1.0, 650, -1050)
    black = mix('Effective Black', manual_black, auto_black, automatic, x=1000, y=-600)
    white = mix('Effective White', manual_white, auto_white, automatic, x=1000, y=-800)
    span = math_node('Effective Range', 'SUBTRACT', white, black, x=1170, y=-600)
    for index, channel in enumerate(('Red', 'Green', 'Blue')):
        y = 120 - index * 200
        shifted = math_node(channel + ' Black', 'SUBTRACT', straight[index], black, x=1050, y=y)
        mapped = math_node(channel + ' Range', 'DIVIDE', shifted, span, clamp=True, x=1200, y=y)
        premultiplied = math_node(channel + ' Premultiply', 'MULTIPLY', mapped, alpha, x=1350, y=y)
        link(premultiplied, combine.inputs[channel])
    # A constant or completely transparent input has no range to stretch.
    # Preserve it exactly in Auto; the explicit manual map remains available.
    apply_range = mix('Apply Valid Range', 1.0, valid, automatic, x=1450, y=900)
    result = mix('Automatic Constant Bypass', source.outputs['Image'], combine.outputs['Image'],
                 apply_range, 'RGBA', x=1700, y=200)
    link(result, output.inputs['Image'])
    return tree


def sync(node):
    tree = node.node_tree
    if not tree:
        return
    black, white = levels_math.points(node.black_point, node.white_point)
    tree.nodes['Manual Black'].outputs[0].default_value = black
    tree.nodes['Manual White'].outputs[0].default_value = white
    tree.nodes['Automatic'].outputs[0].default_value = float(node.auto_level)


def edited(node, _context):
    if not _applying:
        sync(node)


def points_edited(node, _context):
    if not _applying:
        if node.auto_level:
            node.auto_level = False
        else:
            sync(node)


def _black(node, value, _stored_value, _is_set):
    if _applying:
        return value
    if not math.isfinite(value):
        return node.black_point
    return min(value, float(np.nextafter(np.float32(node.white_point), np.float32(-np.inf))))


def _white(node, value, _stored_value, _is_set):
    if _applying:
        return value
    if not math.isfinite(value):
        return node.white_point
    return max(value, float(np.nextafter(np.float32(node.black_point), np.float32(np.inf))))


def apply_points(node, black, white):
    """Set a manual range atomically, without intermediate crossed ranges."""
    global _applying
    black, white = levels_math.points(black, white)
    was_applying = _applying
    _applying = True
    try:
        node.black_point, node.white_point = black, white
        node.auto_level = False
    finally:
        _applying = was_applying
    sync(node)


def reset(node, _context=None):
    apply_points(node, 0.0, 1.0)
    node.auto_level = True


def capture(scene, node):
    """Measure the whole current preview input, before this card is applied."""
    from contextlib import contextmanager
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from . import film_bake, scene_pipeline

    @contextmanager
    def reference():
        effect, parents = scene_pipeline.instance_for_node(scene, node, 'PREVIEW')
        if not effect:
            raise ValueError('Enable this card for preview to measure its input')
        _, output, _ = scene_pipeline.source_socket(scene, node.inputs['Image'], effect=effect,
                                                    parents=parents, mode='PREVIEW', require_type='RGBA')
        source = output.node if output else None
        colorspace = bpy.data.colorspace.working_space
        # Preserve socket semantics: an Image node's Alpha output can also be
        # linked to a color input, and must then be evaluated as a gray image.
        if (source and source.bl_idname == 'CompositorNodeImage' and output.name == 'Image'
                and colorspace == 'Linear Rec.709' and source.image
                and source.image.source in {'FILE', 'GENERATED'} and source.image.type == 'IMAGE'
                and source.image.is_float and source.image.alpha_mode == 'PREMUL'
                and source.image.colorspace_settings.name == 'Linear Rec.709' and all(source.image.size)):
            yield source.image
            return
        with TemporaryDirectory(prefix='photo-levels-') as folder:
            path = film_bake._capture(scene, effect.node_group, node, parents, Path(folder),
                                      mode='PREVIEW', colorspace=colorspace)
            image = bpy.data.images.load(str(path), check_existing=False)
            try:
                image.alpha_mode = 'PREMUL'
                image.colorspace_settings.name = colorspace
                yield image
            finally:
                bpy.data.images.remove(image)

    with reference() as image:
        width, height = image.size
        rgba = np.empty(width * height * 4, dtype=np.float32)
        image.pixels.foreach_get(rgba)
        return levels_math.analyze(rgba.reshape(height, width, 4))


class BR_NODE_auto_level(bpy.types.CompositorNodeCustomGroup):
    bl_idname = TYPE
    bl_label = 'Auto Level'
    bl_icon = 'IMAGE_DATA'
    bl_width_default = 280
    auto_level: BoolProperty(name='Auto Level', default=True, update=edited,
                             description='Fit one RGB range to the visible input of each photograph')
    black_point: FloatProperty(name='Black Point', description='Input value mapped to black in all RGB channels',
                               default=0.0, soft_min=0.0, soft_max=1.0, precision=5,
                               set_transform=_black, update=points_edited)
    white_point: FloatProperty(name='White Point', description='Input value mapped to white in all RGB channels',
                               default=1.0, soft_min=0.0, soft_max=1.0, precision=5,
                               set_transform=_white, update=points_edited)

    @classmethod
    def poll(cls, tree):
        return tree.bl_idname == 'CompositorNodeTree'

    def init(self, _context):
        self.node_tree = make_tree()
        sync(self)

    def copy(self, source):
        from . import adjustments
        if not adjustments.borrowing_groups() and not self.id_data.is_runtime_data and source.node_tree:
            self.node_tree = source.node_tree.copy()

    def draw_buttons(self, context, layout):
        from . import levels_ui
        levels_ui.draw(layout, self, context)

    draw_buttons_ext = draw_buttons


def register():
    bpy.utils.register_class(BR_NODE_auto_level)


def unregister():
    bpy.utils.unregister_class(BR_NODE_auto_level)
