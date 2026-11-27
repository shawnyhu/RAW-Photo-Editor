# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-photo compositor graphs, RAW input and development nodes, and image output."""
import os
import textwrap
import time
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, PointerProperty, StringProperty

from . import crop_node, levels_node
from . import catalog, film_display, film_node, metering, spektrafilm_engine, spot_meter


NODE_TYPE = "BlenderRawDevelop"
DEVELOPER_TYPE = NODE_TYPE
READER_TYPE = "PhotoRawReader"
STUDIO_TAG = "blenderraw_darkroom"
PHOTO_TAG = "blenderraw_photo_id"
DECODE_TAG = "blenderraw_decode_source"
_AUTO_CPU_TAG = "blenderraw_restore_gpu"
# Blender 5.3's node preview and final render both reject GPU dimensions above 8192.
_GPU_COMPOSITOR_LIMIT = 8192


def _make_node(tree, kind, name, x, y):
    node = tree.nodes.new(kind)
    node.name = name
    node.label = name
    node.location = (x, y)
    return node


def _create_reader():
    tree = bpy.data.node_groups.new(".Blender Photo Editor Reader", "CompositorNodeTree")
    tree.interface.new_socket(name="Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    source = _make_node(tree, "CompositorNodeImage", "Decoded Image", 0, 50)
    output = _make_node(tree, "NodeGroupOutput", "Output", 220, 50)
    tree.links.new(source.outputs["Image"], output.inputs["Image"])
    return tree


def _add_input_stage(tree):
    tree.interface.new_socket(name="Image", in_out="INPUT", socket_type="NodeSocketColor")
    source = _make_node(tree, "NodeGroupInput", "Developer Input", -800, 160)
    calibration = _make_node(tree, "NodeReroute", "Calibration", 0, 50)
    tree.links.new(source.outputs["Image"], calibration.inputs[0])
    return calibration


def _create_pipeline():
    tree = bpy.data.node_groups.new(".Blender Photo Editor Developer", "CompositorNodeTree")
    tree.interface.new_socket(name="Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    link = tree.links.new
    source = _add_input_stage(tree)
    ev = tree.interface.new_socket(name="Exposure", in_out="INPUT", socket_type="NodeSocketFloat")
    ev.default_value, ev.min_value, ev.max_value = 0.0, -12.0, 12.0
    ev.hide_value = True  # Keep the link socket; its value is drawn in Auto Exposure.
    meter = _make_node(tree, "ShaderNodeMath", "Meter Offset", 0, -140)
    meter.operation = 'ADD'
    meter.inputs[1].default_value = 0.0
    link(tree.nodes["Developer Input"].outputs["Exposure"], meter.inputs[0])
    exposure = _make_node(tree, "CompositorNodeExposure", "Exposure", 200, 50)
    link(source.outputs[0], exposure.inputs["Image"])
    link(meter.outputs[0], exposure.inputs["Exposure"])

    output = _make_node(tree, "NodeGroupOutput", "Output", 430, 50)
    link(exposure.outputs["Image"], output.inputs["Image"])
    return tree


def _upstream_source(node):
    """Follow the image path whose sensor coordinates and calibration we can identify."""
    seen = set()
    while node and node.as_pointer() not in seen:
        seen.add(node.as_pointer())
        if node.bl_idname == READER_TYPE:
            return None if node.mute else node
        if node.bl_idname not in {NODE_TYPE, "NodeReroute"} or not node.inputs:
            break
        link = next((link for link in node.inputs[0].links if link.is_valid and not link.is_muted), None)
        node = link.from_node if link else None
    return None


def developer_source(node, mode='PREVIEW'):
    source = _upstream_source(node)
    if source and source.bl_idname == READER_TYPE:
        return catalog.lookup_photo(None, source.photo_uid)
    from . import scene_pipeline
    for scene in bpy.data.scenes:
        if not scene.get(STUDIO_TAG):
            continue
        for effect, candidate, parents in scene_pipeline.node_instances(scene, NODE_TYPE, mode='ALL'):
            if candidate != node:
                continue
            readers = {n.photo_uid for _, n, _ in scene_pipeline.upstream(
                scene, node.inputs[:1], effect=effect, parents=parents, mode=mode)
                if n.bl_idname == READER_TYPE and not n.mute}
            if len(readers) == 1:
                return catalog.lookup_photo(scene, readers.pop())
    return None


def _image_predecessor(node, mode='PREVIEW'):
    """Follow a connected Image input through reroutes only."""
    original = node
    seen = set()
    while node and node.inputs and node.as_pointer() not in seen:
        seen.add(node.as_pointer())
        link = next((link for link in node.inputs[0].links if link.is_valid and not link.is_muted), None)
        node = link.from_node if link else None
        if not node or node.bl_idname != "NodeReroute":
            if node and node.type not in {'GROUP_INPUT', 'GROUP'}:
                return node
            break
    from . import scene_pipeline
    for scene in bpy.data.scenes:
        for _, candidate, parents in scene_pipeline.node_instances(scene, mode='ALL'):
            if candidate == original:
                return scene_pipeline.immediate_image_source(scene, original, parents=parents, mode=mode)
    return None


def apply_spektrafilm_developer(film, settings):
    """Apply explicit Apple camera settings in the preset. The Developer owns exposure."""
    flags, choices, values = (settings.get(key, {}) for key in ("bools", "ints", "doubles"))
    if "rawWhiteBalanceMode" not in choices and "rawLensCorrection" not in flags:
        return None
    developer = _image_predecessor(film)
    if (not developer or developer.bl_idname != NODE_TYPE or developer.mute
            or developer.id_data.library or not developer_source(developer)):
        return "Film settings loaded. Connect a Raw Developer with an Apple RAW input to apply preset white balance and lens correction."
    if not catalog.uses_apple_input(developer):
        return "Film settings loaded. The selected RAW decoder supplies white balance and lens correction."
    global _copying_recipe
    _copying_recipe = True
    try:
        if "rawWhiteBalanceMode" in choices:
            wb = ("CAMERA", "DAYLIGHT", "CUSTOM")[choices["rawWhiteBalanceMode"]]
            developer.apple_white_balance = wb
            if wb == "CUSTOM":
                developer.temperature = values.get("rawTemperature", 5500.0)
                developer.tint = values.get("rawTint", 0.0)
        if "rawLensCorrection" in flags:
            developer.apple_lens_correction = flags["rawLensCorrection"]
    finally:
        _copying_recipe = False
    _recipe_changed(developer, None)
    return None


def developers_for(photo):
    for tree in bpy.data.node_groups:
        if tree.library or tree.bl_idname != "CompositorNodeTree":
            continue
        for node in tree.nodes:
            if node.bl_idname == NODE_TYPE and developer_source(node) == photo:
                yield node


def uses_camera_adjustments(node):
    return (node.camera_input != "LEGACY" or node.white_balance != "CAMERA" or node.profile_mode != "EMBEDDED"
            or node.use_dcp_look or node.use_dcp_curve)


def developer_status(node):
    """Read the current preview state without opening files or scheduling work."""
    if node.mute or not node.node_tree:
        return None
    photo = developer_source(node)
    if not photo:
        return None
    from . import jobs
    phase = jobs.preview_phase(photo)
    if phase:
        return phase, "Queued · RAW preview" if phase == "QUEUED" else "Reading RAW preview"
    if photo.error:
        return "ERROR", "Error · " + photo.error
    if uses_camera_adjustments(node):
        phase = jobs.preview_phase(photo, node)
        if phase:
            return phase, "Queued · Development" if phase == "QUEUED" else "Developing preview"
        if node.photo_uid == photo.uid:
            if node.error:
                return "ERROR", "Error · " + node.error
            nodes = node.node_tree.nodes
            developed, baseline = nodes.get("Developed RAW"), nodes.get("As Shot RAW")
            if (not node.get("recipe_dirty") and developed and developed.image
                    and photo.image and baseline and baseline.image == photo.image):
                label = developed.image.get("photo_editor_profile")
                return "READY", f"Ready · {label}" if label else "Preview ready"
    elif photo.image:
        return "READY", "Preview ready"
    return "IDLE", "Awaiting preview"


class BR_MT_preview_status(bpy.types.Menu):
    bl_idname = "BLENDER_RAW_MT_preview_status"
    bl_label = "Preview Status"

    def draw(self, context):
        node = getattr(context, "active_node", None)
        state = _preview_status(node)
        if state:
            for line in state[1].splitlines():
                for part in textwrap.wrap(line, width=60):
                    self.layout.label(text=part)


def _preview_status_menu(self, context):
    node = context.active_node
    if _preview_status(node):
        self.layout.menu(BR_MT_preview_status.bl_idname, icon="INFO")


def _preview_status(node):
    if node and node.bl_idname == NODE_TYPE:
        return developer_status(node)
    if node and node.bl_idname in film_node.FILM_TYPES:
        from . import film_preview
        return film_preview.preview_status(node)
    return None


def _direct_reader_image(node, photo):
    reader = _image_predecessor(node, mode='RENDER' if bpy.app.background else 'PREVIEW')
    if not reader or reader.bl_idname != READER_TYPE or reader.mute or not reader.node_tree:
        return False
    source = reader.node_tree.nodes.get("Decoded Image")
    output = next((n for n in reader.node_tree.nodes if n.type == "GROUP_OUTPUT" and n.is_active_output), None)
    return bool(source and source.image == photo.image and output and
                any(link.from_socket == source.outputs["Image"] and link.is_valid and not link.is_muted
                    for link in output.inputs["Image"].links))


def _meter_image(node):
    variant = node.node_tree.nodes.get("Developed RAW")
    if variant and variant.image:
        return variant.image
    source = _upstream_source(node)
    if source and source.bl_idname == READER_TYPE and source.node_tree:
        return source.node_tree.nodes["Decoded Image"].image
    photo = developer_source(node)
    if photo:
        return photo.image
    return None


def apply_developer_image(node, photo, image):
    """Apply a RAW calibration to the input while retaining differential RGB edits."""
    tree = node.node_tree
    nodes = tree.nodes
    source = nodes["Developer Input"].outputs["Image"]
    target = nodes["Calibration"].inputs[0]
    baseline = nodes.get("As Shot RAW")
    developed = nodes.get("Developed RAW")
    if image is not None and photo is not None and photo.image is not None:
        # For a Reader input this yields exactly the calibrated pixels. Any delta
        # between that input and the original remains in the edited RGB signal.
        if baseline is None:
            baseline = _make_node(tree, "CompositorNodeImage", "As Shot RAW", -800, -80)
            developed = _make_node(tree, "CompositorNodeImage", "Developed RAW", -580, -320)
            subtract = _make_node(tree, "ShaderNodeMix", "Input Difference", -540, 140)
            add = _make_node(tree, "ShaderNodeMix", "Apply Camera Calibration", -270, 140)
            for mix, operation in ((subtract, "SUBTRACT"), (add, "ADD")):
                mix.data_type = "RGBA"
                mix.blend_type = operation
                mix.clamp_result = False
                mix.inputs[0].default_value = 1
            tree.links.new(source, subtract.inputs[6])
            tree.links.new(baseline.outputs["Image"], subtract.inputs[7])
            tree.links.new(subtract.outputs[2], add.inputs[6])
            tree.links.new(developed.outputs["Image"], add.inputs[7])
        baseline.image = photo.image
        developed.image = image
        node.photo_uid = photo.uid
        if "recipe_dirty" in node:
            del node["recipe_dirty"]
        # A direct, unedited Reader contributes no RGB delta. Use the selected
        # decoder's own pixel domain even if its crop differs from the Reader.
        source = developed.outputs["Image"] if _direct_reader_image(node, photo) else nodes["Apply Camera Calibration"].outputs[2]
    elif baseline:
        baseline.image = None
        developed.image = None
    if not target.links or target.links[0].from_socket != source:
        tree.links.new(source, target)
    _update_grade(node, None)
    if photo:
        for scene in bpy.data.scenes:
            if scene.get(STUDIO_TAG) and scene.get(PHOTO_TAG) == photo.uid:
                _set_dimensions(scene, photo)


def _exposure_value(node):
    from . import native_effects
    owner = native_effects.owner_effect(node)
    if owner and native_effects.effect_node(owner[1]) == node:
        value = native_effects.get_input_value(owner[1], "Exposure")
        if value is not None:
            return float(value)
    socket = node.inputs.get("Exposure")
    return float(socket.default_value) if socket else 0.0


def _set_exposure_value(node, value):
    from . import native_effects
    owner = native_effects.owner_effect(node)
    if owner and native_effects.effect_node(owner[1]) == node:
        native_effects.set_input_value(owner[1], "Exposure", value)
        return
    socket = node.inputs.get("Exposure")
    if socket:
        socket.default_value = value


def developer_exposure(node):
    meter = node.node_tree.nodes.get("Meter Offset") if node.node_tree else None
    return node.exposure + (meter.inputs[1].default_value if meter else 0.0)


def _update_grade(node, context):
    tree = node.node_tree
    if not tree:
        return
    nodes = tree.nodes
    if not nodes.get("Meter Offset"):
        return
    measured = 0.0
    if node.auto_exposure != "AS_SHOT":
        measured = metering.exposure_ev(node.auto_exposure, _meter_image(node),
                                        spot=(*node.spot_center, node.spot_radius))
    nodes["Meter Offset"].inputs[1].default_value = measured


def _active_photo(context):
    if context and context.scene and hasattr(context.scene, "blender_raw"):
        lib = catalog.library(context)
        if 0 <= lib.active_index < len(lib.photos):
            return lib.photos[lib.active_index]
    return None


def _bind_node(node, photo):
    node.photo_uid = photo.uid
    tree = node.node_tree
    tree[DECODE_TAG] = photo.uid
    tree.nodes["Decoded Image"].image = photo.image


def restore_raw_group(node):
    """Restore a missing private group without resetting the node's RAW recipe."""
    if node.bl_idname not in {READER_TYPE, DEVELOPER_TYPE} or node.node_tree:
        return False
    if node.bl_idname == READER_TYPE:
        node.node_tree = _create_reader()
        photo = catalog.lookup_photo(None, node.photo_uid)
        if photo:
            _bind_node(node, photo)
    else:
        node.node_tree = _create_pipeline()
    return True


def _restore_developer_preview(node, photo):
    # Several orphan groups may contain the same original with different WB,
    # profiles or decoders. Only the recipe's exact cache key identifies the pixels.
    if photo and uses_camera_adjustments(node):
        try:
            req = catalog.request(photo, "preview", recipe=node)
            image = next((image for image in bpy.data.images if not image.library
                          and image.get("photo_editor_decode_cache") == req["key"]
                          and (image.packed_file or image.has_data or Path(bpy.path.abspath(image.filepath)).is_file())), None)
            if image and photo.image:
                apply_developer_image(node, photo, image)
                return
            if photo.image and Path(req["output"]).is_file():
                catalog.apply_developer_image(photo, node, req)
                return
        except (OSError, ValueError, RuntimeError) as exc:
            node.error = str(exc)
        node["recipe_dirty"] = True
        if not bpy.app.background:
            from . import jobs
            jobs.developer_later(photo, node)
    _update_grade(node, None)


def finish_raw_recovery(tree, nodes):
    """Restore calibration and meter the reconstructed input after replaying the saved links."""
    repaired = set(nodes)
    for node in tree.nodes:
        if node.bl_idname != DEVELOPER_TYPE or not node.node_tree:
            continue
        if node in repaired:
            _restore_developer_preview(node, developer_source(node))
        elif _upstream_source(node) in repaired:
            photo = developer_source(node)
            developed = node.node_tree.nodes.get("Developed RAW")
            if developed and developed.image and photo and photo.uid == node.photo_uid:
                apply_developer_image(node, photo, developed.image)
            else:
                _update_grade(node, None)


def _pick_button(layout, photo, node, text):
    layout.operator_context = "INVOKE_DEFAULT"
    op = layout.operator("blender_raw.pick_neutral", text=text, icon="EYEDROPPER")
    op.photo_uid = photo.uid
    op.tree_name = node.id_data.name
    op.node_name = node.name


_copying_recipe = False
_dirty_trees = set()


def _recipe_changed(node, context):
    if _copying_recipe or not node.node_tree or not node.node_tree.nodes.get("Calibration"):
        return
    photo = developer_source(node)
    node.error = ""
    if not photo or not uses_camera_adjustments(node):
        apply_developer_image(node, photo, None)
    else:
        node["recipe_dirty"] = True
        if not bpy.app.background:
            from . import jobs
            jobs.developer_later(photo, node)


def _input_changed(node, context):
    if node.camera_input != "DCP" and catalog.get_look(node) == 2:
        node.look = "NONE"
    _recipe_changed(node, context)


def _legacy_profile_changed(node, context):
    catalog.set_camera_input(node, 0)
    _recipe_changed(node, context)


def _developer_profile_changed(node, context):
    node["profile_absolute"] = str(catalog.resolved(node.profile_path)) if node.profile_path else ""
    _recipe_changed(node, context)


def _developer_lut_changed(node, context):
    catalog.remember_lut_path(node)
    _recipe_changed(node, context)


_RECIPE_FIELDS = ("white_balance", "apple_white_balance", "apple_lens_correction",
                  "temperature", "tint", "neutral_x", "neutral_y",
                  "profile_mode", "profile_path", "use_dcp_look", "use_dcp_curve", "profile_blob",
                  "lut_path", "lut_space", "lut_blob")
_RECIPE_PATH_KEYS = ("legacy_sample_frame", "profile_absolute", "embedded_profile_path", "lut_absolute")


def reset_developer(node, context):
    for field in (*_RECIPE_FIELDS, "exposure", "auto_exposure", "spot_center", "spot_radius"):
        node.property_unset(field)
    for key in _RECIPE_PATH_KEYS:
        if key in node:
            del node[key]
    catalog.set_camera_input(node, catalog.DEFAULT_INPUT_NUMBER)
    node.exposure = 0.0
    node.look = 'NONE'  # Schedule a single calibration after the complete recipe is in place.
    _update_grade(node, context)


def _copy_photo_recipe(node, photo):
    global _copying_recipe
    _copying_recipe = True
    try:
        for field in _RECIPE_FIELDS:
            setattr(node, field, getattr(photo, field))
        if photo.camera_input != "LEGACY":
            # A saved Mac choice can be copied on Windows, where a fresh
            # Developer's menu only offers decoders that can run locally.
            catalog.set_camera_input(node, catalog.get_camera_input(photo))
            # The previous BaseLook is available only on records already using it.
            catalog.set_look(node, catalog.get_look(photo))
        for field in _RECIPE_PATH_KEYS:
            if field in photo:
                node[field] = photo[field]
    finally:
        _copying_recipe = False


def _sync_developer_sources(tree_ids=None):
    if tree_ids is not None:
        # Reordering a native effect changes sources inside all of its nested groups.
        pending = [tree for tree in bpy.data.node_groups if tree.session_uid in tree_ids]
        tree_ids = set(tree_ids)
        while pending:
            for node in pending.pop().nodes:
                child = getattr(node, 'node_tree', None)
                if child and not child.library and child.session_uid not in tree_ids:
                    tree_ids.add(child.session_uid)
                    pending.append(child)
    for tree in bpy.data.node_groups:
        if (tree.library or tree.bl_idname != "CompositorNodeTree"
                or (tree_ids is not None and tree.session_uid not in tree_ids)):
            continue
        for node in tree.nodes:
            if node.bl_idname != NODE_TYPE or not node.inputs or not node.node_tree:
                continue
            photo = developer_source(node)
            uid = photo.uid if photo else ""
            if node.photo_uid != uid or node.get("refresh_input"):
                node.photo_uid = uid
                if "refresh_input" in node:
                    del node["refresh_input"]
                apply_developer_image(node, photo, None)
                _recipe_changed(node, None)
            elif photo:
                developed = node.node_tree.nodes.get("Developed RAW")
                if developed and developed.image:
                    is_direct = _direct_reader_image(node, photo)
                    calibration = node.node_tree.nodes["Calibration"].inputs[0]
                    if calibration.is_linked and (calibration.links[0].from_node == developed) != bool(is_direct):
                        apply_developer_image(node, photo, developed.image)
    return None


def _flush_source_updates():
    pending = _dirty_trees.copy()
    _dirty_trees.clear()
    _sync_developer_sources(pending)
    for scene in bpy.data.scenes:
        if scene.get(STUDIO_TAG) and scene.get(PHOTO_TAG):
            photo = catalog.lookup_photo(scene, scene[PHOTO_TAG])
            if photo and any(tree.session_uid in pending for tree in photo_trees(photo)):
                _set_dimensions(scene, photo)
    return 0.01 if _dirty_trees else None


def _queue_source_sync(tree):
    if bpy.app.background:
        return
    _dirty_trees.add(tree.session_uid)
    if not bpy.app.timers.is_registered(_flush_source_updates):
        bpy.app.timers.register(_flush_source_updates, first_interval=0.01)


class BR_NODE_raw_reader(bpy.types.CompositorNodeCustomGroup):
    bl_idname = READER_TYPE
    bl_label = "Raw Reader"
    bl_icon = "IMAGE_DATA"
    bl_description = "Read the original RAW photo using its as-shot camera settings"
    bl_width_default = 200
    photo_uid: StringProperty(options={"HIDDEN"})

    @classmethod
    def poll(cls, tree):
        return tree.bl_idname == "CompositorNodeTree"

    def init(self, context):
        self.node_tree = _create_reader()
        photo = catalog.lookup_photo(None, self.id_data.get(PHOTO_TAG)) or _active_photo(context)
        if photo:
            _bind_node(self, photo)
            if not photo.image and not bpy.app.background and context and context.scene == photo.scene:
                from . import jobs
                jobs.preview_later(photo, immediate=True)

    def copy(self, source):
        from . import adjustments
        if adjustments.borrowing_groups():
            return
        # The clipboard's runtime tree borrows its node-group references.
        if not self.id_data.is_runtime_data and source.node_tree:
            self.node_tree = source.node_tree.copy()

    def update(self):
        _queue_source_sync(self.id_data)

    def draw_buttons(self, context, layout):
        photo = catalog.lookup_photo(None, self.photo_uid)
        if photo:
            layout.label(text=photo.name, icon="IMAGE_DATA")
            if catalog.reader_needs_libraw(photo):
                layout.label(text="Apple RAW requires Apple Silicon", icon="INFO")
                op = layout.operator("blender_raw.reader_use_libraw", text="Use LibRaw for Reader")
                op.photo_uid = photo.uid
        else:
            layout.label(text="No photo bound", icon="IMAGE_DATA")

    def draw_buttons_ext(self, context, layout):
        self.draw_buttons(context, layout)
        op = layout.operator("blender_raw.bind_photo", text="Use Active Photo")
        op.tree_name, op.node_name = self.id_data.name, self.name


class BR_NODE_raw_develop(bpy.types.CompositorNodeCustomGroup):
    bl_idname = NODE_TYPE
    bl_label = "Raw Developer"
    bl_icon = "IMAGE_DATA"
    bl_description = "Adjust the connected image with independent camera, exposure and color settings"
    bl_width_default = 235
    bl_width_max = 450

    photo_uid: StringProperty(options={"HIDDEN"})
    instance_uid: StringProperty(options={"HIDDEN"})
    error: StringProperty(options={"SKIP_SAVE"})
    auto_exposure: EnumProperty(name="Auto Exposure", items=metering.STORED_MODES, default='AS_SHOT',
                                options={'HIDDEN', 'ANIMATABLE'}, update=_update_grade)
    as_shot: BoolProperty(name="As Shot", description="Use the recorded exposure with manual compensation",
                          get=lambda node: node.auto_exposure == 'AS_SHOT', set=metering.set_as_shot,
                          options={'SKIP_SAVE'})
    meter_mode: EnumProperty(name="Meter", items=metering.mode_items, get=metering.get_mode, set=metering.set_mode,
                             options={'SKIP_SAVE'})
    spot_center: FloatVectorProperty(name="Spot Center", description="Meter center in normalized image coordinates, from the top left",
                                     size=2, min=0, max=1, default=(0.5, 0.5), update=_update_grade)
    spot_radius: FloatProperty(name="Spot Radius", description="Circle radius as a percentage of the shorter image dimension",
                               subtype="PERCENTAGE", min=0.5, max=50, default=30, precision=1, update=_update_grade)
    white_balance: EnumProperty(name="White Balance", items=[
        ("CAMERA", "As Shot", "Use the camera's recorded white balance"),
        ("AUTO", "Auto", "Estimate the white balance"),
        ("CUSTOM", "Custom", "Set the color temperature and tint"),
        ("SAMPLE", "Neutral Sample", "Use the neutral patch picked from the RAW"),
    ], update=_recipe_changed)
    apple_white_balance: EnumProperty(name="White Balance", items=catalog.APPLE_WB, default="CAMERA", update=_recipe_changed)
    applied_temperature: FloatProperty(name='Applied Temperature (K)', precision=0, options={'SKIP_SAVE', 'HIDDEN'},
                                       get=lambda node: (catalog.applied_white_balance(node) or (0, 0))[0])
    applied_tint: FloatProperty(name='Applied Tint', precision=1, options={'SKIP_SAVE', 'HIDDEN'},
                                get=lambda node: (catalog.applied_white_balance(node) or (0, 0))[1])
    apple_lens_correction: BoolProperty(name="Vendor Lens Correction", description="Apply Apple's correction when the camera and lens support it", update=_recipe_changed)
    temperature: FloatProperty(name="Temperature", min=2000, max=50000, soft_max=25000, default=5500, precision=0, update=_recipe_changed)
    tint: FloatProperty(name="Tint", min=-150, max=150, soft_min=-100, soft_max=100, precision=1, update=_recipe_changed)
    neutral_x: FloatProperty(name="Sample X", min=0, max=1, default=0.5, update=_recipe_changed)
    neutral_y: FloatProperty(name="Sample Y", min=0, max=1, default=0.5, update=_recipe_changed)
    camera_input: EnumProperty(name="Camera Input", items=catalog.camera_input_items, default=catalog.DEFAULT_INPUT_NUMBER,
                               get=catalog.get_camera_input, set=catalog.set_camera_input, update=_input_changed)
    look: EnumProperty(name="Look", items=catalog.look_items, default=1, get=catalog.get_look, set=catalog.set_look, update=_recipe_changed)
    lut_path: StringProperty(name="LUT File", description="A 3D color lookup table in .cube format",
                             subtype="FILE_PATH", options={"PATH_SUPPORTS_BLEND_RELATIVE"}, update=_developer_lut_changed)
    lut_space: EnumProperty(name="LUT Space", items=catalog.LUT_SPACES, default="SRGB", update=_recipe_changed)
    lut_blob: PointerProperty(type=bpy.types.Text)
    profile_mode: EnumProperty(name="Camera Profile", items=catalog.PROFILE_MODES,
                               default="EMBEDDED", update=_legacy_profile_changed)
    profile_path: StringProperty(name="Camera Profile File", subtype="FILE_PATH", update=_developer_profile_changed)
    profile_blob: PointerProperty(type=bpy.types.Text)
    use_dcp_look: BoolProperty(name="DCP Color Tables", update=_recipe_changed)
    use_dcp_curve: BoolProperty(name="DCP Tone Curve", update=_recipe_changed)
    exposure: FloatProperty(name="Exposure", description="Exposure compensation in stops",
                            min=-12, max=12, soft_min=-5, soft_max=5,
                            precision=2, get=_exposure_value, set=_set_exposure_value, update=_update_grade)

    @classmethod
    def poll(cls, tree):
        return tree.bl_idname == "CompositorNodeTree"

    def init(self, context):
        self.instance_uid = uuid.uuid4().hex
        self.camera_input = catalog.DEFAULT_CAMERA_INPUT
        self.node_tree = _create_pipeline()

    def update(self):
        _queue_source_sync(self.id_data)

    def copy(self, source):
        from . import adjustments
        if adjustments.borrowing_groups():
            return
        if self.id_data.is_runtime_data:
            return
        self.instance_uid = uuid.uuid4().hex
        if source.node_tree:
            self.node_tree = source.node_tree.copy()
        self["refresh_input"] = True
        _queue_source_sync(self.id_data)

    def draw_controls(self, layout, *, show_source=False, effect=None):
        photo = developer_source(self) or developer_source(self, mode='RENDER')
        if show_source:
            if photo:
                layout.label(text=photo.name, icon="IMAGE_DATA")
            else:
                layout.label(text="Connect a Raw Reader for camera settings", icon="INFO")
        catalog.draw_develop(layout, self, photo)
        layout.separator()
        spot_meter.draw_controls(layout, self, photo, effect=effect)

    def draw_buttons(self, context, layout):
        self.draw_controls(layout)
        film_node.draw_insert(layout, self)

    def draw_buttons_ext(self, context, layout):
        self.draw_controls(layout, show_source=True)
        film_node.draw_insert(layout, self)


class BR_OT_reader_use_libraw(bpy.types.Operator):
    bl_idname = "blender_raw.reader_use_libraw"
    bl_label = "Use LibRaw for Reader"
    bl_description = "Switch this photo's Raw Readers to LibRaw. Each Developer keeps its own camera input"
    bl_options = {"REGISTER", "UNDO"}
    photo_uid: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        photo = catalog.lookup_photo(context.scene, self.photo_uid)
        if not photo or photo.id_data.library or not catalog.reader_needs_libraw(photo):
            return {"CANCELLED"}
        photo["reader_camera_input"] = catalog.DEFAULT_CAMERA_INPUT
        from . import jobs
        jobs.preview_later(photo, immediate=True)
        return {"FINISHED"}


class BR_OT_bind_photo(bpy.types.Operator):
    bl_idname = "blender_raw.bind_photo"
    bl_label = "Use Active Photo"
    bl_description = "Bind the active Photo Gallery image to this Raw Reader"
    bl_options = {"REGISTER", "UNDO"}
    tree_name: StringProperty(options={"HIDDEN"})
    node_name: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        tree = bpy.data.node_groups.get(self.tree_name)
        node = tree.nodes.get(self.node_name) if tree else None
        photo = _active_photo(context)
        if not photo or not node or node.bl_idname != READER_TYPE:
            self.report({"WARNING"}, "Select a photo in Photo Gallery first")
            return {"CANCELLED"}
        try:
            if not photo.image:
                catalog.ensure_full(context, photo)
            _bind_node(node, photo)
            _queue_source_sync(tree)
        except (ValueError, RuntimeError, OSError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


def _inside(region, x, y):
    return (region.x <= x < region.x + region.width
            and region.y <= y < region.y + region.height)


def _sample_position(area, mouse_x, mouse_y):
    canvas = next((r for r in area.regions if r.type == "WINDOW"), None)
    if not canvas or not _inside(canvas, mouse_x, mouse_y):
        return None
    # The visible sidebar and toolbar may overlap the drawing region.
    if any(r.type != "WINDOW" and r.width > 1 and r.height > 1
           and _inside(r, mouse_x, mouse_y) for r in area.regions):
        return None
    u, v = canvas.view2d.region_to_view(mouse_x - canvas.x, mouse_y - canvas.y)
    if 0.0 <= u <= 1.0 and 0.0 <= v <= 1.0:
        return u, 1.0 - v  # Blender uses a bottom-left image origin.
    return None


def _has_spatial_edit(tree):
    """A warped Viewer has no general one-to-one mapping back to sensor pixels."""
    spatial = {crop_node.TYPE, "CompositorNodeCrop", "CompositorNodeFlip", "CompositorNodeRotate",
               "CompositorNodeScale", "CompositorNodeTranslate", "CompositorNodeTransform",
               "CompositorNodeCornerPin", "CompositorNodePlaneTrackDeform", "CompositorNodeMapUV",
               "CompositorNodeDisplace", "CompositorNodeLensdist", "CompositorNodeMovieDistortion"}
    viewers = [n for n in tree.nodes if n.bl_idname == "CompositorNodeViewer"]
    pending = list(viewers)
    seen = set()
    while pending:
        node = pending.pop()
        if node.as_pointer() in seen:
            continue
        seen.add(node.as_pointer())
        if not node.mute and node.bl_idname in spatial:
            return True
        if node.bl_idname == READER_TYPE:
            continue
        child = None if node.bl_idname in {NODE_TYPE, *film_node.FILM_TYPES} else getattr(node, "node_tree", None)
        if child and not node.mute:
            pending.extend(n for n in child.nodes if n.type == "GROUP_OUTPUT" and n.is_active_output)
        for socket in node.inputs:
            pending.extend(link.from_node for link in socket.links)
    return False


class BR_OT_pick_neutral(bpy.types.Operator):
    bl_idname = "blender_raw.pick_neutral"
    bl_label = "Pick Neutral"
    bl_description = "Click a neutral gray area of the original to set the photo's white balance"
    bl_options = {"UNDO", "BLOCKING"}
    photo_uid: StringProperty(options={"HIDDEN"})
    tree_name: StringProperty(options={"HIDDEN"})
    node_name: StringProperty(options={"HIDDEN"})

    def invoke(self, context, event):
        photo = catalog.lookup_photo(context.scene, self.photo_uid)
        if not photo or not photo.image or not all(photo.image.size):
            self.report({"WARNING"}, "Wait for the photo preview before picking a neutral point")
            return {"CANCELLED"}
        source = photo.image
        if self.tree_name:
            tree = bpy.data.node_groups.get(self.tree_name)
            node = tree.nodes.get(self.node_name) if tree else None
            if not node or node.bl_idname != NODE_TYPE or developer_source(node) != photo:
                return {'CANCELLED'}
            developed = node.node_tree.nodes.get('Developed RAW')
            if developed and developed.image:
                source = developed.image
        targets = []
        for area in context.window.screen.areas:
            if area.type != "IMAGE_EDITOR":
                continue
            shown = area.spaces.active.image
            if shown and (shown == source or shown.type == "COMPOSITING" or
                          (shown == photo.image and tuple(source.size) == tuple(photo.image.size))):
                targets.append(area)
        if not targets:
            self.report({"WARNING"}, "Show the photo or its Viewer in an Image Editor first")
            return {"CANCELLED"}
        self._window = context.window
        self._area = max(targets, key=lambda a: a.width * a.height)
        space = self._area.spaces.active
        self._shown = space.image
        if self._shown.type == "COMPOSITING":
            scene = context.window.scene
            if scene.get(STUDIO_TAG) and scene.get(PHOTO_TAG) != photo.uid:
                self.report({"WARNING"}, "The Viewer is displaying a different photo")
                return {"CANCELLED"}
            effect = scene.compositor_effects.active
            if (tuple(self._shown.size) != tuple(source.size)
                    or (effect and effect.node_group and _has_spatial_edit(effect.node_group))):
                self.report({"WARNING"}, "Connect Viewer before geometric edits to pick the matching RAW pixel")
                return {"CANCELLED"}
        self._area.header_text_set("Click a neutral area in this view. Esc cancels.")
        self._area.tag_redraw()
        self._window.cursor_modal_set("EYEDROPPER")
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def cancel(self, context):
        self._window.cursor_modal_restore()
        try:
            if self._area.type == "IMAGE_EDITOR":
                self._area.header_text_set(None)
                self._area.tag_redraw()
        except ReferenceError:
            pass  # The area or data-block was removed while the picker was active.

    def modal(self, context, event):
        if event.type in {"ESC", "RIGHTMOUSE", "WINDOW_DEACTIVATE"}:
            self.cancel(context)
            return {"CANCELLED"}
        if event.type == "LEFTMOUSE" and event.value == "PRESS":
            try:
                if self._area.type != "IMAGE_EDITOR" or self._area.spaces.active.image != self._shown:
                    self.cancel(context)
                    return {"CANCELLED"}
                position = _sample_position(self._area, event.mouse_x, event.mouse_y)
            except ReferenceError:
                self.cancel(context)
                return {"CANCELLED"}
            if position is None:
                return {"RUNNING_MODAL"}
            self.cancel(context)
            photo = catalog.lookup_photo(context.scene, self.photo_uid)
            if not photo:
                return {"CANCELLED"}
            if self.tree_name:
                tree = bpy.data.node_groups.get(self.tree_name)
                recipe = tree.nodes.get(self.node_name) if tree else None
                if not recipe or recipe.bl_idname != NODE_TYPE or developer_source(recipe) != photo:
                    return {"CANCELLED"}
            else:
                recipe = photo  # Retain the 0.5 operator entry point for saved scripts.
            if "legacy_sample_frame" in recipe:
                del recipe["legacy_sample_frame"]
            recipe.neutral_x, recipe.neutral_y = position
            if catalog.uses_apple_input(recipe):
                recipe.apple_white_balance = "SAMPLE"
            else:
                recipe.white_balance = "SAMPLE"
            return {"FINISHED"}
        if event.type in {"WHEELUPMOUSE", "WHEELDOWNMOUSE", "MIDDLEMOUSE",
                          "TRACKPADPAN", "TRACKPADZOOM"}:
            return {"PASS_THROUGH"}
        return {"RUNNING_MODAL"}


def _add_node_menu(self, context):
    if context.space_data and context.space_data.tree_type == "CompositorNodeTree":
        types = [(READER_TYPE, "Raw Reader"), (NODE_TYPE, "Raw Developer")]
        if not spektrafilm_engine.backend_error():
            types.append((film_node.FILM_TYPE, "Spektrafilm Lite"))
        types.extend(((film_node.OFX_TYPE, "Spektrafilm OFX"), (crop_node.TYPE, "Crop & Perspective")))
        for node_type, label in types:
            op = self.layout.operator("node.add_node", text=label, icon="IMAGE_DATA")
            op.type = node_type
            op.use_transform = True


def ensure_photo_scene(context, photo):
    """Create the persistent native compositor stack owned by one photograph."""
    if photo.scene:
        return photo.scene
    from . import native_effects
    scene = bpy.data.scenes.new("Photo · " + photo.name)
    scene[STUDIO_TAG] = True
    scene[PHOTO_TAG] = photo.uid
    scene["photo_editor_schema"] = 2
    scene.render.use_sequencer = False
    scene.render.use_compositing = True
    scene.render.resolution_percentage = 100
    scene.render.pixel_aspect_x = scene.render.pixel_aspect_y = 1.0
    photo.scene = scene
    try:
        native_effects.create_effect(scene, READER_TYPE, photo=photo)
        developer = native_effects.create_effect(scene, DEVELOPER_TYPE, photo=photo)
        scene.compositor_effects.active = developer
        _set_dimensions(scene, photo)
        film_display.sync(scene)
    except Exception:
        photo.scene = None
        bpy.data.scenes.remove(scene)
        raise
    return scene


def photo_trees(photo):
    from . import scene_pipeline
    return scene_pipeline.photo_trees(photo)


def _fit_compositor_device(scene):
    render = scene.render
    longest_side = max(render.resolution_x, render.resolution_y) * render.resolution_percentage // 100
    if longest_side > _GPU_COMPOSITOR_LIMIT:
        if render.compositor_device == 'GPU':
            scene[_AUTO_CPU_TAG] = True
            render.compositor_device = 'CPU'
    elif scene.get(_AUTO_CPU_TAG):
        render.compositor_device = 'GPU'
        del scene[_AUTO_CPU_TAG]


def restore_preview_devices():
    """Handle saved darkrooms before Blender schedules the first Viewer update."""
    for scene in bpy.data.scenes:
        if scene.get(STUDIO_TAG) and not scene.library:
            _fit_compositor_device(scene)
            film_display.sync(scene)


def _set_dimensions(scene, photo, *, for_export=False):
    from . import scene_pipeline
    image = photo.image
    developers = [node for _, node, _ in scene_pipeline.upstream(
        scene, mode='RENDER' if for_export else 'PREVIEW')
        if node.bl_idname == NODE_TYPE and node.photo_uid == photo.uid]
    variants = [node.node_tree.nodes.get("Developed RAW") for node in developers if node.node_tree]
    images = [src.image for src in variants if src and src.image]
    if images and len({tuple(item.size) for item in images}) == 1:
        image = images[0]
    if image and all(image.size):
        scene.render.resolution_x, scene.render.resolution_y = crop_node.terminal_dimensions(
            scene, *image.size, 'RENDER' if for_export else 'PREVIEW')
        scene.render.resolution_percentage = 100
        _fit_compositor_device(scene)


_viewer_switches = {}
_viewer_retries = {}


@persistent
def _clear_viewer_switches(*_args):
    _viewer_switches.clear()
    _viewer_retries.clear()
    if bpy.app.timers.is_registered(_settle_viewer_switches):
        bpy.app.timers.unregister(_settle_viewer_switches)


def check_viewer_pixels(scene, viewer, *, empty=None):
    """Retry an interrupted native preview once without changing its graph."""
    uid = scene.get(PHOTO_TAG)
    if _viewer_retries.get(scene.session_uid) != (uid, 0) or empty is False:
        return
    if empty is None and viewer and viewer.has_data and min(viewer.size) > 0:
        return
    photo = catalog.lookup_photo(scene, uid)
    if not photo or not photo.image or not photo.image.has_data:
        return
    _viewer_retries[scene.session_uid] = (uid, 1)
    for effect in scene.compositor_effects:
        if effect.node_group and effect.enable_for_preview:
            effect.node_group.update_tag()


def _schedule_viewer_check(key, uid):
    _viewer_switches[key] = (uid, time.monotonic() + .65)
    if not bpy.app.timers.is_registered(_settle_viewer_switches):
        bpy.app.timers.register(_settle_viewer_switches, first_interval=.65)


def _settle_viewer_switches():
    from . import jobs
    if bpy.app.is_job_running("COMPOSITE") or bpy.app.is_job_running("RENDER"):
        return .12
    now = time.monotonic()
    for key, (uid, due) in tuple(_viewer_switches.items()):
        if now < due or jobs.developing(uid):
            continue
        del _viewer_switches[key]
        scene = next((s for s in bpy.data.scenes if s.session_uid == key), None)
        if not scene or scene.get(PHOTO_TAG) != uid:
            continue
        viewer = next((area.spaces.active.image for win in bpy.context.window_manager.windows
                       if win.scene == scene for area in win.screen.areas if area.type == 'IMAGE_EDITOR'
                       and area.spaces.active.image and area.spaces.active.image.type == 'COMPOSITING'), None)
        if viewer:
            check_viewer_pixels(scene, viewer)
    return .12 if _viewer_switches else None


def viewer_image():
    viewer = next((image for image in bpy.data.images if image.type == 'COMPOSITING'), None)
    if viewer is None:
        # Initialize Blender's native Viewer image without persisting a Viewer node.
        tree = bpy.data.node_groups.new(".Photo Viewer Initialization", "CompositorNodeTree")
        tree.nodes.new("CompositorNodeViewer")
        viewer = next((image for image in bpy.data.images if image.type == 'COMPOSITING'), None)
        bpy.data.node_groups.remove(tree)
    return viewer


def _refresh_developer(node, photo, preview):
    nodes = node.node_tree.nodes
    developed = nodes.get("Developed RAW")
    image = developed.image if developed else None
    if not uses_camera_adjustments(node):
        if image:
            apply_developer_image(node, photo, None)
        elif node.auto_exposure != "AS_SHOT":
            _update_grade(node, None)
        return

    same_base = image is not None and nodes["As Shot RAW"].image == photo.image
    same_recipe = not node.get("recipe_dirty")
    embedded_profile = node.profile_blob and node.get("embedded_profile_path") == node.profile_path
    if preview and (node.camera_input != "LEGACY" or node.profile_mode == "AUTO"
                    or (catalog.has_custom_input(node) and not embedded_profile)):
        # Both a catalog update and an external edit can invalidate a saved variant.
        try:
            expected = catalog.request(photo, "preview", recipe=node)["key"]
        except (OSError, ValueError) as exc:
            node.error = str(exc)
            return  # Retain the most recent successful calibration until relinked.
        same_recipe = same_recipe and image is not None and Path(image.filepath).stem == expected
    if same_base and same_recipe:
        if catalog.ensure_cache_pixels(image):
            # A pre-0.15.5 project can bind the same disk EXR via both a short
            # Windows path and its long spelling. Keep distinct packed payloads.
            if (photo.image and image != photo.image and image.get("photo_editor_decode_cache") == photo.decode_key
                    and not image.packed_file and not photo.image.packed_file
                    and catalog.resolved(image.filepath) == catalog.resolved(photo.image.filepath)):
                apply_developer_image(node, photo, photo.image)
            node.error = ""
            return
    if not same_base:
        apply_developer_image(node, photo, None)
    node["recipe_dirty"] = True
    if preview:
        from . import jobs
        jobs.developer_later(photo, node)


def refresh_photo(photo, *, preview=None):
    """Called on the Blender main thread after an asynchronously decoded image is loaded."""
    for tree in bpy.data.node_groups:
        if not tree.library and tree.get(DECODE_TAG) == photo.uid:
            source = tree.nodes.get("Decoded Image")
            if source and source.image != photo.image:
                source.image = photo.image
    for scene in bpy.data.scenes:
        if scene.get(STUDIO_TAG) and scene.get(PHOTO_TAG) == photo.uid:
            _set_dimensions(scene, photo)
    if preview is None:
        preview = not bpy.app.background
    for node in developers_for(photo):
        _refresh_developer(node, photo, preview=preview)


def open_photo(context, photo):
    scene = ensure_photo_scene(context, photo)
    _set_dimensions(scene, photo)
    window = context.window
    if window:
        switched = window.scene != scene
        window.scene = scene
        viewer = viewer_image()
        for area in window.screen.areas:
            if area.type == 'IMAGE_EDITOR' and viewer:
                area.spaces.active.image = viewer
                viewer.use_view_as_render = True
            elif area.type == 'NODE_EDITOR' and area.ui_type == 'CompositorNodeTree':
                area.spaces.active.pin = False
            elif area.type == 'PROPERTIES' and area.spaces.active.context == 'COMPOSITOR':
                area.spaces.active.pin_id = None
            area.tag_redraw()
        from . import gallery, edited_preview
        gallery.finish_workspace_panels(window)
        if switched:
            # Scene browsing alone does not invalidate a previously evaluated
            # compositor. Reassigning the same active effect sends Blender's
            # native compositor update without changing the selected card.
            active = scene.compositor_effects.active
            if active:
                scene.compositor_effects.active = active
            edited_preview.fit_on_switch(context, scene, photo.uid)
            _viewer_retries[scene.session_uid] = (photo.uid, 0)
            if not bpy.app.background:
                _schedule_viewer_check(scene.session_uid, photo.uid)
    return scene


def replace_removed_photo(context, removed_uids, next_photo):
    """Switch affected windows, then unlink only the removed photo scenes."""
    from . import jobs
    removed = [scene for scene in bpy.data.scenes if not scene.library
               and scene.get(STUDIO_TAG) and scene.get(PHOTO_TAG) in removed_uids]
    replacement = ensure_photo_scene(context, next_photo) if next_photo else next(
        (scene for scene in bpy.data.scenes if scene.blender_raw.owner and not scene.library), None)
    if replacement is None:
        replacement = bpy.data.scenes.new("Photo Library")
        replacement.blender_raw.owner = True
    for win in context.window_manager.windows:
        if win.scene in removed:
            win.scene = replacement
            if next_photo:
                with context.temp_override(window=win):
                    open_photo(bpy.context, next_photo)
            else:
                for area in win.screen.areas:
                    if area.type == 'IMAGE_EDITOR' and area.spaces.active.image and area.spaces.active.image.type == 'COMPOSITING':
                        area.spaces.active.image = None
    for scene in removed:
        bpy.data.scenes.remove(scene)
    if next_photo:
        jobs.open_preview(next_photo)


def ensure_render_camera(scene):
    """Satisfy Blender 5.3's render preflight in an isolated compositor scene."""
    if scene.camera:
        return
    camera = bpy.data.cameras.new(".Photo Render Camera")
    camera.type = 'ORTHO'
    obj = bpy.data.objects.new(".Photo Render Camera", camera)
    scene.collection.objects.link(obj)
    scene.camera = obj
    scene.render.engine = 'BLENDER_WORKBENCH'
    scene.display.render_aa = 'OFF'
    scene.world = None


def _copy_output_values(source, target, skip=()):
    for prop in source.bl_rna.properties:
        if prop.is_readonly or prop.identifier in skip or prop.type in {'POINTER', 'COLLECTION'}:
            continue
        value = getattr(source, prop.identifier)
        setattr(target, prop.identifier, tuple(value) if getattr(prop, 'is_array', False) else value)


def _copy_output_settings(source, target):
    """Copy the dialog's image encoding without changing a photo's render scene."""
    # Format first: valid depth/channel choices depend on it. View transform
    # similarly determines which looks Blender accepts.
    target.media_type, target.file_format = source.media_type, source.file_format
    _copy_output_values(source, target, {'media_type', 'file_format'})
    for name in ('stereo_3d_format', 'display_settings', 'linear_colorspace_settings'):
        _copy_output_values(getattr(source, name), getattr(target, name))
    target.view_settings.view_transform = source.view_settings.view_transform
    _copy_output_values(source.view_settings, target.view_settings, {'view_transform'})
    if source.view_settings.use_curve_mapping:
        original, copied = source.view_settings.curve_mapping, target.view_settings.curve_mapping
        copied.initialize()
        _copy_output_values(original, copied)
        for before, after in zip(original.curves, copied.curves):
            for point in list(after.points)[1:-1]:
                after.points.remove(point)
            after.points[0].location = before.points[0].location
            after.points[-1].location = before.points[-1].location
            for point in list(before.points)[1:-1]:
                after.points.new(*point.location)
            for a, b in zip(before.points, after.points):
                b.handle_type = a.handle_type
        copied.update()


def export_scene(settings_scene, format_override="", *, output_settings=None):
    """Copy the native image and color settings once in the isolated batch process."""
    scene = settings_scene.copy()
    scene.name = ".Blender Photo Editor Export"
    # Baking temporarily rewires processing graphs. Isolate them before collecting
    # film passes so a depsgraph ownership handler cannot replace a group mid-bake.
    from . import native_effects
    for effect in scene.compositor_effects:
        if effect.node_group and native_effects._has_processing(effect.node_group):
            effect.node_group = native_effects._clone_group(effect.node_group)
    scene.blender_raw.owner = False
    scene.blender_raw.photos.clear()
    scene.render.use_sequencer = False
    scene.render.use_compositing = True
    scene.render.use_border = False
    scene.render.use_multiview = False
    scene.render.pixel_aspect_x = scene.render.pixel_aspect_y = 1
    scene.camera = None
    for obj in list(scene.collection.objects):
        scene.collection.objects.unlink(obj)
    for collection in list(scene.collection.children):
        scene.collection.children.unlink(collection)
    ensure_render_camera(scene)
    if output_settings is not None:
        _copy_output_settings(output_settings, scene.render.image_settings)
    if format_override:
        scene.render.image_settings.media_type = "IMAGE"
        scene.render.image_settings.file_format = format_override
    return scene


def export_photo(context, photo, path, scene):
    """Render the photo's actual Group Output to a new file, without a camera or UI."""
    from . import film_bake
    if not photo.image or not all(photo.image.size):
        raise ValueError(f"No developed image available for {photo.name}")
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(destination)
    _set_dimensions(scene, photo, for_export=True)
    render = scene.render
    if bpy.app.background:
        render.compositor_device = "CPU"
    render.use_file_extension = False
    with TemporaryDirectory(prefix=".blender-photo-editor-", dir=destination.parent) as folder:
        temporary = Path(folder) / destination.name
        with film_bake.prepared(scene, None, folder) as has_film:
            settings = render.image_settings
            full_chroma = (has_film and settings.file_format == 'JPEG'
                           and settings.quality == 100 and settings.color_mode == 'RGB')
            rendered = temporary
            if full_chroma:
                image_dir = Path(folder) / 'film-jpeg'
                image_dir.mkdir()
                rendered = image_dir / 'display-16.png'
            render.filepath = str(rendered)
            try:
                if full_chroma:
                    png_compression = settings.compression
                    settings.file_format, settings.color_depth = 'PNG', '16'
                    settings.compression = 0
                result = bpy.ops.render.render(scene=scene.name, write_still=True)
            finally:
                if full_chroma:
                    settings.compression = png_compression
                    settings.file_format = 'JPEG'
            if 'FINISHED' not in result or not rendered.is_file():
                raise RuntimeError(f"Blender did not write {photo.name}")
            if full_chroma:
                from . import film_jpeg
                film_jpeg.encode(rendered, temporary)
        if os.name == "nt":
            # Windows rename refuses to replace a file created by another exporter.
            os.rename(temporary, destination)
        else:
            os.link(temporary, destination)
    return str(destination)


_held_for_save = []


@persistent
def _migrate_exposure_controls(*_args):
    """Move saved input controls into the Developer sections, retaining their RNA paths."""
    from . import native_effects
    for tree in bpy.data.node_groups:
        if tree.bl_idname != 'CompositorNodeTree' or not tree.is_editable:
            continue
        if tree.get(native_effects.KIND_TAG) == DEVELOPER_TYPE:
            socket = tree.interface.items_tree.get('Exposure')
            if socket and socket.item_type == 'SOCKET' and socket.in_out == 'INPUT' and not socket.hide_in_modifier:
                socket.hide_in_modifier = True
        for node in tree.nodes:
            if node.bl_idname != DEVELOPER_TYPE or not node.node_tree or not node.node_tree.is_editable:
                continue
            socket = node.node_tree.interface.items_tree.get('Exposure')
            if socket and socket.item_type == 'SOCKET' and socket.in_out == 'INPUT' and not socket.hide_value:
                socket.hide_value = True


@persistent
def _retain_legacy_groups(_filepath):
    # Earlier clipboard copies could reduce a still-attached group's users to zero.
    # Blender omits such IDs when saving a project or a preview-worker snapshot.
    missing_users = {g for g in bpy.data.node_groups if g.users == 0 and not g.library}
    if not missing_users:
        return
    for tree in bpy.data.node_groups:
        if tree.bl_idname != "CompositorNodeTree" or tree.library:
            continue
        for node in tree.nodes:
            if node.bl_idname in (READER_TYPE, DEVELOPER_TYPE, crop_node.TYPE, levels_node.TYPE, *film_node.FILM_TYPES):
                group = node.node_tree
                if group in missing_users and not group.use_extra_user:
                    group.use_extra_user = True
                    _held_for_save.append(group)


@persistent
def _release_legacy_groups(_filepath):
    for group in _held_for_save:
        group.use_extra_user = False
    _held_for_save.clear()


def register():
    film_display.register()
    film_node.register()
    bpy.utils.register_class(BR_NODE_raw_reader)
    bpy.utils.register_class(BR_NODE_raw_develop)
    bpy.utils.register_class(BR_MT_preview_status)
    bpy.types.NODE_MT_context_menu.append(_preview_status_menu)
    bpy.utils.register_class(BR_OT_bind_photo)
    bpy.utils.register_class(BR_OT_reader_use_libraw)
    bpy.utils.register_class(BR_OT_pick_neutral)
    spot_meter.register()
    bpy.types.NODE_MT_compositor_node_add_all.append(_add_node_menu)
    bpy.app.handlers.save_pre.append(_retain_legacy_groups)
    bpy.app.handlers.save_post.append(_release_legacy_groups)
    bpy.app.handlers.save_post_fail.append(_release_legacy_groups)
    bpy.app.handlers.load_pre.append(_clear_viewer_switches)
    bpy.app.handlers.load_post.append(_migrate_exposure_controls)
    bpy.app.handlers.blend_import_post.append(_migrate_exposure_controls)
    # Extension enable can run against a file that is already open, inside RestrictBlend.
    if not bpy.app.background:
        bpy.app.timers.register(_migrate_exposure_controls)


def unregister():
    if bpy.app.timers.is_registered(_migrate_exposure_controls):
        bpy.app.timers.unregister(_migrate_exposure_controls)
    bpy.app.handlers.blend_import_post.remove(_migrate_exposure_controls)
    bpy.app.handlers.load_post.remove(_migrate_exposure_controls)
    bpy.app.handlers.save_pre.remove(_retain_legacy_groups)
    bpy.app.handlers.save_post.remove(_release_legacy_groups)
    bpy.app.handlers.save_post_fail.remove(_release_legacy_groups)
    bpy.app.handlers.load_pre.remove(_clear_viewer_switches)
    _release_legacy_groups(None)
    if bpy.app.timers.is_registered(_flush_source_updates):
        bpy.app.timers.unregister(_flush_source_updates)
    _clear_viewer_switches()
    _dirty_trees.clear()
    bpy.types.NODE_MT_compositor_node_add_all.remove(_add_node_menu)
    bpy.types.NODE_MT_context_menu.remove(_preview_status_menu)
    bpy.utils.unregister_class(BR_MT_preview_status)
    spot_meter.unregister()
    bpy.utils.unregister_class(BR_OT_pick_neutral)
    bpy.utils.unregister_class(BR_OT_bind_photo)
    bpy.utils.unregister_class(BR_OT_reader_use_libraw)
    bpy.utils.unregister_class(BR_NODE_raw_develop)
    bpy.utils.unregister_class(BR_NODE_raw_reader)
    film_node.unregister()
    film_display.unregister()
