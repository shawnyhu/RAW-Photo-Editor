# SPDX-License-Identifier: GPL-3.0-or-later
import bpy

from . import crop_editor, levels_node, levels_ui, viewer_background
from . import catalog, compositor, film_preview, filters, gallery, jobs, native_effects, node_recovery, node_status, operators


def register():
    if hasattr(bpy.types.Scene, 'blender_raw'):
        raise RuntimeError('Disable the other copy of the photo editor in Preferences before enabling this extension.')
    catalog.register()
    crop_editor.register()
    levels_node.register()
    levels_ui.register()
    compositor.register()
    operators.register()
    native_effects.register()
    gallery.register()
    node_status.register()
    node_recovery.register()
    jobs.register()
    film_preview.register()
    filters.register()
    viewer_background.register()


def unregister():
    # Clear native instanced-panel animations before any other module removes
    # a Panel class (Blender's RNA unregistration frees native instances too).
    native_effects.unregister()
    viewer_background.unregister()
    filters.unregister()
    film_preview.unregister()
    jobs.unregister()
    node_recovery.unregister()
    node_status.unregister()
    gallery.unregister()
    operators.unregister()
    compositor.unregister()
    levels_ui.unregister()
    levels_node.unregister()
    crop_editor.unregister()
    catalog.unregister()
