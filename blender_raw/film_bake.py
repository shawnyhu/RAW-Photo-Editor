# SPDX-License-Identifier: GPL-3.0-or-later
"""Materialize the pixels arriving at a film node in an isolated Blender worker."""
from contextlib import contextmanager, ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

import bpy

from . import compositor, film_node, scene_pipeline


def group_output(tree):
    return next((node for node in tree.nodes if node.type == 'GROUP_OUTPUT' and node.is_active_output), None)


def _matching(sockets, identifier):
    return next((socket for socket in sockets if socket.identifier == identifier), None)


def active_input_links(socket):
    """Blender uses the socket default when every incoming wire is muted or invalid."""
    if not socket.is_linked:
        return ()
    return tuple(link for link in socket.links if link.is_valid and not link.is_muted)


def upstream(inputs, parents=(), *, preview=False):
    """Yield executable node instances, inputs before consumers, crossing group interfaces."""
    seen = set()

    def follow(sockets, path):
        for socket in sockets:
            for link in active_input_links(socket):
                yield from visit(link.from_socket, path)

    def visit(output, path):
        key = (output.as_pointer(), tuple(group.as_pointer() for group in path))
        if key in seen:
            return
        seen.add(key)
        node = output.node
        if node.mute:
            yield from follow((link.from_socket for link in node.internal_links if link.to_socket == output), path)
            return
        if preview and node.bl_idname in film_node.FILM_TYPES and not node.live_preview:
            # A paused scan feeds its last image to downstream previews. Before
            # its first scan the node simply passes the input through.
            if node.node_tree.nodes['Film Result'].image is None:
                yield from follow([node.inputs['Image']], path)
            return
        if node.bl_idname in {*film_node.FILM_TYPES, compositor.DEVELOPER_TYPE}:
            # Final exports and live scans evaluate the actual input branch.
            yield from follow([node.inputs['Image']], path)
        elif node.bl_idname == compositor.READER_TYPE:
            pass
        elif node.bl_idname == 'CompositorNodeSwitch' and not active_input_links(node.inputs['Switch']):
            yield from follow([node.inputs['On' if node.inputs['Switch'].default_value else 'Off']], path)
        elif node.type == 'GROUP_INPUT' and path:
            outer = _matching(path[-1].inputs, output.identifier)
            if outer:
                yield from follow([outer], path[:-1])
        elif node.type == 'GROUP' and node.node_tree:
            end = group_output(node.node_tree)
            inner = _matching(end.inputs, output.identifier) if end else None
            if inner:
                yield from follow([inner], (*path, node))
        else:
            yield from follow((s for s in node.inputs if s.enabled and s.is_linked), path)
        yield node, path

    yield from follow(inputs, parents)


def _paths(tree, target, path=()):
    if tree == target:
        yield path
        return
    for node in tree.nodes:
        if node.type == 'GROUP' and node.node_tree:
            yield from _paths(node.node_tree, target, (*path, node))


def _change(stack, obj, prop, value):
    old = getattr(obj, prop)
    if old != value:
        setattr(obj, prop, value)
        stack.callback(setattr, obj, prop, old)


@contextmanager
def _capture_scene(scene, root, mode='RENDER'):
    """Preserve preceding effect inputs while isolating side-effect outputs and later effects."""
    with ExitStack() as stack:
        # This runs in the isolated Blender worker. Native multi-effect stacks
        # still require a camera during render preflight, even with an image source.
        previous_camera, previous_world = scene.camera, scene.world
        previous_engine, previous_aa = scene.render.engine, scene.display.render_aa
        created_objects, created_cameras = set(bpy.data.objects), set(bpy.data.cameras)

        def restore_render_context():
            scene.camera, scene.world = previous_camera, previous_world
            scene.render.engine, scene.display.render_aa = previous_engine, previous_aa
            for obj in set(bpy.data.objects) - created_objects:
                bpy.data.objects.remove(obj, do_unlink=True)
            for camera in set(bpy.data.cameras) - created_cameras:
                if not camera.users:
                    bpy.data.cameras.remove(camera)

        stack.callback(restore_render_context)
        compositor.ensure_render_camera(scene)
        effects = scene.compositor_effects
        matches = [i for i, e in enumerate(effects) if e.node_group == root]
        if len(matches) > 1:
            raise ValueError('Spektrafilm requires a unique instance of this scene effect')
        pos = matches[0] if matches else None
        for i, effect in enumerate(effects):
            if pos is None or i > pos:
                _change(stack, effect, 'enable_for_render', False)
            elif mode == 'PREVIEW':
                _change(stack, effect, 'enable_for_render', effect.enable_for_preview)
        if pos is None:
            active = effects.active
            if active:
                stack.callback(setattr, effects, 'active', active)
            effect = effects.new('Spektrafilm Input')
            stack.callback(effects.remove, effect)
            effect.node_group = root
        else:
            _change(stack, effects[pos], 'enable_for_render', True)
        # File Output nodes execute even when disconnected from Group Output. Suppress
        # every instance in the participating effects, including those in child groups.
        trees = [e.node_group for e in effects if e.enable_for_render and e.node_group]
        visited = set()
        while trees:
            tree = trees.pop()
            if tree in visited:
                continue
            visited.add(tree)
            for node in tree.nodes:
                if node.bl_idname in {'CompositorNodeOutputFile', 'CompositorNodeViewer'}:
                    _change(stack, node, 'mute', True)
                child = getattr(node, 'node_tree', None)
                if child:
                    trees.append(child)
        _change(stack, scene.render, 'use_compositing', True)
        _change(stack, scene.render, 'use_sequencer', False)
        _change(stack, scene.render, 'use_multiview', False)
        _change(stack, scene.render, 'compositor_device', 'CPU')
        _change(stack, scene.render, 'compositor_precision', 'FULL')
        yield


def _capture(scene, root, node, parents, folder, mode='RENDER', *, colorspace='Linear Rec.709'):
    links = active_input_links(node.inputs['Image'])
    if not links:
        raise ValueError(f'Connect an image to {node.name} before rendering film')

    with _capture_scene(scene, root, mode), ExitStack() as stack:
        socket = links[0].from_socket
        for parent in reversed(parents):
            # Expose the actual instance input through each group on its path.
            child = parent.node_tree
            output = group_output(child)
            if not output or child.library:
                raise ValueError(f'Cannot capture Spektrafilm in linked or incomplete group {child.name}')
            extra = child.interface.new_socket(name='Spektrafilm Capture', in_out='OUTPUT', socket_type='NodeSocketColor')
            stack.callback(child.interface.remove, extra)
            child.links.new(socket, _matching(output.inputs, extra.identifier))
            socket = _matching(parent.outputs, extra.identifier)

        # Group Output would pad a 53x27 Image node to a 1920x1080 scene even
        # for a pure image graph. File Output writes the actual compositor domain.
        def temporary_node(kind):
            item = root.nodes.new(kind)
            stack.callback(root.nodes.remove, item)
            return item

        # File Output rasterizes onto a centered canvas. Center the branch first
        # so translated edges survive; the node restores its location downstream.
        info = temporary_node('CompositorNodeImageInfo')
        negate = temporary_node('ShaderNodeVectorMath')
        negate.operation = 'SCALE'
        negate.inputs['Scale'].default_value = -1
        separate = temporary_node('ShaderNodeSeparateXYZ')
        center = temporary_node('CompositorNodeTranslate')
        connect = root.links.new
        connect(socket, info.inputs['Image'])
        connect(info.outputs['Location'], negate.inputs[0])
        connect(negate.outputs['Vector'], separate.inputs[0])
        connect(socket, center.inputs['Image'])
        connect(separate.outputs['X'], center.inputs['X'])
        connect(separate.outputs['Y'], center.inputs['Y'])
        sink = temporary_node('CompositorNodeOutputFile')
        sink.directory, sink.file_name = str(folder), ''
        fmt = sink.format
        fmt.media_type, fmt.file_format = 'IMAGE', 'OPEN_EXR'
        fmt.exr_codec, fmt.color_depth, fmt.color_mode = 'NONE', '32', 'RGBA'
        fmt.color_management = 'OVERRIDE'
        fmt.linear_colorspace_settings.name = colorspace
        sink.save_as_render = True
        sink.file_output_items.new('RGBA', 'input')
        connect(center.outputs['Image'], sink.inputs[0])
        result = bpy.ops.render.render(scene=scene.name, write_still=False)
        path = folder / 'input.exr'
        if 'FINISHED' not in result or not path.is_file():
            raise RuntimeError(f'Blender could not evaluate the image connected to {node.name}')
        return path


def _film_passes(nodes):
    paths = {}
    for node, path in nodes:
        if node.bl_idname not in film_node.FILM_TYPES:
            continue
        if node in paths and paths[node] != path:
            raise ValueError(f'Make the group containing {node.name} single-user before rendering film')
        paths[node] = path
    return paths


def direct_raw_image(scene, film, mode='PREVIEW'):
    """Resolve a Developer that feeds only a decoded linear EXR and a constant exposure."""
    tree = film.id_data
    if bpy.data.colorspace.working_space != 'Linear Rec.709' or tree.animation_data or scene.animation_data:
        return None

    effect, parents = scene_pipeline.instance_for_node(scene, film, mode)
    if not effect or any(e.node_group.animation_data for e in scene_pipeline.effects(scene, mode)):
        return None

    def source(socket):
        # Any implicit socket conversion (e.g. Color -> Value) belongs to Blender.
        if socket is not None and socket.type == 'RGBA':
            return next((link.from_socket for link in socket.links if link.is_valid and not link.is_muted
                         and link.from_socket.type == 'RGBA'), None)
        return None

    _, output, _ = scene_pipeline.source_socket(scene, film.inputs['Image'], effect=effect,
                                                parents=parents, mode=mode, require_type='RGBA')
    if output is None or output.name != 'Image':
        return None
    developer = output.node
    if developer.bl_idname != compositor.DEVELOPER_TYPE or developer.mute:
        return None
    child = developer.node_tree
    if not child or child.animation_data:
        return None
    end = group_output(child)
    inlet = _matching(end.inputs, output.identifier) if end else None
    socket = source(inlet)
    if socket is None or socket.node.bl_idname != 'CompositorNodeExposure' or socket.node.mute:
        return None
    exposure = socket.node
    gain = exposure.inputs['Exposure']
    if gain.is_linked:
        # Native Developer wrappers expose Exposure as a real effect input.
        # Only the known metering offset may sit between it and Blender's node.
        meter = child.nodes.get('Meter Offset')
        links = active_input_links(gain)
        if (not meter or meter.bl_idname != 'ShaderNodeMath' or meter.operation != 'ADD' or meter.mute
                or len(links) != 1 or links[0].from_node != meter):
            return None
        developer_effect, developer_parents = scene_pipeline.instance_for_node(scene, developer, mode)
        valid, ev = scene_pipeline.constant_input(scene, developer.inputs['Exposure'],
            effect=developer_effect, parents=developer_parents, mode=mode)
        if not valid or active_input_links(meter.inputs[1]):
            return None
        gain_value = ev + meter.inputs[1].default_value
    else:
        gain_value = gain.default_value
    if not -32.0 <= gain_value <= 32.0:
        return None
    socket = source(exposure.inputs['Image'])
    if socket and socket.node.type == 'REROUTE':
        socket = source(socket.node.inputs[0])
    if (socket is None or socket.node.bl_idname != 'CompositorNodeImage' or socket.node.mute
            or socket.name != 'Image' or socket.node.name != 'Developed RAW'):
        return None
    image = socket.node.image
    if (not image or image.source != 'FILE' or image.type != 'IMAGE' or image.is_dirty
            or image.use_multiview or image.alpha_mode != 'PREMUL' or image.colorspace_settings.name != 'Linear Rec.709'):
        return None
    path = Path(bpy.path.abspath(image.filepath, library=image.library))
    if path.suffix.lower() != '.exr' or path.stem != image.get('photo_editor_decode_cache'):
        return None
    return image, gain_value


def direct_raw_input(scene, film, folder, mode='PREVIEW'):
    direct = direct_raw_image(scene, film, mode)
    if not direct:
        return None
    image, exposure = direct
    path = Path(bpy.path.abspath(image.filepath, library=image.library))
    if image.packed_file:
        path = folder / 'developed.exr'
        path.write_bytes(image.packed_file.data)
    return (path, exposure) if path.is_file() else None


def _process(scene, root, node, path, folder, output, *, temporary=False, mode='RENDER'):
    from . import film_backend
    direct = direct_raw_input(scene, node, folder, mode)
    params = {**film_node.recipe(node), 'frame': scene.frame_current + scene.frame_subframe}
    if direct:
        source, exposure = direct
        film_backend.process(source, output, params, temporary=temporary, raw_exposure=exposure)
    else:
        pixels = _capture(scene, root, node, path, folder, mode)
        film_backend.process(pixels, output, params, temporary=temporary)


@contextmanager
def _materialize(scene, passes, folder, mode='RENDER'):
    with ExitStack() as stack:
        for index, ((root, node), path) in enumerate(passes.items()):
            work = folder / f'film-{index}'
            work.mkdir()
            output = work / 'result.exr'
            _process(scene, root, node, path, work, output, temporary=True, mode=mode)
            image = bpy.data.images.load(str(output), check_existing=False)
            stack.callback(bpy.data.images.remove, image)
            image.colorspace_settings.name = 'Linear Rec.709'
            image.alpha_mode = 'PREMUL'
            previous = node.node_tree.nodes['Film Result'].image
            stack.callback(film_node.apply_result, node, previous)
            film_node.apply_result(node, image)
        yield


@contextmanager
def prepared(scene, tree, scratch):
    """Bind full-resolution film results across the complete native scene stack."""
    passes = _scene_film_passes(scene_pipeline.upstream(scene, mode='RENDER'))
    if not passes:
        yield False
        return
    with TemporaryDirectory(dir=scratch, prefix='film-') as folder:
        with _materialize(scene, passes, Path(folder)):
            yield True


def _scene_film_passes(instances):
    passes = {}
    seen = {}
    for effect, node, parents in instances:
        if node.bl_idname not in film_node.FILM_TYPES:
            continue
        identity = (effect.as_pointer(), tuple(n.as_pointer() for n in parents))
        if node in seen and seen[node] != identity:
            raise ValueError(f'Make the group containing {node.name} single-user before rendering film')
        seen[node] = identity
        passes[(effect.node_group, node)] = parents
    return passes


def preview(scene, tree, node, output):
    """Render a node in the snapshot, honoring paused previews on its inputs."""
    if node.bl_idname not in film_node.FILM_TYPES:
        raise ValueError('The selected Spektrafilm node no longer exists')
    effect, path = scene_pipeline.instance_for_node(scene, node, 'PREVIEW')
    if not effect:
        raise ValueError('The selected Spektrafilm effect is disabled or no longer exists')
    root = effect.node_group
    nodes = _scene_film_passes(scene_pipeline.upstream(scene, [node.inputs['Image']], effect=effect,
                                                      parents=path, mode='PREVIEW'))
    with TemporaryDirectory(prefix='spektrafilm-preview-') as tmp:
        folder = Path(tmp)
        with _materialize(scene, nodes, folder, 'PREVIEW'):
            _process(scene, root, node, path, folder, Path(output), mode='PREVIEW')
