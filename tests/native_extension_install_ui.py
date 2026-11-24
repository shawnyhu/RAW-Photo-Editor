"""Real Extension Manager install/upgrade regression in an isolated profile.

Modes after --:
  fresh NEW.zip
  upgrade OLD.zip NEW.zip
  broken-recovery OLD.zip BROKEN.zip NEW.zip
  restart NEW.zip

For restart, reuse broken-recovery's profile without --factory-startup.
"""
import importlib
import os
from pathlib import Path
import sys
import tomllib
import traceback
import zipfile

import addon_utils
import bpy

mode, *packages = sys.argv[sys.argv.index('--') + 1:]
packages = [Path(package).resolve() for package in packages]
with zipfile.ZipFile(packages[-1]) as package:
    manifest = tomllib.loads(package.read('blender_manifest.toml').decode())
    package_id = manifest['id']
    expected_version = manifest['version']
MODULE = f'bl_ext.user_default.{package_id}'


def install(package):
    print('INSTALL_BEGIN', package.name, flush=True)
    result = bpy.ops.extensions.package_install_files(
        filepath=str(package), repo='user_default', enable_on_install=True)
    assert result == {'FINISHED'}, result
    print('INSTALL_FINISHED', package.name, flush=True)


def installed_manifest():
    module = sys.modules.get(MODULE)
    if module is not None:
        directory = Path(module.__file__).parent
    else:
        # Blender removes a module whose registration failed; the package
        # files have already been updated and are available after restart.
        directory = Path(os.environ['BLENDER_USER_EXTENSIONS']) / 'user_default' / package_id
    return tomllib.loads((directory / 'blender_manifest.toml').read_text())


def assert_enabled():
    module = importlib.import_module(MODULE)
    assert addon_utils.check(MODULE)[1], addon_utils.check(MODULE)
    assert module.catalog.BR_Photo.is_registered
    assert bpy.types.PropertyGroup.bl_rna_get_subclass_py('BR_Photo') is module.catalog.BR_Photo
    return module


def draw_native_cards(window, module):
    scene = window.scene
    scene[module.compositor.STUDIO_TAG] = 'extension-install-regression'
    if not scene.compositor_effects:
        module.native_effects.create_effect(scene, module.native_effects.CUSTOM_TYPE)
        module.native_effects.create_effect(scene, module.native_effects.CUSTOM_TYPE)
    area = max(window.screen.areas, key=lambda item: item.width * item.height)
    area.type = 'PROPERTIES'
    area.spaces.active.context = 'COMPOSITOR'
    for _ in range(12):
        area.tag_redraw()
        yield
    if hasattr(module.native_effects, '_ui_panel_ready'):
        region = next(item for item in area.regions if item.type == 'WINDOW')
        with bpy.context.temp_override(window=window, area=area, region=region):
            assert module.native_effects._ui_panel_ready(bpy.context)
        assert not any(scene.get(module.native_effects._UI_RESET_TAG) for scene in bpy.data.scenes)


def run():
    profile = os.environ.get('BLENDER_USER_RESOURCES', '')
    assert 'profile-upgrade-probe' in profile, 'Use an isolated upgrade probe profile'
    temporary = Path(profile) / 'tmp'
    temporary.mkdir(parents=True, exist_ok=True)
    bpy.context.preferences.filepaths.temporary_directory = str(temporary)
    print('PROFILE', bpy.utils.user_resource('CONFIG'), flush=True)
    window = bpy.context.window_manager.windows[0]
    window.event_simulate(type='ESC', value='PRESS')
    window.event_simulate(type='ESC', value='RELEASE')
    yield
    if mode == 'fresh':
        assert not hasattr(bpy.types.Scene, 'blender_raw')
        install(packages[0])
    elif mode in {'upgrade', 'broken-recovery'}:
        install(packages[0])
        old_module = assert_enabled()
        yield from draw_native_cards(window, old_module)
        if mode == 'broken-recovery':
            try:
                install(packages[1])
            except RuntimeError as exc:
                assert '_RestrictData' in str(exc) and 'scenes' in str(exc), str(exc)
                print('EXPECTED_0191_REGISTRATION_FAILURE', flush=True)
            else:
                raise AssertionError('Broken 0.19.1 unexpectedly enabled')
            try:
                install(packages[2])
            except RuntimeError as exc:
                assert "already registered as a subclass 'BR_Photo'" in str(exc), str(exc)
                print('EXPECTED_PARTIAL_REGISTRATION_REQUIRES_RESTART', flush=True)
            manifest = installed_manifest()
            assert manifest['version'] == expected_version, manifest
            bpy.ops.wm.save_userpref()
            print('NATIVE_EXTENSION_RECOVERY_FILES_UPDATED_PASS', flush=True)
            return
        install(packages[1])
    elif mode == 'restart':
        # Real restart may already enable the fixed package from saved prefs.
        print('RESTART_INITIAL_ENABLED', addon_utils.check(MODULE), flush=True)
        if not addon_utils.check(MODULE)[1]:
            assert bpy.ops.preferences.addon_enable(module=MODULE) == {'FINISHED'}
    else:
        raise ValueError(mode)
    module = assert_enabled()
    assert installed_manifest()['version'] == expected_version
    yield from draw_native_cards(window, module)
    print('NATIVE_EXTENSION_' + mode.upper() + '_PASS', flush=True)


iterator = run()


def tick():
    try:
        next(iterator)
        return .2
    except StopIteration:
        bpy.ops.wm.quit_blender()
    except BaseException:
        traceback.print_exc()
        sys.stderr.flush()
        os._exit(1)
    return None


bpy.app.timers.register(tick, first_interval=.5)
