# SPDX-License-Identifier: GPL-3.0-or-later
"""Spektrafilm recipe, compositor node and graph editing operators."""
import json
import uuid
from functools import lru_cache

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, StringProperty

from . import spektrafilm_engine
from .film_effects import BY_KEY, DIR_MATRIX_PARAMETERS, FAMILIES, FIXED_EXPOSURES, FLOW_SECTIONS, GRAIN_MODELS, GROUPS, PROCESS_MODES


FILM_TYPE = "BlenderPhotoSpektrafilm"
OFX_TYPE = "BlenderPhotoSpektrafilmOFX"
FILM_TYPES = (FILM_TYPE, OFX_TYPE)
_connections = {}
_applying_user_preset = set()


def backend_error(node):
    if node.bl_idname == OFX_TYPE:
        from . import ofx_config, spektrafilm_ofx_engine
        return spektrafilm_ofx_engine.backend_error(ofx_config.current_path())
    return spektrafilm_engine.backend_error()


def recipe(node):
    if node.bl_idname == OFX_TYPE:
        from . import ofx_node
        return ofx_node.recipe(node)
    settings = {key: getattr(node, key) for key in
                ("film", "paper", "grain", "halation")}
    settings.update(exposure=0.0, print_exposure=0.0)
    settings["grain_model"] = GRAIN_MODELS.index(node.grain_model)
    effects = {}
    for key, effect in BY_KEY.items():
        value = getattr(node, key)
        if isinstance(effect.default, tuple):
            value = tuple(value)
        elif isinstance(value, str):
            value = FAMILIES.index(value)
        effects[key] = value
    settings["effects"] = effects
    parameters = _stored_parameters(node.preset_parameters) if node.preset_parameters else {}
    settings["parameters"] = {k: v for k, v in parameters.items()
                              if k not in effects and k not in FIXED_EXPOSURES}
    settings["parameters"].update(process=PROCESS_MODES.index(node.process),
                                  scanNegativeInvert=node.scanNegativeInvert)
    return settings


def load_preset(node, path):
    """Replace the film recipe and import compatible Apple RAW settings upstream."""
    from . import compositor, film_preset

    name, settings, fields, parameters = film_preset.read(path)
    notice = compositor.apply_spektrafilm_developer(node, settings)
    for prop in film_preset.NODE_PROPERTIES.values():
        if prop in fields:
            setattr(node, prop, fields[prop])
        elif node.is_property_set(prop):
            node.property_unset(prop)
    node.preset_parameters = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
    node.preset_name = name
    node.preset_film = fields.get("film") or spektrafilm_engine.default_film()
    node.use_stock_calibration = False
    _request(node)
    notices = [notice] if notice else []
    encoding = settings.get("ints", {}).get("outputColorSpace")
    if encoding in (15, 24, 25):
        label = {15: "Linear Rec.709", 24: "Rec.709 Gamma 2.2", 25: "Rec.709 Gamma 2.4"}[encoding]
        notices.append(f"Preset output is {label}. The Photo Workspace uses sRGB for display and SDR export. "
                       "Choose a matching export encoding to reproduce the standalone file values.")
    return notices


def _create_group():
    tree = bpy.data.node_groups.new(".Blender Photo Editor Spektrafilm", "CompositorNodeTree")
    tree.interface.new_socket(name="Image", in_out="INPUT", socket_type="NodeSocketColor")
    tree.interface.new_socket(name="Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    source = tree.nodes.new("NodeGroupInput")
    source.name = "Film Input"
    source.location = (-220, 100)
    cache = tree.nodes.new("CompositorNodeImage")
    cache.name = "Film Result"
    cache.location = (-220, -100)
    # File exchange stores a zero-centered frame. Reattach the live input's
    # canvas offset so moving a branch doesn't move the film result back.
    info = tree.nodes.new("CompositorNodeImageInfo")
    info.location = (0, 180)
    axes = tree.nodes.new("ShaderNodeSeparateXYZ")
    axes.location = (190, 180)
    position = tree.nodes.new("CompositorNodeTranslate")
    position.name = "Film Position"
    position.location = (380, -40)
    tree.links.new(source.outputs["Image"], info.inputs["Image"])
    tree.links.new(info.outputs["Location"], axes.inputs[0])
    tree.links.new(cache.outputs["Image"], position.inputs["Image"])
    for name in ("X", "Y"):
        tree.links.new(axes.outputs[name], position.inputs[name])
    output = tree.nodes.new("NodeGroupOutput")
    output.name = "Film Output"
    output.location = (580, 80)
    tree.links.new(source.outputs["Image"], output.inputs["Image"])
    return tree


def restore_film_group(node):
    if node.bl_idname in FILM_TYPES and not node.node_tree:
        node.node_tree = _create_group()
        node.status = ""


def apply_result(node, image):
    """Switch the group output to a completed scan, or to its live input."""
    nodes = node.node_tree.nodes
    cached = nodes["Film Result"]
    if cached.image != image:
        cached.image = image
    source = (nodes["Film Position"] if image else nodes["Film Input"]).outputs["Image"]
    target = nodes["Film Output"].inputs["Image"]
    if not target.links or target.links[0].from_socket != source:
        node.node_tree.links.new(source, target)


def invalidate(node):
    apply_result(node, None)


def has_legacy_exposure(node):
    return node.exposure != 0.0 or node.print_exposure != 0.0


def reset_legacy_exposure(node):
    node.property_unset("exposure")
    node.property_unset("print_exposure")


def reset_recipe(node):
    if node.bl_idname == OFX_TYPE:
        from . import ofx_node
        return ofx_node.reset_recipe(node)
    from . import film_preview
    for field in ("preset_parameters", "preset_name", "preset_film", "use_stock_calibration",
                  "film", "paper", "process", "scanNegativeInvert",
                  "grain", "grain_model", "halation", *BY_KEY):
        node.property_unset(field)
    reset_legacy_exposure(node)
    film_preview.discard_scan(node)
    film_preview.request(node)


def _zero_ev(node, value, stored_value, is_set):
    # Keep old RNA paths readable for migration and force all new writes to zero.
    return 0.0


def _request(node, _context=None):
    if node.as_pointer() not in _applying_user_preset and node.node_tree:
        from . import film_preview
        film_preview.request(node)


def _live_preview_changed(node, _context):
    if node.node_tree:
        from . import film_preview
        film_preview.live_preview_changed(node)


@lru_cache(maxsize=32)
def _stored_parameters(serialized):
    return json.loads(serialized)


def _current_film(node):
    if node.film:
        return node.film
    try:
        return spektrafilm_engine.default_film()
    except (OSError, RuntimeError):
        return ""


def _inherited_calibration(node):
    if node.use_stock_calibration or not node.preset_parameters:
        return {}
    values = _stored_parameters(node.preset_parameters)
    stock = _current_film(node)
    if node.preset_film:
        return values if stock == node.preset_film else {}
    # Old nodes did not record the preset's stock. Repair a recognized factory
    # matrix for a different stock, but retain unidentifiable custom calibration.
    known = spektrafilm_engine.known_dir_calibration(values)
    if known is not None and known != spektrafilm_engine.dir_defaults(stock):
        return {}
    return values


def _dir_value(key):
    def effective(node, value, is_set):
        if is_set:
            return value
        return _inherited_calibration(node).get(key, spektrafilm_engine.dir_defaults(_current_film(node))[key])
    return effective


def _film_changed(node, _context=None):
    if node.as_pointer() in _applying_user_preset:
        return
    stock = _current_film(node)
    try:
        valid = stock in spektrafilm_engine.stocks()[0]
    except (OSError, RuntimeError):
        valid = False
    if valid:
        # Preserve unattributed legacy custom matrices when simply opening a
        # file, but an explicit stock selection must not carry them forward.
        if not node.preset_film and node.preset_parameters:
            inherited = _stored_parameters(node.preset_parameters)
            if any(key in inherited for key in DIR_MATRIX_PARAMETERS):
                node.use_stock_calibration = True
        # The four coefficients describe the chosen stock. Artistic DIR amounts
        # and all unrelated effects survive a stock change.
        for key in DIR_MATRIX_PARAMETERS:
            node.property_unset(key)
        if node.process != PROCESS_MODES[2]:
            node.process = PROCESS_MODES[1 if spektrafilm_engine.is_slide(stock) else 0]
            node.scanNegativeInvert = False
    _request(node)


def reset_calibration(node):
    node.use_stock_calibration = True
    for key in DIR_MATRIX_PARAMETERS:
        node.property_unset(key)
    _request(node)


def _preset_parameter(key):
    def legacy_value(node, value, is_set):
        if not is_set and node.preset_parameters:
            return _stored_parameters(node.preset_parameters).get(key, value)
        return value
    return legacy_value


def _effect_property(effect, stored_in_old_presets):
    options = dict(name=effect.label, description=effect.description, update=_request)
    if stored_in_old_presets:
        # Version 0.14 stored these native values in the imported preset JSON.
        # An RNA edit takes precedence, including in linked and appended node trees.
        options["get_transform"] = _preset_parameter(effect.key)
    if effect.key in DIR_MATRIX_PARAMETERS:
        options["get_transform"] = _dir_value(effect.key)
    if isinstance(effect.default, tuple):
        return FloatVectorProperty(size=len(effect.default), default=effect.default, min=effect.minimum,
                                   max=effect.maximum, precision=3, **options)
    if isinstance(effect.default, bool):
        return BoolProperty(default=effect.default, **options)
    if isinstance(effect.default, int):
        items = [(family, family, "", index) for index, family in enumerate(FAMILIES)]
        return EnumProperty(items=items, default=FAMILIES[effect.default], **options)
    return FloatProperty(default=effect.default, min=effect.minimum, max=effect.maximum,
                         precision=3, **options)


def _stocks(index, query):
    try:
        choices = spektrafilm_engine.stocks()[index]
    except (OSError, RuntimeError):
        return []
    words = query.replace("_", " ").casefold().split()
    return [name for name in choices if all(word in name.casefold() for word in words)]


def _films(self, context, query):
    return _stocks(0, query)


def _papers(self, context, query):
    return _stocks(1, query)


def _action(layout, operator, node, text, icon):
    op = layout.operator(operator, text=text, icon=icon)
    op.tree_name = node.id_data.name
    op.node_name = node.name
    return op


def draw_insert(layout, developer):
    error = spektrafilm_engine.backend_error()
    if error:
        layout.label(text=error, icon="INFO")
    else:
        _action(layout, "blender_raw.add_spektrafilm", developer, "Add Spektrafilm Lite", "ADD")


def _operator_node(op, context, node_type):
    tree = getattr(context.space_data, "edit_tree", None)
    if not tree or tree.name != op.tree_name:
        tree = bpy.data.node_groups.get(op.tree_name)
    if tree and tree.is_editable and tree.bl_idname == "CompositorNodeTree":
        node = tree.nodes.get(op.node_name)
        if node and node.bl_idname == node_type:
            return node
    return None


class BR_NODE_spektrafilm(bpy.types.CompositorNodeCustomGroup):
    bl_idname = FILM_TYPE
    bl_label = "Spektrafilm Lite"
    bl_description = "Simulate film exposure, development, printing and scanning on the GPU"
    bl_icon = "IMAGE_DATA"
    bl_width_default = 240

    instance_uid: StringProperty(options={"HIDDEN"})
    live_preview: BoolProperty(name="Live Preview", description="Update the scan when its inputs change. "
                               "Pausing holds the last scan, including its upstream adjustments",
                               default=False, options=set(), update=_live_preview_changed)
    status: StringProperty(options={"SKIP_SAVE", "HIDDEN"})
    preset_name: StringProperty(options={"HIDDEN"})
    preset_parameters: StringProperty(options={"HIDDEN"})
    preset_film: StringProperty(options={"HIDDEN"})
    use_stock_calibration: BoolProperty(options={"HIDDEN"})
    show_dir: BoolProperty(name="DIR Couplers", options=set())
    process: EnumProperty(name="Mode",
                          items=[(name, name, description, index) for index, (name, description) in enumerate(zip(
                              PROCESS_MODES, ("Simulate film, then print and scan it",
                                              "Scan the developed film without printing; use for slide film",
                                              "Print an existing negative scan stored as linear film transmittance")))],
                          default=PROCESS_MODES[0], get_transform=_preset_parameter("process"), update=_request)
    scanNegativeInvert: BoolProperty(name="Positive Output", default=False,
                                     description="Normalize the scan using the film stock's polarity. Leave off for a direct slide scan",
                                     get_transform=_preset_parameter("scanNegativeInvert"), update=_request)
    # Persist the engine's names instead of indices, which move as profiles are added.
    film: StringProperty(name="Film", description="Changing stock restores its DIR calibration and selects the normal film/scan mode. Leave empty for the engine default",
                         search=_films, options=set(), update=_film_changed)
    paper: StringProperty(name="Print", description="Print stock; leave empty for the engine default",
                          search=_papers, options=set(), update=_request)
    exposure: FloatProperty(name="Film Exposure", options={"HIDDEN"}, set_transform=_zero_ev)
    print_exposure: FloatProperty(name="Print Exposure", options={"HIDDEN"}, set_transform=_zero_ev)
    grain: BoolProperty(name="Grain", default=False, update=_request)
    grain_model: EnumProperty(name="Grain Model",
                              items=[(name, name, "", index) for index, name in enumerate(GRAIN_MODELS)],
                              default="Preview", update=_request)
    halation: BoolProperty(name="Halation", default=False, update=_request)

    @classmethod
    def poll(cls, tree):
        return tree.bl_idname == "CompositorNodeTree" and not spektrafilm_engine.backend_error()

    def init(self, context):
        self.instance_uid = uuid.uuid4().hex
        self.node_tree = _create_group()

    def copy(self, source):
        from . import adjustments
        if adjustments.borrowing_groups():
            return
        # Blender's temporary clipboard tree borrows the original group without
        # adding an ID user. Replacing it there would decrement the real owner.
        if self.id_data.is_runtime_data:
            return
        self.instance_uid = uuid.uuid4().hex
        self.status = ""
        if source.node_tree:
            self.node_tree = source.node_tree.copy()
            # Blender also calls copy() when snapshotting a graph for a render.
            # Keep that snapshot's upstream pixels; recheck live copies once wired.
            _request(self)

    def update(self):
        if bpy.app.background or not self.inputs or not self.node_tree:
            return
        key = (self.id_data.session_uid, self.as_pointer())
        signature = tuple((link.from_socket.as_pointer(), link.is_muted) for link in self.inputs[0].links)
        if signature != _connections.get(key, ()):
            _connections[key] = signature
            _request(self)

    def free(self):
        _connections.pop((self.id_data.session_uid, self.as_pointer()), None)

    def _draw_stocks(self, layout):
        if self.preset_name:
            layout.label(text=self.preset_name)
        layout.prop(self, "process")
        if self.process != PROCESS_MODES[2]:
            layout.prop(self, "film", placeholder="Engine Default")
        if self.process == PROCESS_MODES[1]:
            layout.prop(self, "scanNegativeInvert")
        else:
            layout.prop(self, "paper", placeholder="Engine Default")

    def _draw_effects(self, layout, *, native_panels=False):
        if self.process != PROCESS_MODES[2]:
            self._draw_dir(layout, native_panels=native_panels)
        for key, label, enabled, properties in FLOW_SECTIONS:
            if self.process == PROCESS_MODES[2] and key in {"grain", "halation", "camera"}:
                continue
            opened = "show_" + key
            if native_panels:
                header, body = layout.panel_prop(self, opened)
                header.use_property_split = False
                header.prop(self, enabled, text=label)
            else:
                # Node canvases do not support region-wide layout panels.
                # Keep their disclosure button left-aligned as well.
                header = layout.row(align=True)
                header.use_property_split = False
                header.prop(self, opened, text="", emboss=False,
                            icon="TRIA_DOWN" if getattr(self, opened) else "TRIA_RIGHT")
                header.prop(self, enabled, text=label)
                body = layout if getattr(self, opened) else None
            if body is None:
                continue
            controls = body.column(align=True)
            controls.active = getattr(self, enabled)
            for name in properties:
                if name == "halationStrength":
                    controls.label(text="Strength RGB")
                    channels = controls.row(align=True)
                    for i, channel in enumerate("RGB"):
                        channels.prop(self, name, index=i, text=channel)
                else:
                    field = controls.row()
                    if name == "scannerWhiteLevel":
                        field.active = self.scannerWhiteCorrection
                    elif name == "scannerBlackLevel":
                        field.active = self.scannerBlackCorrection
                    field.prop(self, name)

    def _draw_dir(self, layout, *, native_panels=False):
        if native_panels:
            header, body = layout.panel_prop(self, "show_dir")
            header.label(text="DIR Couplers")
        else:
            header = layout.row(align=True)
            header.prop(self, "show_dir", text="", emboss=False,
                        icon="TRIA_DOWN" if self.show_dir else "TRIA_RIGHT")
            header.label(text="DIR Couplers")
            body = layout if self.show_dir else None
        if body is None:
            return
        edited = any(self.is_property_set(key) for key in DIR_MATRIX_PARAMETERS)
        inherited = _inherited_calibration(self)
        source = "Custom" if edited else "Preset" if any(key in inherited for key in DIR_MATRIX_PARAMETERS) else "Film defaults"
        body.label(text="Calibration: " + source)
        _action(body, "blender_raw.reset_film_calibration", self, "Restore Film Calibration", "LOOP_BACK")
        for effect in GROUPS["dir"]:
            if isinstance(effect.default, tuple):
                body.label(text=effect.label)
                row = body.row(align=True)
                channels = "RGB" if len(effect.default) == 3 else {"dirGammaRToGb": "GB", "dirGammaGToRb": "RB", "dirGammaBToRg": "RG"}[effect.key]
                for index, channel in enumerate(channels):
                    row.prop(self, effect.key, index=index, text=channel)
            else:
                body.prop(self, effect.key)

    def draw_buttons(self, context, layout):
        presets = layout.row()
        presets.context_pointer_set("spektrafilm_preset_node", self)
        presets.popover(panel="BR_PT_spektrafilm_presets", text="Presets", icon="PRESET")
        error = spektrafilm_engine.backend_error()
        if not error:
            layout.prop(self, "live_preview", text="Live Preview", toggle=True,
                        icon="PAUSE" if self.live_preview else "PLAY")
        else:
            layout.label(text=error, icon="INFO")
        self._draw_stocks(layout)
        self._draw_effects(layout, native_panels=context.space_data.type == 'PROPERTIES')
        if not error:
            from . import film_display
            film_display.draw(context, layout)

    def draw_buttons_ext(self, context, layout):
        self.draw_buttons(context, layout)


# Register the native parameters as node RNA so old files inherit the engine defaults
# and Blender handles undo, duplication, animation and .blend persistence.
BR_NODE_spektrafilm.__annotations__.update({
    effect.key: _effect_property(effect, key in {"grain", "halation", "dir"})
    for key, group in GROUPS.items() for effect in group
})
BR_NODE_spektrafilm.__annotations__.update({
    "show_" + key: BoolProperty(name=label, options=set())
    for key, label, _enabled, _properties in FLOW_SECTIONS
})


def _make_room(previous, inserted, downstream):
    """Move just the downstream graph in the same frame when the insert needs space."""
    right = [node.location.x for node in downstream
             if node.parent == previous.parent and node.location.x > previous.location.x]
    shift = inserted.location.x + inserted.width + 55 - min(right) if right else 0
    if shift <= 0:
        return
    pending = list(downstream)
    seen = {previous, inserted}
    while pending:
        node = pending.pop()
        if node in seen:
            continue
        seen.add(node)
        if node.parent == previous.parent and node.location.x > previous.location.x:
            node.location.x += shift
        pending.extend(link.to_node for socket in node.outputs for link in socket.links)


def insert_image_node(previous, kind, group=None):
    """Insert a color adjustment on the existing output, including any Viewer branches."""
    tree = previous.id_data
    output = previous.outputs[0]
    outgoing = list(output.links)
    node = tree.nodes.new(kind)
    if group is not None:
        node.node_tree = group
        node.label = group.name
    if not (node.inputs and node.outputs and node.inputs[0].type == node.outputs[0].type == 'RGBA'):
        tree.nodes.remove(node)
        raise ValueError("This adjustment requires a primary image input and output")
    node.parent = previous.parent
    node.location = (previous.location.x + previous.width + 55, previous.location.y)
    downstream = [link.to_node for link in outgoing]
    for old in outgoing:
        target, muted = old.to_socket, old.is_muted
        new = tree.links.new(node.outputs[0], target)
        if target.is_multi_input:
            old.swap_multi_input_sort_id(new)
            tree.links.remove(old)
        new.is_muted = muted
    tree.links.new(output, node.inputs[0])
    _make_room(previous, node, downstream)
    return node


def select_node(node):
    tree = node.id_data
    for other in tree.nodes:
        other.select = other == node
    tree.nodes.active = node


def insert_after(developer):
    """The Developer shortcut selects its existing film on repeated clicks."""
    film = next((link.to_node for link in developer.outputs["Image"].links
                 if link.to_node.bl_idname == FILM_TYPE), None)
    if film is None:
        film = insert_image_node(developer, FILM_TYPE)
    select_node(film)
    return film


class BR_OT_reset_film_calibration(bpy.types.Operator):
    bl_idname = "blender_raw.reset_film_calibration"
    bl_label = "Restore Film Calibration"
    bl_description = "Restore this film's four DIR coefficient groups; keep other effect adjustments"
    bl_options = {"REGISTER", "UNDO"}
    tree_name: StringProperty(options={"HIDDEN"})
    node_name: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        node = _operator_node(self, context, FILM_TYPE)
        if node is None:
            return {"CANCELLED"}
        reset_calibration(node)
        return {"FINISHED"}


class BR_OT_add_spektrafilm(bpy.types.Operator):
    bl_idname = "blender_raw.add_spektrafilm"
    bl_label = "Add Spektrafilm"
    bl_description = "Insert Spektrafilm after this Developer or select its existing film node"
    bl_options = {"REGISTER", "UNDO"}
    tree_name: StringProperty(options={"HIDDEN"})
    node_name: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        from .compositor import DEVELOPER_TYPE
        developer = _operator_node(self, context, DEVELOPER_TYPE)
        error = spektrafilm_engine.backend_error()
        if error or not developer:
            self.report({"WARNING"}, error or "Choose an editable Raw Developer")
            return {"CANCELLED"}
        insert_after(developer)
        return {"FINISHED"}


class BR_OT_refresh_spektrafilm(bpy.types.Operator):
    bl_idname = "blender_raw.refresh_spektrafilm"
    bl_label = "Refresh Spektrafilm Preview"
    bl_description = "Render the image currently connected to this Spektrafilm node"
    tree_name: StringProperty(options={"HIDDEN"})
    node_name: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        node = _operator_node(self, context, FILM_TYPE)
        error = spektrafilm_engine.backend_error()
        if error:
            self.report({"WARNING"}, error)
            return {"CANCELLED"}
        from . import film_bake
        if not node or not film_bake.active_input_links(node.inputs["Image"]) or node.mute:
            self.report({"WARNING"}, "Connect an image and enable the Spektrafilm node")
            return {"CANCELLED"}
        if not node.live_preview:
            self.report({"INFO"}, "Play Live Preview to refresh this node")
            return {"CANCELLED"}
        _request(node)
        return {"FINISHED"}


_CLASSES = (BR_NODE_spektrafilm, BR_OT_add_spektrafilm, BR_OT_refresh_spektrafilm,
            BR_OT_reset_film_calibration)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    from . import film_user_presets, ofx_config, ofx_node
    film_user_presets.register()
    ofx_config.register()
    ofx_node.register()


def unregister():
    from . import film_user_presets, ofx_config, ofx_node
    ofx_node.unregister()
    ofx_config.unregister()
    film_user_presets.unregister()
    _connections.clear()
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
