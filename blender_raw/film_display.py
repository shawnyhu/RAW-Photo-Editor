# SPDX-License-Identifier: GPL-3.0-or-later
"""Present linear photo composites through a single SDR sRGB display transform."""
import bpy


def _standard_srgb(scene):
    view, display = scene.view_settings, scene.display_settings
    return (display.display_device == "sRGB" and display.emulation == "AUTO"
            and view.view_transform == "Standard" and view.look == "None"
            and view.exposure == 0 and view.gamma == 1
            and not view.use_curve_mapping and not view.use_white_balance)


def _apply(scene):
    if _standard_srgb(scene):
        return True
    view, display = scene.view_settings, scene.display_settings
    previous = display.display_device, view.view_transform, view.look
    try:
        display.display_device = "sRGB"
        view.view_transform = "Standard"
        view.look = "None"
    except TypeError:  # A custom OCIO configuration may omit this display or view.
        display.display_device, view.view_transform, view.look = previous
        return False
    display.emulation = "AUTO"
    view.exposure, view.gamma = 0.0, 1.0
    view.use_curve_mapping = view.use_white_balance = False
    return True


def sync(scene):
    from .compositor import STUDIO_TAG
    if scene and scene.is_editable and scene.get(STUDIO_TAG) and not scene.get("photo_editor_display_initialized"):
        # Older darkrooms remembered an AgX view to restore when a scan was bypassed.
        for key in ("photo_editor_film_previous_view", "photo_editor_auto_film_view"):
            if key in scene:
                del scene[key]
        if not _apply(scene):
            print(f"RAW Photo Editor: {scene.name} requires an OCIO configuration with sRGB / Standard")
        else:
            scene["photo_editor_display_initialized"] = True


def draw(context, layout):
    scene = context.scene
    if not scene:
        return
    layout.label(text=f"{scene.display_settings.display_device} / {scene.view_settings.view_transform}")
    if not _standard_srgb(scene):
        layout.operator("blender_raw.film_sdr_view", icon="COLOR")
    if scene.render.image_settings.color_management == "OVERRIDE" and not scene.render.image_settings.has_linear_colorspace:
        layout.label(text="Export uses a color override", icon="INFO")


class BR_OT_film_sdr_view(bpy.types.Operator):
    bl_idname = "blender_raw.film_sdr_view"
    bl_label = "Use Standard sRGB View"
    bl_description = "Display this scene in sRGB with the Standard view, neutral exposure and gamma"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.scene and context.scene.is_editable

    def execute(self, context):
        if not _apply(context.scene):
            self.report({"WARNING"}, "The active OCIO configuration has no matching sRGB / Standard view")
            return {"CANCELLED"}
        return {"FINISHED"}


def register():
    bpy.utils.register_class(BR_OT_film_sdr_view)


def unregister():
    bpy.utils.unregister_class(BR_OT_film_sdr_view)
