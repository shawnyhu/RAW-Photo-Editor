# SPDX-License-Identifier: GPL-3.0-or-later
"""Spektrafilm Lite user preset controls for both effect cards and node editors."""
import bpy
from bpy.props import StringProperty

from . import film_node, film_user_library, film_user_settings


class BR_OT_save_film_preset(bpy.types.Operator):
    bl_idname = "blender_raw.save_film_preset"
    bl_label = "Save Spektrafilm Lite Preset"
    bl_description = "Save this node's film settings for use in any project"
    tree_name: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    node_name: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    preset_name: StringProperty(name="Name", options={"SKIP_SAVE"})

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, confirm_text="Save")

    def execute(self, context):
        node = film_node._operator_node(self, context, film_node.FILM_TYPE)
        if node is None:
            return {"CANCELLED"}
        try:
            film_user_library.save(self.preset_name, film_user_settings.snapshot(node))
        except (OSError, RuntimeError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


class BR_OT_apply_film_preset(bpy.types.Operator):
    bl_idname = "blender_raw.apply_film_preset"
    bl_label = "Apply Spektrafilm Lite Preset"
    bl_description = "Apply the saved film settings to this node"
    bl_options = {"UNDO"}
    tree_name: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    node_name: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    preset_id: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        node = film_node._operator_node(self, context, film_node.FILM_TYPE)
        if node is None:
            return {"CANCELLED"}
        try:
            film_user_settings.apply(node, film_user_library.load(self.preset_id))
        except (OSError, RuntimeError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


class BR_OT_rename_film_preset(bpy.types.Operator):
    bl_idname = "blender_raw.rename_film_preset"
    bl_label = "Rename Spektrafilm Lite Preset"
    preset_id: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    preset_name: StringProperty(name="Name", options={"SKIP_SAVE"})

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, confirm_text="Rename")

    def execute(self, context):
        try:
            film_user_library.rename(self.preset_id, self.preset_name)
        except (OSError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


class BR_OT_delete_film_preset(bpy.types.Operator):
    bl_idname = "blender_raw.delete_film_preset"
    bl_label = "Delete Spektrafilm Lite Preset"
    preset_id: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    preset_name: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(
            self, event, title=self.bl_label, message=f'Delete “{self.preset_name}” from your presets?',
            confirm_text="Delete", icon="WARNING")

    def execute(self, context):
        try:
            film_user_library.delete(self.preset_id)
        except (OSError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


class BR_PT_spektrafilm_presets(bpy.types.Panel):
    bl_label = "Spektrafilm Lite Presets"
    bl_space_type = "NODE_EDITOR"
    bl_region_type = "HEADER"
    bl_ui_units_x = 18

    def draw(self, context):
        node = getattr(context, "spektrafilm_preset_node", None)
        if node is None:
            return
        layout = self.layout
        layout.operator_context = "INVOKE_DEFAULT"
        film_node._action(layout, BR_OT_save_film_preset.bl_idname, node, "Save Current as Preset…", "ADD")
        try:
            presets = film_user_library.list_presets()
        except (OSError, ValueError) as error:
            layout.label(text=str(error), icon="ERROR")
            presets = []
        if presets:
            layout.separator()
        for preset_id, name in presets:
            row = layout.row(align=True)
            apply = film_node._action(row, BR_OT_apply_film_preset.bl_idname, node, name, "PRESET")
            apply.preset_id = preset_id
            for action, icon in ((BR_OT_rename_film_preset, "GREASEPENCIL"), (BR_OT_delete_film_preset, "X")):
                op = row.operator(action.bl_idname, text="", icon=icon)
                op.preset_id, op.preset_name = preset_id, name
        layout.separator()
        layout.label(text="Built-in Presets")
        for preset_id, name in film_user_library.BUILTIN_PRESETS.items():
            op = film_node._action(layout, BR_OT_apply_film_preset.bl_idname, node, name, "PRESET")
            op.preset_id = preset_id


_CLASSES = (BR_OT_save_film_preset, BR_OT_apply_film_preset, BR_OT_rename_film_preset,
            BR_OT_delete_film_preset, BR_PT_spektrafilm_presets)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
