# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent, descriptor-driven Spektrafilm OFX compositor node."""
import textwrap
import uuid

import bpy
from bpy.props import (BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty,
                       IntProperty, StringProperty)

from . import ofx_schema, spektrafilm_ofx_engine as engine


FILM_TYPE = "BlenderPhotoSpektrafilmOFX"
_connections = {}
_changing = set()
_choice_items = {}
_DIR_COEFFICIENTS = ("dirGammaSameLayerRgb", "dirGammaRToGb", "dirGammaGToRb", "dirGammaBToRg")
_HDR_FIELDS = ("hdrTransfer", "hdrReferenceWhiteNits", "hdrPeakNits", "hdrExposureEv", "hdrToneMapping")
_GROUP_PARENTS = {p["name"]: p.get("group", "") for p in ofx_schema.METADATA["parameters"]
                  if p["type"] == "OfxParamTypeGroup"}
_GROUP_LABELS = dict(ofx_schema.GROUPS)
_STOCK_DERIVED = frozenset(_DIR_COEFFICIENTS) | frozenset(
    p.key for p in ofx_schema.PARAMETERS if p.kind == "vector" and p.key.startswith(("mtfNegative", "mtfPrint")))


def parameter_property(key):
    """Keep the OFX namespace separate from Blender's built-in Node properties."""
    return "ofx_" + key


def parameters(node, *, explicit_only=False):
    values = {}
    for parameter in ofx_schema.PARAMETERS:
        prop = parameter_property(parameter.key)
        # The native plug-in computes per-stock values in InstanceChanged. Sending
        # unrelated RNA defaults would overwrite that calibration after film changes.
        if explicit_only and not node.is_property_set(prop) and not parameter.key.endswith("Seed"):
            continue
        value = getattr(node, prop)
        if parameter.kind == "choice":
            value = int(value)
        elif parameter.kind == "vector":
            value = tuple(value)
        values[parameter.key] = value
    return values


def recipe(node):
    from . import ofx_config
    return {"engine": "ofx", "plugin_path": ofx_config.current_path(),
            "parameters": parameters(node, explicit_only=True)}


def _request(node, _context=None):
    if node.node_tree:
        from . import film_preview
        film_preview.request(node)


def _set_native(node, key, value):
    parameter = ofx_schema.BY_KEY.get(key)
    if parameter:
        setattr(node, parameter_property(key), str(value) if parameter.kind == "choice" else value)


def _parameter_changed(parameter):
    def changed(node, context):
        identity = node.as_pointer()
        if identity in _changing:
            return
        _changing.add(identity)
        try:
            key = parameter.key
            if key in {"filmCategory", "film", "mtfNegativeSource", "printCategory", "paper", "mtfPrintSource"}:
                negative = key in {"filmCategory", "film", "mtfNegativeSource"}
                for field in _STOCK_DERIVED:
                    matches = (field.startswith("mtfNegative") or field in _DIR_COEFFICIENTS) if negative else field.startswith("mtfPrint")
                    if matches:
                        if field in ofx_schema.BY_KEY:
                            node.property_unset(parameter_property(field))
            if key in {"filmCategory", "printCategory"}:
                child = "film" if key == "filmCategory" else "paper"
                dependency = ofx_schema.CHOICE_DEPENDENCIES.get(child)
                if dependency:
                    selected = dependency["values"][str(_value(node, key))]
                    _set_native(node, child, selected["default"])
                if child + "ProfileId" in ofx_schema.BY_KEY:
                    node.property_unset(parameter_property(child + "ProfileId"))
            elif key in {"film", "paper"}:
                if key + "ProfileId" in ofx_schema.BY_KEY:
                    node.property_unset(parameter_property(key + "ProfileId"))
            elif key == "hdrPreset" and _value(node, key) in (0, 1, 2):
                preset = _value(node, key)
                for field, value in zip(("hdrTransfer", "hdrReferenceWhiteNits", "hdrPeakNits", "hdrToneMapping"),
                                        (1 if preset == 2 else 0, 203.0, 4000.0 if preset == 1 else 1000.0, 1)):
                    _set_native(node, field, value)
            elif key in _HDR_FIELDS:
                preset = ofx_schema.BY_KEY.get("hdrPreset")
                if preset and "Custom" in preset.options:
                    _set_native(node, "hdrPreset", preset.options.index("Custom"))
            elif key in _DIR_COEFFICIENTS:
                _set_native(node, "dirUsesStockCalibration", False)
            elif key == "dirUsesStockCalibration":
                if _value(node, key):
                    for field in _DIR_COEFFICIENTS:
                        if field in ofx_schema.BY_KEY:
                            node.property_unset(parameter_property(field))
                else:
                    # Materialize the numbers displayed by the custom controls.
                    for field in _DIR_COEFFICIENTS:
                        if field in ofx_schema.BY_KEY:
                            value = getattr(node, parameter_property(field))
                            _set_native(node, field, tuple(value))
        finally:
            _changing.discard(identity)
        _request(node, context)
    return changed


def _live_preview_changed(node, _context):
    if node.node_tree:
        from . import film_preview
        film_preview.live_preview_changed(node)


def apply_parameters(node, values, *, name=""):
    """Validate the whole preset before changing any editable Blender property."""
    engine.normalize_parameters(values)
    for key, value in values.items():
        parameter = ofx_schema.BY_KEY[key]
        if parameter.kind in {"float", "vector"}:
            numbers = value if parameter.kind == "vector" else (value,)
            if any(abs(number) > 3.402823e38 for number in numbers):
                raise ValueError(f"{parameter.label} exceeds Blender's supported numeric range")
    identity = node.as_pointer()
    _changing.add(identity)
    try:
        for parameter in ofx_schema.PARAMETERS:
            prop = parameter_property(parameter.key)
            if parameter.key in values:
                _set_native(node, parameter.key, values[parameter.key])
            elif node.is_property_set(prop):
                node.property_unset(prop)
    finally:
        _changing.discard(identity)
    node.preset_name = name
    from . import film_preview
    film_preview.discard_scan(node)
    _request(node)


def reset_recipe(node):
    apply_parameters(node, {})


def load_preset(node, path):
    from . import ofx_preset
    name, values, notices = ofx_preset.read(path)
    apply_parameters(node, values, name=name)
    return notices


def _action(layout, operator, node, text, icon):
    from . import film_node
    film_node._action(layout, operator, node, text, icon)


def _node(operator, context):
    from . import film_node
    return film_node._operator_node(operator, context, FILM_TYPE)


def _stock_search(parameter):
    # Keep the choices referenced by a closure for Blender's search callback.
    choices = tuple(parameter.options)

    def search(_self, _context, query):
        words = query.replace("_", " ").casefold().split()
        return [name for name in choices if all(word in name.casefold() for word in words)]
    return search


def _dependent_choices(parameter):
    def items(node, _context):
        dependency = ofx_schema.CHOICE_DEPENDENCIES[parameter.key]
        control = dependency["control"]
        category = _value(node, control, ofx_schema.ALL_BY_KEY[control].default) if node else ofx_schema.ALL_BY_KEY[control].default
        cache_key = (parameter.key, category)
        if cache_key not in _choice_items:
            labels = ofx_schema.choice_options(parameter.key, {control: category})
            _choice_items[cache_key] = [(str(index), label, "", index) for index, label in enumerate(labels)]
        return _choice_items[cache_key]
    return items


def _property(parameter):
    options = dict(name=parameter.label, description=f"{parameter.label} ({_GROUP_LABELS.get(parameter.group) or 'Processing'})",
                   update=_parameter_changed(parameter))
    if parameter.key == "dirUsesStockCalibration":
        options.update(name="Use Stock Calibration", description="Use the selected film's native DIR calibration; turn off to edit custom coefficients")
    if parameter.kind == "bool":
        return BoolProperty(default=parameter.default, **options)
    if parameter.kind == "choice":
        if parameter.key in ofx_schema.CHOICE_DEPENDENCIES:
            return EnumProperty(items=_dependent_choices(parameter), default=int(parameter.default), **options)
        return EnumProperty(items=[(str(index), label, "", index)
                                   for index, label in enumerate(parameter.options)],
                            default=str(parameter.default), **options)
    if parameter.kind == "stock":
        return StringProperty(default=parameter.default, search=_stock_search(parameter),
                              options=set(), **options)
    if parameter.kind == "string":
        return StringProperty(default=parameter.default, **options)
    limits = dict(min=max(float(parameter.minimum), -3.402823e38),
                  max=min(float(parameter.maximum), 3.402823e38))
    if parameter.kind == "int":
        return IntProperty(default=parameter.default,
                           min=int(parameter.minimum), max=int(parameter.maximum), **options)
    if parameter.kind == "vector":
        return FloatVectorProperty(size=len(parameter.default), default=tuple(parameter.default),
                                   precision=4, **limits, **options)
    return FloatProperty(default=parameter.default, precision=4, **limits, **options)


def _value(node, key, default=None):
    if key not in ofx_schema.BY_KEY:
        return default
    value = getattr(node, parameter_property(key))
    return int(value) if ofx_schema.BY_KEY[key].kind == "choice" else value


def _enabled(node, key):
    """Match the dependencies of the OFX controls without discarding their values."""
    dependency = {
        "cameraUvCutNm": "cameraUvFilterEnabled",
        "cameraIrCutNm": "cameraIrFilterEnabled",
        "autoExposureMethod": "autoExposure",
        "scannerWhiteLevel": "scannerWhiteCorrection",
        "scannerBlackLevel": "scannerBlackCorrection",
    }.get(key)
    if dependency:
        return bool(_value(node, dependency, True))
    for prefix, switch in (("cameraDiffusion", "cameraDiffusionEnabled"),
                           ("printDiffusion", "printDiffusionEnabled"),
                           ("colorAdaptation", "colorAdaptation")):
        if key.startswith(prefix) and key != switch:
            return bool(_value(node, switch, True))
    if key.startswith("grainSynthesis"):
        return bool(_value(node, "grainEnabled", True)) and _value(node, "grainModel", 2) == 2
    if key == "grainSubLayerCount":
        return bool(_value(node, "grainSublayersEnabled", True))
    return True


def _group_toggle(key):
    candidates = {"grainGroup": "grainEnabled", "grain": "grainEnabled",
                  "halationGroup": "halationEnabled", "halation": "halationEnabled",
                  "scannerGroup": "scannerEnabled", "scanner": "scannerEnabled"}
    candidate = candidates.get(key, key.removesuffix("Group") + "Enabled")
    return candidate if candidate in ofx_schema.BY_KEY else None


def _children(key):
    return [child for child, _label in ofx_schema.GROUPS if child and _GROUP_PARENTS.get(child) == key] if key else []


def _has_controls(key):
    return any(p.group == key for p in ofx_schema.PARAMETERS) or any(_has_controls(child) for child in _children(key))


def _conditional_control(node, key):
    if key.startswith("hdr"):
        return _value(node, "outputRole", 0) == 1
    if key == "negativeScanOutput":
        return _value(node, "process", 0) == 1
    if key.startswith("negativeColor") or key in {"negativeScanInput", "negativeInputGreyReference", "negativeCalibrationMode"}:
        return _value(node, "process", 0) == 2
    if key in {"negativeScanGreyReference", "scanNegativeInvert"}:
        return _value(node, "process", 0) == 1
    return False


def _panel(layout, node, opened, label, native_panels, toggle=None):
    if native_panels:
        header, body = layout.panel_prop(node, opened)
    else:
        header = layout.row(align=True)
        header.prop(node, opened, text="", emboss=False,
                    icon="TRIA_DOWN" if getattr(node, opened) else "TRIA_RIGHT")
        body = layout if getattr(node, opened) else None
    header.use_property_split = False
    if toggle:
        header.prop(node, parameter_property(toggle), text=label)
    else:
        header.label(text=label)
    return body


def _draw_control(node, controls, parameter):
    if parameter.key in {"filmProfileId", "paperProfileId"}:
        row = controls.row()
        row.active = False
        row.label(text=parameter.label)
        prop = parameter_property(parameter.key)
        row.label(text=str(getattr(node, prop)) if node.is_property_set(prop) else "Follows stock selection")
        return
    if parameter.key in _STOCK_DERIVED and not node.is_property_set(parameter_property(parameter.key)):
        row = controls.row()
        row.active = False
        row.label(text=parameter.label)
        row.label(text="Stock default")
        return
    if (parameter.key in _DIR_COEFFICIENTS
            and "dirUsesStockCalibration" in ofx_schema.BY_KEY
            and _value(node, "dirUsesStockCalibration", False)):
        row = controls.row()
        row.active = False
        row.label(text=parameter.label)
        row.label(text="Stock default")
        return
    row = controls.row()
    row.active = _enabled(node, parameter.key)
    row.prop(node, parameter_property(parameter.key))
    help_button = row.operator("blender_raw.ofx_parameter_help", text="", icon="QUESTION", emboss=False)
    help_button.key = parameter.key


def _draw_group(node, layout, key, native_panels):
    if not _has_controls(key):
        return
    toggle = _group_toggle(key)
    body = _panel(layout, node, "show_ofx_" + key, _GROUP_LABELS[key], native_panels, toggle)
    if body is None:
        return
    controls = body.column(align=True)
    if toggle:
        controls.active = bool(_value(node, toggle))
    if any(p.group == key and p.key in _STOCK_DERIVED and not node.is_property_set(parameter_property(p.key))
           for p in ofx_schema.PARAMETERS):
        op = controls.operator("blender_raw.resolve_spektrafilm_ofx_values", text="Edit Stock Defaults", icon="OPTIONS")
        op.tree_name, op.node_name, op.group_key = node.id_data.name, node.name, key
    advanced = []
    for parameter in ofx_schema.PARAMETERS:
        if parameter.group != key or parameter.key == toggle:
            continue
        if parameter.secret and not _conditional_control(node, parameter.key) and parameter.key != "dirUsesStockCalibration":
            advanced.append(parameter)
        else:
            _draw_control(node, controls, parameter)
    if advanced:
        extra = _panel(controls, node, "show_ofx_advanced_" + key, "Advanced Controls", native_panels)
        if extra is not None:
            for parameter in advanced:
                _draw_control(node, extra, parameter)
    if key == "colorGroup":
        if _value(node, "process", 0) == 2 and _value(node, "negativeScanInput", 0) != 0:
            controls.label(text="Use a Non-Color source for encoded negative data.", icon="INFO")
        if engine.output_is_data(parameters(node, explicit_only=True)):
            controls.label(text="Output contains Cineon/ADX negative data.", icon="INFO")
            controls.label(text="Use untransformed 32-bit EXR to retain its values.")
    if key == "manageGroup":
        _action(controls, "blender_raw.export_spektrafilm_ofx_lut", node, "Export CUBE LUT", "EXPORT")
    for child in _children(key):
        _draw_group(node, controls, child, native_panels)


class BR_NODE_spektrafilm_ofx(bpy.types.CompositorNodeCustomGroup):
    bl_idname = FILM_TYPE
    bl_label = "Spektrafilm OFX"
    bl_description = "Spektrafilm OFX film, print and scan processing with independent settings"
    bl_icon = "IMAGE_DATA"
    bl_width_default = 280

    instance_uid: StringProperty(options={"HIDDEN"})
    live_preview: BoolProperty(name="Live Preview", default=False, options=set(),
                               description="Update the scan when its input or settings change; pausing holds the completed scan",
                               update=_live_preview_changed)
    status: StringProperty(options={"SKIP_SAVE", "HIDDEN"})
    preset_name: StringProperty(options={"HIDDEN"})

    @classmethod
    def poll(cls, tree):
        # Missing engines must not prevent opening and editing saved recipes.
        return tree.bl_idname == "CompositorNodeTree"

    def init(self, context):
        from . import film_node
        self.instance_uid = uuid.uuid4().hex
        self.node_tree = film_node._create_group()
        self.node_tree.name = ".Blender Photo Editor Spektrafilm OFX"

    def copy(self, source):
        from . import adjustments
        if adjustments.borrowing_groups() or self.id_data.is_runtime_data:
            return
        self.instance_uid = uuid.uuid4().hex
        self.status = ""
        if source.node_tree:
            self.node_tree = source.node_tree.copy()
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

    def _draw_parameters(self, context, layout):
        native_panels = context.space_data.type == "PROPERTIES"
        for group_key, _label in ofx_schema.GROUPS:
            if not _GROUP_PARENTS.get(group_key):
                _draw_group(self, layout, group_key, native_panels)

    def draw_buttons(self, context, layout):
        from . import ofx_config
        ofx_config.draw_setup(layout, context)
        row = layout.row(align=True)
        _action(row, "blender_raw.import_spektrafilm_ofx_preset", self, "Import Preset", "FILE_FOLDER")
        _action(row, "blender_raw.export_spektrafilm_ofx_preset", self, "", "EXPORT")
        if context.space_data.type != "PROPERTIES":
            _action(row, "blender_raw.reset_spektrafilm_ofx", self, "", "LOOP_BACK")
        if self.preset_name:
            layout.label(text=self.preset_name)
        _action(layout, "blender_raw.builtin_spektrafilm_ofx_preset", self, "Built-in Looks", "PRESET")
        error = engine.backend_error(ofx_config.current_path(context))
        if not error:
            layout.prop(self, "live_preview", toggle=True,
                        icon="PAUSE" if self.live_preview else "PLAY")
        self._draw_parameters(context, layout)

    def draw_buttons_ext(self, context, layout):
        self.draw_buttons(context, layout)


BR_NODE_spektrafilm_ofx.__annotations__.update({
    parameter_property(parameter.key): _property(parameter)
    for parameter in ofx_schema.PARAMETERS
})
BR_NODE_spektrafilm_ofx.__annotations__.update({
    "show_ofx_advanced_" + key: BoolProperty(name="Advanced Controls", options=set())
    for key, _label in ofx_schema.GROUPS
})


class BR_OT_ofx_parameter_help(bpy.types.Operator):
    bl_idname = "blender_raw.ofx_parameter_help"
    bl_label = "Parameter Help"
    bl_description = "Read this control's help from your installed OFX plug-in"
    key: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        from . import ofx_config
        parameter = ofx_schema.BY_KEY.get(self.key)
        if parameter is None:
            return {"CANCELLED"}
        path = ofx_config.current_path(context)
        try:
            hint = engine.parameter_help(self.key, path) if not engine.backend_error(path) else ""
        except (OSError, RuntimeError, ValueError) as error:
            self.report({"WARNING"}, str(error))
            hint = ""
        if not hint:
            hint = f"{parameter.label}. Default: {parameter.default}."

        def draw(menu, _context):
            for paragraph in hint.splitlines():
                for line in textwrap.wrap(paragraph, width=54) or [""]:
                    menu.layout.label(text=line)

        context.window_manager.popup_menu(draw, title=parameter.label, icon="INFO")
        return {"FINISHED"}


_builtin_items = []


def _preset_items(_operator, _context):
    return _builtin_items


class BR_OT_builtin_spektrafilm_ofx_preset(bpy.types.Operator):
    bl_idname = "blender_raw.builtin_spektrafilm_ofx_preset"
    bl_label = "Load Spektrafilm OFX Built-in Look"
    bl_description = "Apply a bundled look using the official plug-in's preset settings"
    bl_options = {"REGISTER", "UNDO"}
    bl_property = "preset"
    tree_name: StringProperty(options={"HIDDEN"})
    node_name: StringProperty(options={"HIDDEN"})
    preset: EnumProperty(name="Look", items=_preset_items)

    def invoke(self, context, event):
        from . import ofx_config
        try:
            looks = engine.builtin_looks(ofx_config.current_path(context))
        except (OSError, RuntimeError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        _builtin_items[:] = [(f"{category}:{index}", f"{group} / {label}", "", item)
                             for item, (category, index, group, label) in enumerate(looks)]
        context.window_manager.invoke_search_popup(self)
        return {"FINISHED"}

    def execute(self, context):
        node = _node(self, context)
        if not node:
            return {"CANCELLED"}
        try:
            from . import ofx_config
            category, index = (int(value) for value in self.preset.split(":"))
            values = engine.builtin_preset(category, index, plugin_path=ofx_config.current_path(context))
            label = next(label for c, i, _, label in engine.builtin_looks(ofx_config.current_path(context))
                         if (c, i) == (category, index))
            apply_parameters(node, values, name=label)
        except (OSError, RuntimeError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}
BR_NODE_spektrafilm_ofx.__annotations__.update({
    "show_ofx_" + key: BoolProperty(name=label, options=set(),
                                   default=key in {"colorGroup", "filmGroup", "printGroup", "color", "film", "print"})
    for key, label in ofx_schema.GROUPS
})


class BR_OT_import_spektrafilm_ofx_preset(bpy.types.Operator):
    bl_idname = "blender_raw.import_spektrafilm_ofx_preset"
    bl_label = "Import Spektrafilm OFX Preset"
    bl_description = "Load the processing settings from an OFX .spkpreset"
    bl_options = {"REGISTER", "UNDO"}
    tree_name: StringProperty(options={"HIDDEN"})
    node_name: StringProperty(options={"HIDDEN"})
    filepath: StringProperty(subtype="FILE_PATH")
    filter_glob: StringProperty(default="*.spkpreset", options={"HIDDEN"})

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        node = _node(self, context)
        if not node:
            self.report({"ERROR"}, "Choose an editable Spektrafilm OFX node")
            return {"CANCELLED"}
        try:
            notices = load_preset(node, bpy.path.abspath(self.filepath))
        except (OSError, RuntimeError, ValueError, RecursionError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        for notice in notices:
            self.report({"WARNING"}, notice)
        return {"FINISHED"}


class BR_OT_export_spektrafilm_ofx_preset(bpy.types.Operator):
    bl_idname = "blender_raw.export_spektrafilm_ofx_preset"
    bl_label = "Export Spektrafilm OFX Preset"
    bl_description = "Save this node's settings as an OFX .spkpreset"
    tree_name: StringProperty(options={"HIDDEN"})
    node_name: StringProperty(options={"HIDDEN"})
    filepath: StringProperty(subtype="FILE_PATH")
    check_existing: BoolProperty(default=True, options={"HIDDEN"})
    filter_glob: StringProperty(default="*.spkpreset", options={"HIDDEN"})

    def invoke(self, context, event):
        node = _node(self, context)
        if not self.filepath:
            self.filepath = bpy.path.clean_name(node.preset_name or "Spektrafilm OFX") + ".spkpreset" if node else "Spektrafilm OFX.spkpreset"
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def check(self, context):
        value = bpy.path.ensure_ext(self.filepath, ".spkpreset")
        changed = self.filepath != value
        self.filepath = value
        return changed

    def execute(self, context):
        node = _node(self, context)
        if not node:
            self.report({"ERROR"}, "Choose an editable Spektrafilm OFX node")
            return {"CANCELLED"}
        try:
            from . import ofx_config, ofx_preset
            effective = engine.effective_parameters(parameters(node, explicit_only=True),
                                                    plugin_path=ofx_config.current_path(context))
            ofx_preset.write(bpy.path.ensure_ext(bpy.path.abspath(self.filepath), ".spkpreset"),
                             node.preset_name or "Spektrafilm OFX", effective)
        except (OSError, RuntimeError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


class BR_OT_reset_spektrafilm_ofx(bpy.types.Operator):
    bl_idname = "blender_raw.reset_spektrafilm_ofx"
    bl_label = "Reset Spektrafilm OFX"
    bl_description = "Restore this node's OFX factory settings"
    bl_options = {"REGISTER", "UNDO"}
    tree_name: StringProperty(options={"HIDDEN"})
    node_name: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        node = _node(self, context)
        if not node:
            return {"CANCELLED"}
        reset_recipe(node)
        return {"FINISHED"}


class BR_OT_export_spektrafilm_ofx_lut(bpy.types.Operator):
    bl_idname = "blender_raw.export_spektrafilm_ofx_lut"
    bl_label = "Export Spektrafilm OFX CUBE LUT"
    bl_description = "Export a 33 or 65 point color LUT through the official OFX engine"
    tree_name: StringProperty(options={"HIDDEN"})
    node_name: StringProperty(options={"HIDDEN"})
    filepath: StringProperty(subtype="FILE_PATH")
    check_existing: BoolProperty(default=True, options={"HIDDEN"})
    filter_glob: StringProperty(default="*.cube", options={"HIDDEN"})
    lut_size: EnumProperty(name="LUT Size", items=(("33", "33", ""), ("65", "65", "")), default="65")

    def invoke(self, context, event):
        if not self.filepath:
            self.filepath = "Spektrafilm OFX.cube"
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def check(self, context):
        value = bpy.path.ensure_ext(self.filepath, ".cube")
        changed = self.filepath != value
        self.filepath = value
        return changed

    def draw(self, context):
        self.layout.prop(self, "lut_size")
        self.layout.label(text="Input: Linear Rec.709")
        self.layout.label(text="Output: selected OFX SDR encoding")

    def execute(self, context):
        node = _node(self, context)
        if not node:
            self.report({"ERROR"}, "Choose an editable Spektrafilm OFX node")
            return {"CANCELLED"}
        try:
            from . import ofx_config
            engine.export_lut(bpy.path.ensure_ext(bpy.path.abspath(self.filepath), ".cube"),
                              parameters(node, explicit_only=True), size=int(self.lut_size),
                              plugin_path=ofx_config.current_path(context), overwrite=True)
        except (OSError, RuntimeError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


class BR_OT_resolve_spektrafilm_ofx_values(bpy.types.Operator):
    bl_idname = "blender_raw.resolve_spektrafilm_ofx_values"
    bl_label = "Edit Spektrafilm OFX Stock Defaults"
    bl_description = "Read this stock's actual native calibration and make these values editable"
    bl_options = {"REGISTER", "UNDO"}
    tree_name: StringProperty(options={"HIDDEN"})
    node_name: StringProperty(options={"HIDDEN"})
    group_key: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        node = _node(self, context)
        if not node:
            return {"CANCELLED"}
        try:
            from . import ofx_config
            values = engine.effective_parameters(parameters(node, explicit_only=True),
                                                  plugin_path=ofx_config.current_path(context))
            resolved = {p.key: values[p.key] for p in ofx_schema.PARAMETERS
                        if p.group == self.group_key and p.key in _STOCK_DERIVED
                        and not node.is_property_set(parameter_property(p.key))}
            for key, value in resolved.items():
                engine.validate_parameter(key, value)
            _changing.add(node.as_pointer())
            try:
                for key, value in resolved.items():
                    _set_native(node, key, value)
            finally:
                _changing.discard(node.as_pointer())
            _request(node)
        except (OSError, RuntimeError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


_CLASSES = (BR_OT_ofx_parameter_help, BR_NODE_spektrafilm_ofx, BR_OT_import_spektrafilm_ofx_preset,
            BR_OT_export_spektrafilm_ofx_preset, BR_OT_reset_spektrafilm_ofx,
            BR_OT_builtin_spektrafilm_ofx_preset, BR_OT_export_spektrafilm_ofx_lut,
            BR_OT_resolve_spektrafilm_ofx_values)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    _connections.clear()
    _changing.clear()
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
