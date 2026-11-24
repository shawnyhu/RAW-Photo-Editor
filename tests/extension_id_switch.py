"""Exercise switching from the installed legacy ID in an isolated Blender profile."""
import addon_utils
import bpy

OLD = "bl_ext.user_default.blender_raw"
NEW = "bl_ext.user_default.raw_photo_editor"

assert addon_utils.check(OLD)[1] and not addon_utils.check(NEW)[1]
legacy_photo = bpy.types.PropertyGroup.bl_rna_get_subclass_py("BR_Photo")
errors = []
assert addon_utils.enable(NEW, handle_error=errors.append) is None
assert len(errors) == 1 and "Disable the other copy" in str(errors[0]), errors
assert addon_utils.check(OLD)[1]
assert bpy.types.PropertyGroup.bl_rna_get_subclass_py("BR_Photo") is legacy_photo

addon_utils.disable(OLD, default_set=True)
addon = addon_utils.enable(NEW, default_set=True)
assert addon and bpy.types.PropertyGroup.bl_rna_get_subclass_py("BR_Photo") is addon.catalog.BR_Photo
assert NEW in bpy.context.preferences.addons and OLD not in bpy.context.preferences.addons
assert bpy.ops.wm.save_userpref() == {"FINISHED"}
