# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

import bpy
from bpy.props import BoolProperty, CollectionProperty, StringProperty
from bpy_extras.io_utils import ImportHelper

from . import catalog, compositor, filters, import_preview, jobs, naming


RAW_SUFFIXES = filters.RAW_SUFFIXES
RAW_FILTER = ";".join("*." + ext for ext in sorted(RAW_SUFFIXES))


def _selected(context):
    return [p for p in catalog.library(context).photos if p.selected]


class BR_OT_import_photos(bpy.types.Operator, ImportHelper):
    bl_idname = "blender_raw.import_photos"
    bl_label = "Import RAW Photos"
    bl_description = "Add camera originals to Photo Gallery"
    bl_options = {"REGISTER", "UNDO"}
    files: CollectionProperty(type=bpy.types.OperatorFileListElement, options={"HIDDEN", "SKIP_SAVE"})
    directory: StringProperty(subtype="DIR_PATH")
    filter_glob: StringProperty(default=RAW_FILTER, options={"HIDDEN"})
    pack: BoolProperty(name="Embed Originals", description="Store the original file bytes inside the Blender project")

    def invoke(self, context, event):
        import_preview.open_dialog(self)
        return ImportHelper.invoke(self, context, event)

    def draw(self, context):
        self.layout.prop(self, 'pack')

    def cancel(self, context):
        import_preview.close_dialog(self)

    def execute(self, context):
        import_preview.close_dialog(self)
        paths = []
        if self.files:
            directory = catalog.resolved(self.directory)
            paths = [directory / item.name for item in self.files if item.name]
        if not paths and self.filepath:
            paths = [catalog.resolved(self.filepath)]
        if not paths:
            self.report({"ERROR"}, "Choose at least one RAW file")
            return {"CANCELLED"}
        added = existing = 0
        errors = []
        path_index = catalog.photo_path_index(catalog.library(context).photos) if len(paths) > 1 else None
        for path in paths:
            try:
                if path.suffix.lower().lstrip(".") not in RAW_SUFFIXES:
                    raise ValueError(f"Unrecognized RAW extension: {path.suffix}")
                photo, new = catalog.add_photo(context, path, self.pack, path_index=path_index)
                compositor.ensure_photo_scene(context, photo)
                jobs.thumbnail_later(photo)
                added += int(new)
                existing += int(not new)
            except Exception as exc:
                errors.append(f"{path.name}: {exc}")
        if errors and not (added or existing):
            self.report({"ERROR"}, errors[0])
            return {"CANCELLED"}
        msg = f"{added} imported, {existing} already in Photo Gallery"
        if errors:
            msg += f". {len(errors)} failed: {errors[0]}"
        self.report({"WARNING" if errors else "INFO"}, msg)
        jobs.redraw()
        return {"FINISHED"}


class BR_OT_open_photo(bpy.types.Operator):
    bl_idname = "blender_raw.open_photo"
    bl_label = "Develop Photo"
    bl_description = "Open this photo's scene and native effects stack"
    uid: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        lib = catalog.library(context)
        index = next((i for i, p in enumerate(lib.photos) if p.uid == self.uid), -1)
        if index < 0:
            self.report({"ERROR"}, "Photo not found")
            return {"CANCELLED"}
        lib.active_index = index
        photo = lib.photos[index]
        try:
            compositor.open_photo(context, photo)
            jobs.open_preview(photo)
        except Exception as exc:
            photo.status, photo.error = "Develop failed", str(exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        jobs.redraw()
        return {"FINISHED"}


class BR_OT_pack_selected(bpy.types.Operator):
    bl_idname = "blender_raw.pack_selected"
    bl_label = "Embed Selected Originals"
    bl_description = "Store the selected originals and their active camera profiles and 3D LUTs in this project"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(context.scene and _selected(context))

    def execute(self, context):
        done, failures = 0, []
        for photo in _selected(context):
            try:
                catalog.pack_photo(photo)
                done += 1
            except Exception as exc:
                failures.append(f"{photo.name}: {exc}")
        message = f"Embedded {done} originals. Save the project to keep them."
        if failures:
            message += f" {len(failures)} failed: {failures[0]}"
        self.report({"WARNING" if failures else "INFO"}, message)
        return {"FINISHED"} if done else {"CANCELLED"}


class BR_OT_relink_photo(bpy.types.Operator, ImportHelper):
    bl_idname = "blender_raw.relink_photo"
    bl_label = "Locate Original"
    bl_options = {"REGISTER", "UNDO"}
    uid: StringProperty(options={"HIDDEN"})
    filter_glob: StringProperty(default=RAW_FILTER, options={"HIDDEN"})

    def execute(self, context):
        photo = catalog.lookup_photo(context.scene, self.uid)
        path = catalog.resolved(self.filepath)
        if not photo or not path.is_file() or path.suffix.lower().lstrip(".") not in RAW_SUFFIXES:
            self.report({"ERROR"}, "Choose an existing RAW file")
            return {"CANCELLED"}
        if photo.packed:
            self.report({"ERROR"}, "This original is already embedded")
            return {"CANCELLED"}
        photo.filepath, photo.name = catalog.stored_path(path), path.name
        photo.source_absolute = str(path)
        filters.index_photo(photo, path)
        photo.thumbnail_path = ""
        for key in ("thumbnail_bytes", "edited_thumbnail_path", "edited_thumbnail_bytes", "thumbnail_error"):
            if key in photo:
                del photo[key]
        jobs.thumbnail_later(photo)
        if photo.scene:
            jobs.preview_later(photo)
        return {"FINISHED"}


class BR_OT_remove_photo(bpy.types.Operator):
    bl_idname = "blender_raw.remove_photo"
    bl_label = "Remove from Photo Gallery"
    bl_description = "Remove selected photos from this Gallery. Keep originals and exported files on disk"
    bl_options = {"REGISTER", "UNDO"}
    uid: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def _targets(self, context):
        if self.uid:
            return {self.uid}
        return {p.uid for p in _selected(context)}

    def invoke(self, context, event):
        from . import gallery
        if event.type in {'X', 'DEL'} and not (
                gallery._is_gallery(context) and context.region and context.region.type == 'WINDOW'):
            return {'PASS_THROUGH'}
        self._confirmed_uids = self._targets(context)
        if not self._confirmed_uids:
            return {'PASS_THROUGH'}
        count = len(self._confirmed_uids)
        title = f"Remove {count} Photo{'s' if count != 1 else ''} from Gallery?"
        message = "Originals and exported files on disk are kept."
        if any(p.packed and p.uid in self._confirmed_uids for p in catalog.library(context).photos):
            message = "This also removes the Gallery entries for embedded originals. Files on disk are kept."
        return context.window_manager.invoke_confirm(
            self, event, title=title, message=message, confirm_text="Remove")

    def execute(self, context):
        lib = catalog.library(context)
        targets = getattr(self, "_confirmed_uids", None)
        if targets is None:
            targets = self._targets(context)
        indices = [i for i, p in enumerate(lib.photos) if p.uid in targets]
        if not indices:
            return {"CANCELLED"}
        # An in-flight export owns its saved snapshot. Only stop previews in this project.
        jobs.stop_previews(targets)
        active = lib.active_index
        for i in reversed(indices):
            lib.photos.remove(i)
        lib.active_index = (max(0, min(active - sum(i < active for i in indices), len(lib.photos) - 1))
                            if lib.photos else -1)
        next_photo = lib.photos[lib.active_index] if lib.active_index >= 0 else None
        compositor.replace_removed_photo(context, targets, next_photo)
        jobs.redraw()
        count = len(indices)
        self.report({"INFO"}, f"Removed {count} photo{'s' if count != 1 else ''} from Gallery")
        return {"FINISHED"}


class BR_OT_export_photos(bpy.types.Operator):
    bl_idname = "blender_raw.export_photos"
    bl_label = "Export Selected Photos"
    bl_description = "Develop the selected photos at full resolution in the background"
    directory: StringProperty(name="Output Directory", subtype="DIR_PATH")
    filter_folder: BoolProperty(default=True, options={"HIDDEN"})
    # Compatibility with scripts that specified a format in version 0.1.
    format: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        return bool(context.scene and not jobs.export_running() and _selected(context))

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def draw(self, context):
        self.layout.use_property_split = True
        self.layout.use_property_decorate = False
        self.layout.template_image_settings(context.scene.render.image_settings, color_management=True)
        naming.draw(self.layout, context, _selected(context), catalog.library(context), self.directory)
        if context.scene.render.is_movie_format:
            self.layout.label(text="Choose an image format for photo export.", icon="ERROR")

    def execute(self, context):
        if not self.directory.strip():
            self.report({"ERROR"}, "Choose an output directory")
            return {"CANCELLED"}
        try:
            jobs.start_export(context, _selected(context), bpy.path.abspath(self.directory), self.format,
                              output_settings_scene=context.scene)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, "Export started")
        return {"FINISHED"}


class BR_OT_cancel_export(bpy.types.Operator):
    bl_idname = "blender_raw.cancel_export"
    bl_label = "Cancel Export"

    @classmethod
    def poll(cls, context):
        return jobs.export_running()

    def execute(self, context):
        jobs.cancel_export()
        return {"FINISHED"}


_classes = (import_preview.BR_OT_import_grid_events, BR_OT_import_photos, BR_OT_open_photo, BR_OT_pack_selected, BR_OT_relink_photo,
            BR_OT_remove_photo, BR_OT_export_photos, BR_OT_cancel_export)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.app.handlers.load_pre.append(import_preview.clear)


def unregister():
    if import_preview.clear in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(import_preview.clear)
    import_preview.clear()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
