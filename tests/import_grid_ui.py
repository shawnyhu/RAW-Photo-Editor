"""Verify visible RAW tiles, point and box selection, and the original paths imported.

Run with --enable-event-simulate --python tests/import_grid_ui.py -- A.CR3 B.DNG [C.NEF D.ARW].
"""
from pathlib import Path
from tempfile import TemporaryDirectory
import os
import shutil
import sys
import time
import traceback

import bpy
from bl_ext.user_default.raw_photo_editor import catalog, import_preview, jobs


def wait(check):
    deadline = time.monotonic() + 60
    while not check():
        assert time.monotonic() < deadline, 'Timed out waiting for the RAW file browser'
        yield


def run(root, names):
    parent = bpy.context.window_manager.windows[0]
    area = max(parent.screen.areas, key=lambda a: a.width * a.height)
    region = next(r for r in area.regions if r.type == 'WINDOW')
    with bpy.context.temp_override(window=parent, area=area, region=region):
        assert bpy.ops.blender_raw.import_photos('INVOKE_DEFAULT', directory=str(root) + os.sep) == {'RUNNING_MODAL'}

    def browsers():
        return [(w, a) for w in bpy.context.window_manager.windows for a in w.screen.areas
                if a.type == 'FILE_BROWSER' and a.spaces.active.active_operator]

    yield from wait(browsers)
    window, area = browsers()[0]
    region = next(r for r in area.regions if r.type == 'WINDOW')
    session = import_preview._sessions[area.spaces.active.active_operator.as_pointer()]
    area.spaces.active.params.display_size = 128
    # The draw callback only uploads textures when each real image is visible on screen.
    yield from wait(lambda: len(session.textures) == len(names))
    with bpy.context.temp_override(window=window, area=area, region=region):
        assert not bpy.context.selected_files and not catalog.library(bpy.context).photos
        tiles = {session.entries[i].name: (region.x + x, region.y + y, w, h)
                 for i, x, y, w, h in session.tiles(bpy.context)}

    def point_on(name, fx=.5, fy=.5):
        x, y, w, h = tiles[name]
        return round(x + w * fx), round(y + h * fy)

    def gap_below(name):
        x, y, w, _ = tiles[name]
        point = round(x + w / 3), y - 2
        assert point[1] >= region.y and not any(
            tx <= point[0] < tx + tw and ty <= point[1] < ty + th for tx, ty, tw, th in tiles.values())
        return point

    def emit(kind, value, point, **modifiers):
        window.event_simulate(type=kind, value=value, x=point[0], y=point[1], **modifiers)
        yield

    def click(name, **modifiers):
        point = point_on(name)
        yield from emit('MOUSEMOVE', 'NOTHING', point)
        yield from emit('LEFTMOUSE', 'PRESS', point, **modifiers)
        yield from emit('LEFTMOUSE', 'RELEASE', point, **modifiers)

    def screenshot(variable):
        path = os.environ.get(variable)
        if path:
            with bpy.context.temp_override(window=window, area=area, region=region):
                bpy.ops.screen.screenshot(filepath=path)

    def box(start, end, *, keyboard=False, cancel=False, show_drag=False, **modifiers):
        yield from emit('MOUSEMOVE', 'NOTHING', start)
        if keyboard:
            yield from emit('B', 'PRESS', start)
            yield from emit('B', 'RELEASE', start)
        yield from emit('LEFTMOUSE', 'PRESS', start, **modifiers)
        yield from emit('MOUSEMOVE', 'NOTHING', end, **modifiers)
        if show_drag:
            screenshot('BLENDERRAW_IMPORT_DRAG_SCREENSHOT')
        if cancel:
            yield from emit('ESC', 'PRESS', end)
            yield from emit('ESC', 'RELEASE', end)
        yield from emit('LEFTMOUSE', 'RELEASE', end, **modifiers)

    def expect(*expected):
        assert browsers(), 'Selection unexpectedly closed the import dialog'
        with bpy.context.temp_override(window=window, area=area, region=region):
            selected = {f.relative_path for f in (bpy.context.selected_files or ())}
            assert selected == set(expected), (selected, expected)
            assert not catalog.library(bpy.context).photos

    a, b, c, d = tiles
    assert point_on(a)[1] == point_on(b)[1]
    yield from click(a)
    expect(a)
    yield from click(d, ctrl=True)
    expect(a, d)
    yield from click(b, shift=True)
    expect(b, c, d)
    screenshot('BLENDERRAW_IMPORT_SELECTED_SCREENSHOT')
    # Ordinary drags can start on an unselected card. The release replaces the old selection.
    yield from box(point_on(a, .25, .25), point_on(b, .75, .8), show_drag=True)
    expect(a, b)
    yield from box(gap_below(d), point_on(d), shift=True)
    expect(a, b, d)
    yield from box(gap_below(a), point_on(a), ctrl=True)
    expect(b, d)
    # Cancel both an ordinary drag and one started with the B shortcut.
    for keyboard in (False, True):
        yield from box(point_on(b, .8, .7), point_on(a, .2, .2), keyboard=keyboard, cancel=True)
        expect(b, d)
    yield from box(point_on(b, .8, .7), point_on(a, .2, .2), keyboard=True)
    expect(a, b)
    with bpy.context.temp_override(window=window, area=area, region=region):
        assert bpy.ops.file.execute() == {'FINISHED'}
    yield from wait(lambda: not browsers() and not import_preview._sessions)
    yield from wait(lambda: len(catalog.library(bpy.context).photos) > 0)
    assert {catalog.linked_file(photo) for photo in catalog.library(bpy.context).photos} == {root / a, root / b}
    jobs.stop_previews()
    print('PASS visible RAW tiles, click, Ctrl/Shift click, replace/add/subtract box, B, Esc, original paths', flush=True)


bpy.context.preferences.view.show_splash = False
sources = [Path(p).resolve() for p in sys.argv[sys.argv.index('--') + 1:]]
assert 2 <= len(sources) <= 4 and len({source.name.casefold() for source in sources}) == len(sources)
temporary = TemporaryDirectory(prefix='raw-grid-ui-')
root = Path(temporary.name).resolve()
names = [source.name for source in sources]
# Retain the two-input invocation while providing four distinct entries for range and subtraction tests.
for i in range(len(names), 4):
    alias = 'copy-' + sources[i % len(sources)].name
    while alias.casefold() in {name.casefold() for name in names}:
        alias = 'copy-' + alias
    names.append(alias)
for i, name in enumerate(names):
    source = sources[i % len(sources)]
    try:
        os.link(source, root / name)
    except OSError:
        shutil.copyfile(source, root / name)
steps = run(root, names)


def advance():
    try:
        next(steps)
        return .1
    except StopIteration:
        temporary.cleanup()
        bpy.ops.wm.quit_blender()
    except Exception:
        traceback.print_exc()
        sys.stderr.flush()
        os._exit(1)


bpy.app.timers.register(advance, first_interval=.5)
