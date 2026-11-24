# SPDX-License-Identifier: GPL-3.0-or-later
"""Locate the complete, separately installed official Spektrafilm OFX bundle."""
import os
from pathlib import Path

import bpy
from bpy.props import BoolProperty, StringProperty


ENVIRONMENT = 'BLENDER_PHOTO_SPEKTRAFILM_OFX'


def preferences(context=None):
    context = context or bpy.context
    addon = context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


def current_path(context=None):
    prefs = preferences(context)
    path = prefs.spektrafilm_ofx_path if prefs else ''
    return bpy.path.abspath(path) if path else os.environ.get(ENVIRONMENT, '')


def changed(_self, context):
    from . import film_node, film_preview, spektrafilm_ofx_engine
    spektrafilm_ofx_engine.configure(current_path(context))
    for tree in bpy.data.node_groups:
        if tree.bl_idname == 'CompositorNodeTree' and not tree.library:
            for node in tree.nodes:
                if node.bl_idname == film_node.OFX_TYPE:
                    film_preview.discard_scan(node)
                    film_preview.request(node)


def draw_setup(layout, context):
    from . import spektrafilm_ofx_engine
    error = spektrafilm_ofx_engine.backend_error(current_path(context))
    if error:
        box = layout.box()
        box.label(text='Spektrafilm OFX setup required', icon='INFO')
        box.label(text=error)
        box.operator('blender_raw.locate_spektrafilm_ofx', icon='FILE_FOLDER')
        box.operator('wm.url_open', text='Download Official OFX', icon='URL').url = 'https://spektrafilm.114c.de/'


class BR_OT_locate_spektrafilm_ofx(bpy.types.Operator):
    bl_idname = 'blender_raw.locate_spektrafilm_ofx'
    bl_label = 'Locate Spektrafilm OFX Folder'
    bl_description = 'Select the installed plugin folder or the fully extracted official download'

    directory: StringProperty(subtype='DIR_PATH')
    filter_folder: BoolProperty(default=True, options={'HIDDEN'})

    def invoke(self, context, _event):
        self.directory = current_path(context)
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        from . import spektrafilm_ofx_engine
        path = str(Path(bpy.path.abspath(self.directory)).resolve())
        error = spektrafilm_ofx_engine.backend_error(path)
        if error:
            self.report({'ERROR'}, error)
            return {'CANCELLED'}
        prefs = preferences(context)
        if prefs:
            prefs.spektrafilm_ofx_path = path
        else:
            # Source-checkout sessions have no enabled AddonPreferences entry.
            os.environ[ENVIRONMENT] = path
            changed(None, context)
        self.report({'INFO'}, 'Spektrafilm OFX folder connected')
        return {'FINISHED'}


def register():
    bpy.utils.register_class(BR_OT_locate_spektrafilm_ofx)


def unregister():
    bpy.utils.unregister_class(BR_OT_locate_spektrafilm_ofx)
