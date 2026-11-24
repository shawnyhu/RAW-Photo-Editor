"""Check Mac decoder choices and migrate an Apple RAW (Auto) project across Blender saves.

With 0.15.3 installed: blender -b --python tests/apple_raw_selection.py -- write original.NEF old.blend
With the new installer: blender -b --python tests/apple_raw_selection.py -- check old.blend
On Windows: blender -b --python tests/apple_raw_selection.py -- windows original.NEF
"""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import bpy
from bl_ext.user_default.raw_photo_editor import catalog, compositor


def choices(recipe):
    prop = type(recipe).__annotations__['camera_input'].keywords
    return [(key, label) for key, label, _, _ in prop['items'](recipe, bpy.context)]


def write_legacy(original, filename):
    photo, _ = catalog.add_photo(bpy.context, original)
    tree = compositor.ensure_tree(bpy.context, photo)
    developer, = compositor.developers_for(photo)
    assert developer.camera_input == photo.camera_input == 'AUTO'
    for recipe in (photo, developer):
        assert ('APPLE_AUTO', 'Apple RAW (Auto)') in choices(recipe)
        recipe.camera_input = 'APPLE_AUTO'
        assert recipe['photo_editor_camera_input'] == 5
    photo['reader_camera_input'] = 'APPLE_AUTO'
    developer.name = 'Old Apple Auto'
    developer.apple_white_balance = 'CUSTOM'
    developer.temperature, developer.tint = 4300, -5
    # Other camera input values must retain their numeric meanings.
    for kind in ('AUTO', 'APPLE', 'DCP', 'ICC'):
        extra = tree.nodes.new(compositor.DEVELOPER_TYPE)
        extra.name = 'Existing ' + kind
        extra.camera_input = kind
    assert bpy.ops.wm.save_as_mainfile(filepath=str(filename)) == {'FINISHED'}


def check():
    photo, = catalog.library(bpy.context).photos
    tree = photo.tree
    developer = tree.nodes['Old Apple Auto']
    expected = [('AUTO', 'LibRaw'), ('APPLE', 'Apple RAW (Core Image)'),
                ('DCP', 'Custom DCP'), ('ICC', 'Custom ICC')]
    for recipe in (photo, developer):
        assert choices(recipe) == expected, choices(recipe)
        assert recipe.camera_input == 'APPLE'
        assert recipe['photo_editor_camera_input'] == 4, dict(recipe.items())
    assert (developer.apple_white_balance, developer.temperature, developer.tint) == ('CUSTOM', 4300, -5)
    for kind in ('AUTO', 'APPLE', 'DCP', 'ICC'):
        assert tree.nodes['Existing ' + kind].camera_input == kind
    for mode in ('preview', 'full'):
        assert catalog.request(photo, mode)['settings']['camera_input'] == 'APPLE'
        req = catalog.request(photo, mode, developer)
        assert req['settings']['camera_input'] == 'APPLE'
        assert req['settings']['white_balance'] == 'CUSTOM'
    new = tree.nodes.new(compositor.DEVELOPER_TYPE)
    assert new.camera_input == 'AUTO' and choices(new) == expected
    tree.nodes.remove(new)


def check_windows(original):
    photo, _ = catalog.add_photo(bpy.context, original)
    assert photo.camera_input == catalog.reader_camera_input(photo) == 'AUTO'
    assert [key for key, label in choices(photo)] == ['AUTO', 'DCP', 'ICC']
    # Store the fields written by the Mac version, including the old Auto value.
    photo['reader_camera_input'] = 'APPLE_AUTO'
    photo['photo_editor_camera_input'] = 5
    tree = compositor.ensure_tree(bpy.context, photo)
    developer, = compositor.developers_for(photo)
    developer.exposure = .75
    assert developer.camera_input == 'APPLE'
    with TemporaryDirectory(prefix='photo-editor-reader-') as folder:
        project = str(Path(folder) / 'mac-inputs.blend')
        assert bpy.ops.wm.save_as_mainfile(filepath=project) == {'FINISHED'}
        assert bpy.ops.wm.open_mainfile(filepath=project, load_ui=False) == {'FINISHED'}
        photo, = catalog.library(bpy.context).photos
        developer, = compositor.developers_for(photo)
        assert catalog.reader_needs_libraw(photo) and developer.camera_input == 'APPLE'
        assert bpy.ops.blender_raw.reader_use_libraw(photo_uid=photo.uid) == {'FINISHED'}
        assert developer.camera_input == 'APPLE' and photo.camera_input == 'APPLE'
        developer.camera_input = 'AUTO'
        assert bpy.ops.wm.save_as_mainfile(filepath=project) == {'FINISHED'}
        assert bpy.ops.wm.open_mainfile(filepath=project, load_ui=False) == {'FINISHED'}
        photo, = catalog.library(bpy.context).photos
        developer, = compositor.developers_for(photo)
        assert developer.exposure == .75 and not catalog.reader_needs_libraw(photo)
        for recipe in (None, developer):
            for mode in ('preview', 'full'):
                assert catalog.request(photo, mode, recipe)['settings']['camera_input'] == 'AUTO'
    print('PASS Windows LibRaw default, saved Apple choices, independent Reader/Developer conversion and reload')


mode, *args = sys.argv[sys.argv.index('--') + 1:]
if mode == 'write':
    original, filename = args
    write_legacy(Path(original).resolve(), Path(filename).resolve())
elif mode == 'check':
    assert bpy.ops.wm.open_mainfile(filepath=str(Path(args[0]).resolve()), load_ui=False) == {'FINISHED'}
    check()
    with TemporaryDirectory(prefix='photo-editor-apple-upgrade-') as temp:
        updated = str(Path(temp) / 'current.blend')
        assert bpy.ops.wm.save_as_mainfile(filepath=updated, copy=True) == {'FINISHED'}
        assert bpy.ops.wm.open_mainfile(filepath=updated, load_ui=False) == {'FINISHED'}
        check()
        lib = catalog.library(bpy.context)
        original = catalog.source_file(lib.photos[0])
        lib.photos.remove(0)
        fresh, created = catalog.add_photo(bpy.context, original)
        assert created and fresh.camera_input == 'AUTO'
        assert catalog.request(fresh, 'full')['settings']['camera_input'] == 'AUTO'
    print('PASS Mac menu, LibRaw default, previous Apple Auto, explicit inputs, RAW requests and reload')
elif mode == 'windows':
    assert sys.platform == 'win32'
    check_windows(Path(args[0]).resolve())
else:
    raise ValueError(mode)
