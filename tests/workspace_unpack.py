"""Check the workspace confirmation and preservation of packed files in a real Blender window.

Run Blender with an isolated configuration, --enable-event-simulate and --python tests/workspace_unpack.py.
All input files, cache entries and the saved project are created inside a temporary directory.
"""
from hashlib import sha256
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback
from unittest.mock import patch
import wave

import bpy

installed = "bl_ext.user_default.raw_photo_editor" in bpy.context.preferences.addons
if installed:
    from bl_ext.user_default import raw_photo_editor as addon
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import blender_raw as addon


def write_image(path, color):
    source = bpy.data.images.new("Temporary pixels", width=8, height=6, alpha=False,
                                 float_buffer=path.suffix == ".exr")
    try:
        source.generated_color = (*color, 1.0)
        source.filepath_raw = str(path)
        source.file_format = "OPEN_EXR" if path.suffix == ".exr" else "PNG"
        source.save()
    finally:
        bpy.data.images.remove(source)


def load_image(path, name):
    image = bpy.data.images.load(str(path))
    image.name = name
    image.use_fake_user = True
    return image


def send(window, key):
    window.event_simulate(type=key, value="PRESS")
    window.event_simulate(type=key, value="RELEASE")


def wait_for(check, message):
    deadline = time.monotonic() + 18
    while not check():
        assert time.monotonic() < deadline, message
        yield


def invoke(window, operator):
    area = max(window.screen.areas, key=lambda a: a.width * a.height)
    region = next(r for r in area.regions if r.type == "WINDOW")
    with bpy.context.temp_override(window=window, area=area, region=region):
        return operator("INVOKE_DEFAULT")


def test(folder):
    if not installed:
        addon.register()
    window = bpy.context.window_manager.windows[0]
    assert not bpy.data.is_saved, "Run this regression in a fresh, isolated Blender instance"
    bpy.context.preferences.view.show_splash = False
    send(window, "ESC")
    yield

    sources = folder / "source"
    sources.mkdir()
    original_paths = {}
    payloads = {}
    sample_pixels = {}
    for name, color in (("Matching", (.8, .1, .2)), ("Missing", (.2, .1, .8)), ("Conflict", (.4, .8, .1))):
        path = sources / (name + ".png")
        write_image(path, color)
        image = load_image(path, name)
        original_paths[name] = path
        payloads[name] = path.read_bytes()
        sample_pixels[name] = tuple(image.pixels[:4])

    sound_path = sources / "Missing sound.wav"
    with wave.open(str(sound_path), "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x00\x00\x00\x08" * 128)
    sound = bpy.data.sounds.load(str(sound_path))
    sound.name, sound.use_fake_user = "Missing sound", True
    original_paths[sound.name] = sound_path
    payloads[sound.name] = sound_path.read_bytes()

    cache_key = sha256(b"workspace-unpack-collision").hexdigest()
    cache_path = addon.catalog.cache_dir() / (cache_key + ".exr")
    write_image(cache_path, (.2, .3, .7))
    cached = load_image(cache_path, "Preview cache")
    cached[addon.catalog._CACHE_KEY] = cache_key
    original_paths[cached.name] = cache_path
    payloads[cached.name] = cache_path.read_bytes()
    sample_pixels[cached.name] = tuple(cached.pixels[:4])

    lib = addon.catalog.library(bpy.context)
    lib.pack_cache_images = True
    bpy.data.use_autopack = True
    assert bpy.ops.file.pack_all() == {"FINISHED"}
    project = folder / "Workspace.blend"
    assert bpy.ops.wm.save_as_mainfile(filepath=str(project)) == {"FINISHED"}
    original_paths["Missing"].unlink()
    sound_path.unlink()
    write_image(original_paths["Conflict"], (.9, .9, .9))
    write_image(cache_path, (.1, .1, .1))
    conflicts = {name: original_paths[name].read_bytes() for name in ("Conflict", "Preview cache")}
    assert all(conflicts[name] != payloads[name] for name in conflicts)

    def blocks():
        return {**{name: bpy.data.images[name] for name in sample_pixels},
                "Missing sound": bpy.data.sounds["Missing sound"]}

    def verify_packed():
        assert lib.pack_cache_images and bpy.data.use_autopack
        assert all(block.packed_file and bytes(block.packed_file.data) == payloads[name]
                   for name, block in blocks().items())
        assert not original_paths["Missing"].exists() and not sound_path.exists()
        assert all(original_paths[name].read_bytes() == data for name, data in conflicts.items())

    original_workspace = window.workspace
    verify_packed()
    for operator in (bpy.ops.blender_raw.new_workspace, bpy.ops.blender_raw.open_workspace):
        assert invoke(window, operator) == {"RUNNING_MODAL"}, operator
        yield
        send(window, "ESC")
        yield
        verify_packed()
        assert window.workspace == original_workspace
        assert not any(w.get("blender_raw_workspace") for w in bpy.data.workspaces)
    print("CANCEL on New and Open keeps workspace, both packing settings and all five packed payloads", flush=True)

    assert invoke(window, bpy.ops.blender_raw.open_workspace) == {"RUNNING_MODAL"}
    yield
    send(window, "RET")
    yield from wait_for(lambda: window.workspace.get("blender_raw_workspace"), "Confirmation did not open the workspace")
    yield from wait_for(lambda: not addon.gallery._timers, "Workspace layout did not finish")

    def verify_disk():
        lib = addon.catalog.library(bpy.context)
        assert not bpy.data.use_autopack and not lib.pack_cache_images
        assert not lib.cache_storage_error, lib.cache_storage_error
        for name, block in blocks().items():
            assert not block.packed_file, name
            path = Path(bpy.path.abspath(block.filepath)).resolve()
            assert path.is_relative_to(folder.resolve()), (name, path)
            assert path.is_file() and path.read_bytes() == payloads[name], (name, path)
            if name == "Matching":
                assert path == original_paths[name].resolve(), path
            if name in conflicts:
                assert path != original_paths[name].resolve(), (name, path)
                assert original_paths[name].read_bytes() == conflicts[name], name
            if name in sample_pixels:
                block.reload()
                assert tuple(block.size) == (8, 6) and tuple(block.pixels[:4]) == sample_pixels[name], name
            else:
                with wave.open(str(path), "rb") as audio:
                    assert audio.getnframes() == 256 and audio.getframerate() == 8000
        print("Verified four reloadable images and one WAV with their original bytes", flush=True)

    verify_disk()
    assert bpy.ops.wm.save_as_mainfile(filepath=str(project)) == {"FINISHED"}
    assert bpy.ops.wm.open_mainfile(filepath=str(project)) == {"FINISHED"}
    verify_disk()
    print("PASS confirmed project reopened with both packing options off; both disk conflicts survived", flush=True)


def test_unsaved(folder):
    assert bpy.ops.wm.read_homefile(use_factory_startup=True, use_empty=True) == {"FINISHED"}
    window = bpy.context.window_manager.windows[0]
    path = folder / "Unsaved.png"
    write_image(path, (.7, .2, .3))
    original = path.read_bytes()
    image = load_image(path, "Unsaved project image")
    image.pack()
    path.unlink()
    # Use Blender's real persistent datafiles routing, isolated from the user's profile.
    datafiles = folder / "user-datafiles"
    datafiles.mkdir()
    with patch.dict(os.environ, {"BLENDER_USER_DATAFILES": str(datafiles)}):
        with bpy.context.temp_override(window=window, area=window.screen.areas[0]):
            assert bpy.ops.blender_raw.open_workspace() == {"FINISHED"}
        yield from wait_for(lambda: window.workspace.get("blender_raw_workspace"), "Unsaved workspace did not open")
        recovered = Path(bpy.path.abspath(image.filepath)).resolve()
        assert not image.packed_file and recovered.is_relative_to(datafiles)
        assert recovered.read_bytes() == original
        project = folder / "First Save.blend"
        assert bpy.ops.wm.save_as_mainfile(filepath=str(project)) == {"FINISHED"}
        assert bpy.ops.wm.open_mainfile(filepath=str(project)) == {"FINISHED"}
        image = bpy.data.images["Unsaved project image"]
        image.reload()
        assert not image.packed_file and tuple(image.size) == (8, 6)
        assert Path(bpy.path.abspath(image.filepath)).resolve() == recovered
    print("PASS unsaved resource used persistent storage and reloaded after first Save As", flush=True)


def run():
    with tempfile.TemporaryDirectory(prefix="photo-workspace-unpack-") as path:
        # Isolate the real add-on cache location, including the deliberately conflicting EXR.
        with patch.object(tempfile, "tempdir", path):
            yield from test(Path(path))
            yield from test_unsaved(Path(path))


steps = run()


def advance():
    try:
        next(steps)
        return .18
    except StopIteration:
        bpy.ops.wm.quit_blender()
    except Exception:
        traceback.print_exc()
        sys.stderr.flush()
        os._exit(1)


bpy.app.timers.register(advance, first_interval=1., persistent=True)
