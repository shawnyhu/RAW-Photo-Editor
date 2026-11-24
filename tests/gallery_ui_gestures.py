"""Exercise the real Gallery, menus, wheel and area resizing in a fresh Blender window.

blender --factory-startup --enable-event-simulate -p 50 50 1440 920 --python tests/gallery_ui_gestures.py
"""
from pathlib import Path
import os
import sys
import tempfile
import traceback

import bpy


bpy.context.preferences.view.show_splash = False
installed = "bl_ext.user_default.raw_photo_editor" in bpy.context.preferences.addons
if installed:
    from bl_ext.user_default import raw_photo_editor as extension
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import blender_raw as extension


def run():
    if not installed:
        extension.register()
    gallery = extension.gallery
    assert gallery.BlenderRawGalleryTree.bl_label == "Photo Gallery"
    menu_draws = []
    menu_layouts = []
    def record_context_menu(self, context):
        menu_draws.append(tuple(p.uid for p in gallery._selection(context)))
        menu_layouts.append(str(self.layout.introspect()))
    gallery.BR_MT_gallery_photo.append(record_context_menu)
    window = bpy.context.window_manager.windows[0]
    window.event_simulate(type='ESC', value='PRESS')
    window.event_simulate(type='ESC', value='RELEASE')
    yield
    area = max(window.screen.areas, key=lambda a: a.width * a.height)
    area.ui_type = 'CompositorNodeTree'
    area.spaces.active.show_region_ui = True
    area.spaces.active.show_region_toolbar = True
    yield
    area.ui_type = gallery.GALLERY_TYPE
    region = next(r for r in area.regions if r.type == 'WINDOW')
    lib = extension.catalog.library(bpy.context)
    for i in range(137):
        photo = lib.photos.add()
        photo.name, photo.uid = f"Test {i}.NEF", str(i)
        photo.capture_date_indexed = True
    size = lib.bl_rna.properties['thumbnail_scale']
    assert [item.identifier for item in size.enum_items] == list('SML')
    assert size.default == lib.thumbnail_scale == 'M'

    def grid():
        with bpy.context.temp_override(window=window, area=area, region=region):
            shown = gallery._filtered(lib)
            layout = gallery._layout(bpy.context, shown)
            return layout, {i: (x, y) for i, _, x, y in gallery._visible_cards(shown, layout)}

    def card_point(index):
        layout, cards = grid()
        x, y = cards[index]
        return (int(region.x + x + layout.card_w / 2),
                int(region.y + y + layout.card_h / 2))

    def assert_visible(index):
        layout, cards = grid()
        assert index in cards, (index, cards.keys(), layout)
        x, y = cards[index]
        assert 0 <= x and x + layout.card_w <= layout.width
        assert 0 <= y - layout.caption and y + layout.card_h <= layout.height, (x, y, layout)

    # Inspect the actual dimensions passed to thumbnail rendering, separately from its caption.
    draw_thumbnail = gallery._thumbnail
    drawn_sizes = set()
    def record_thumbnail(photo, x, y, width, height):
        drawn_sizes.add((width, height))
        return draw_thumbnail(photo, x, y, width, height)
    gallery._thumbnail = record_thumbnail
    area.tag_redraw()
    yield
    dims, cards = grid()
    assert dims.columns >= 3 and dims.maximum > 0 and dims.card_w == dims.card_h
    assert drawn_sizes and all(w == h for w, h in drawn_sizes), drawn_sizes
    for i in range(dims.columns):
        assert cards[i][1] == cards[0][1]
    assert cards[dims.columns] == (cards[0][0], cards[0][1] - dims.row_step)
    points = [card_point(i) for i in range(3)]

    # The native status bar shows the batch even if the Gallery header is too narrow.
    draw_header = gallery._gallery_header
    progress_state = extension.jobs.export_progress
    activity_state = extension.jobs.export_activity
    header_layouts = []
    def record_header(header, context):
        draw_header(header, context)
        header_layouts.append(str(header.layout.introspect()))
    gallery._gallery_header = record_header
    status_layouts = []
    def record_status(header, context):
        status_layouts.append(str(header.layout.introspect()))
    bpy.types.STATUSBAR_HT_header.append(record_status)
    extension.jobs.export_progress = lambda: (2, 5)
    extension.jobs.export_activity = lambda: "Rendering Test 2.NEF"
    area.tag_redraw()
    gallery.redraw_statusbars()
    yield
    assert not area.spaces.active.show_region_ui and not area.spaces.active.show_region_toolbar
    assert header_layouts and "Export 2/5" not in header_layouts[-1], header_layouts
    assert status_layouts and "Photo Export 2/5" in status_layouts[-1], status_layouts
    assert "blender_raw.cancel_export" in status_layouts[-1]
    assert "Rendering Test 2.NEF" in status_layouts[-1]
    extension.jobs.export_progress = progress_state
    extension.jobs.export_activity = activity_state
    area.tag_redraw()
    gallery.redraw_statusbars()
    yield
    assert "Export 2/5" not in status_layouts[-1] and "blender_raw.cancel_export" not in status_layouts[-1]
    for action in ("import_photos", "gallery_filter", "export_photos"):
        assert "blender_raw." + action in header_layouts[-1], header_layouts[-1]
    assert "blender_raw.gallery_details" not in header_layouts[-1]
    header = header_layouts[-1]
    controls = ("blender_raw.import_photos", "blender_raw.export_photos", "blender_raw.gallery_filter",
                "Preview", "Size")
    assert [header.index(control) for control in controls] == sorted(header.index(control) for control in controls)
    assert header_layouts[-1].count("blender_raw.export_photos") == 1
    assert "BLENDERRAW_PT_gallery_export" not in header_layouts[-1]
    assert "blender_raw.cancel_export" not in header_layouts[-1]

    def event(kind, state, point, **mods):
        window.event_simulate(type=kind, value=state, x=point[0], y=point[1], **mods)

    def selected():
        return {i for i, photo in enumerate(lib.photos) if photo.selected}

    def key(kind, point):
        event(kind, 'PRESS', point)
        event(kind, 'RELEASE', point)

    def move_edge(horizontal, delta):
        if horizontal:
            edge = (area.x + area.width // 2, area.y)
        else:
            edge = (area.x + area.width, area.y + area.height // 2)
        event('MOUSEMOVE', 'NOTHING', edge)
        yield
        with bpy.context.temp_override(window=window, area=area):
            assert bpy.ops.screen.area_move(x=edge[0], y=edge[1], delta=delta) == {'FINISHED'}
        yield

    def drag(a, b, keyboard=False, cancel=False, **mods):
        event('MOUSEMOVE', 'NOTHING', points[a])
        yield
        if keyboard:
            event('B', 'PRESS', points[a])
            event('B', 'RELEASE', points[a])
            yield
        event('LEFTMOUSE', 'PRESS', points[a], **mods)
        yield
        event('MOUSEMOVE', 'NOTHING', points[b], **mods)
        yield
        event('ESC' if cancel else 'LEFTMOUSE', 'PRESS' if cancel else 'RELEASE', points[b], **mods)
        yield
        if cancel:
            event('ESC', 'RELEASE', points[b])
            event('LEFTMOUSE', 'RELEASE', points[b])
            yield
        assert not gallery._boxes

    yield from drag(0, 1)
    assert selected() == {0, 1}, selected()
    event('P', 'PRESS', points[0])
    event('P', 'RELEASE', points[0])
    yield
    assert {i for i, p in enumerate(lib.photos) if p.flagged} == {0, 1}
    yield from drag(1, 2, keyboard=True, shift=True)
    assert selected() == {0, 1, 2}, selected()
    # Target a member of the selection from the real Gallery context menu.
    for photo in lib.photos:
        photo.rating = 5
    event('MOUSEMOVE', 'NOTHING', points[1])
    key('RIGHTMOUSE', points[1])
    yield
    assert menu_draws and menu_draws[-1] == ('0', '1', '2'), menu_draws
    assert all("blender_raw." + action in menu_layouts[-1] for action in
               ('rate_photos', 'flag_photos', 'copy_adjustments', 'paste_adjustments', 'pack_selected', 'remove_photo', 'gallery_navigate')), menu_layouts[-1]
    key('HOME', points[1])
    key('RET', points[1])
    yield
    assert [p.rating for p in lib.photos[:4]] == [0, 0, 0, 5]
    # The same operators exposed by the sidebar handle mixed selections.
    with bpy.context.temp_override(window=window, area=area, region=region):
        bpy.ops.blender_raw.rate_photos(rating=4)
        bpy.ops.blender_raw.flag_photos(flagged=True)
    assert [p.rating for p in lib.photos[:4]] == [4, 4, 4, 5]
    assert {i for i, p in enumerate(lib.photos) if p.flagged} == {0, 1, 2}
    key('RIGHTMOUSE', points[1])
    yield
    key('ESC', points[1])
    yield
    with bpy.context.temp_override(window=window, area=area, region=region):
        bpy.ops.blender_raw.flag_photos(flagged=False)
    assert all(not p.flagged for p in lib.photos)
    lib.photos[0].flagged = lib.photos[1].flagged = True
    yield from drag(1, 2, ctrl=True)
    assert selected() == {0}, selected()
    yield from drag(0, 2, cancel=True)
    assert selected() == {0}, selected()
    event('U', 'PRESS', points[0])
    event('U', 'RELEASE', points[0])
    yield
    lib.flag_filter = 'FLAGGED'
    assert [i for i, _ in gallery._filtered(lib)] == [1]
    lib.flag_filter = 'UNFLAGGED'
    assert all(i != 1 for i, _ in gallery._filtered(lib))
    lib.flag_filter = 'ALL'
    key('RIGHTMOUSE', points[2])
    yield
    assert menu_draws[-1] == ('2',) and selected() == {2}
    assert all("blender_raw." + action in menu_layouts[-1] for action in
               ('edit_gallery_tags', 'relink_photo', 'remove_photo', 'pack_selected', 'gallery_navigate'))
    key('END', points[2])
    key('RET', points[2])
    yield
    assert not selected(), 'Select None from the context menu kept selected photos'
    key('RIGHTMOUSE', points[2])
    yield
    key('ESC', points[2])
    yield
    with bpy.context.temp_override(window=window, area=area, region=region):
        assert bpy.ops.blender_raw.edit_gallery_tags(uid='2', tags='Portrait, Portfolio') == {'FINISHED'}
    assert lib.photos[2].tags == 'Portrait, Portfolio' and not lib.photos[1].tags
    # Wheel events scroll rows. Thumbnail columns stay aligned after every scroll.
    event('WHEELDOWNMOUSE', 'PRESS', points[0])
    yield
    after, scrolled = grid()
    assert after.offset == dims.row_step
    assert scrolled[dims.columns] == cards[0]
    yield from drag(0, 1)
    assert selected() == {dims.columns, dims.columns + 1}, selected()
    points.append(card_point(2 * dims.columns + 1))
    yield from drag(0, 3)
    assert selected() == {dims.columns, dims.columns + 1, 2 * dims.columns, 2 * dims.columns + 1}
    for _ in range(len(lib.photos) // dims.columns + 2):
        event('WHEELDOWNMOUSE', 'PRESS', points[0])
    yield
    last = len(lib.photos) - 1
    current, scrolled = grid()
    assert current.offset == current.maximum and max(scrolled) == last
    assert_visible(last)
    assert scrolled[last][0] == cards[last % dims.columns][0]
    last_point = card_point(last)
    key('RIGHTMOUSE', last_point)
    yield
    assert menu_draws[-1] == (str(len(lib.photos)-1),)
    key('ESC', last_point)
    yield
    event('WHEELUPMOUSE', 'PRESS', points[0])
    yield
    assert grid()[0].offset == dims.maximum - dims.row_step
    lib.query = 'Test 9.'
    yield
    current, filtered = grid()
    assert current.offset == 0 and list(filtered) == [9]
    lib.query = ''
    yield

    # Mimic an active developed photo while browsing a different row. Set the anchor
    # and scroll to the top before resizing so only the resize should bring it back.
    lib.active_index = len(lib.photos) // 2
    target = lib.active_index
    for _ in range(len(lib.photos) // dims.columns + 2):
        event('WHEELUPMOUSE', 'PRESS', points[0])
    yield
    before, visible = grid()
    assert before.offset == 0 and target not in visible
    old_width, old_height = region.width, region.height
    yield from move_edge(horizontal=False, delta=-area.width // 3)
    narrower, narrow_cards = grid()
    assert region.width < old_width and narrower.columns < before.columns
    assert round(50 * narrower.scale) <= narrower.card_h == narrower.card_w <= round(300 * narrower.scale)
    assert_visible(target)
    leftmost = min(x for x, _ in narrow_cards.values())
    assert narrow_cards[target][0] == leftmost + (target % narrower.columns) * (narrower.card_w + narrower.pad)
    yield from move_edge(horizontal=True, delta=area.height // 3)
    shorter, _ = grid()
    assert region.height < old_height and (shorter.card_w, shorter.card_h) == (narrower.card_w, narrower.card_h)
    assert_visible(target)
    print('AREA REFLOW', (old_width, old_height), 'to', (region.width, region.height),
          'columns', before.columns, 'to', shorter.columns, 'active', target, flush=True)

    previous_size, previous_columns = 0, None
    for value, limit in (('S', 200), ('M', 300), ('L', 400)):
        lib.thumbnail_scale = value
        yield
        large, _ = grid()
        assert previous_size <= large.card_w == large.card_h
        assert previous_columns is None or large.columns <= previous_columns
        assert round(50 * large.scale) <= large.card_w <= round(limit * large.scale)
        assert (large.card_w + large.pad) * large.columns + large.pad <= large.width
        assert large.columns == 1 or ((large.columns - 1) * (round(limit * large.scale) + large.pad)
                                      + 2 * large.pad < large.width)
        assert_visible(target)
        previous_size, previous_columns = large.card_w, large.columns
    # Very short editors cannot fit a full thumbnail; keep the image center on screen.
    shrink = region.height - max(64, large.card_h // 2)
    yield from move_edge(horizontal=True, delta=shrink)
    shallow, shallow_cards = grid()
    assert shallow.height < shallow.card_h and shallow.card_h == large.card_h
    assert abs(shallow_cards[target][1] + shallow.card_h / 2 - shallow.height / 2) <= 1
    yield from move_edge(horizontal=True, delta=-shrink)
    lib.thumbnail_scale = size.default
    yield
    screenshot = os.environ.get("BLENDERRAW_GALLERY_SCREENSHOT")
    if screenshot:
        with bpy.context.temp_override(window=window, area=area, region=region):
            bpy.ops.screen.screenshot(filepath=screenshot)
    # Open the same popups as the header buttons and inspect their native layouts.
    lib.photos[0].selected = lib.photos[1].selected = True
    header_region = next(r for r in area.regions if r.type == 'HEADER')
    def popup(operator, helper_name, expected, excluded=()):
        layouts = []
        helper = getattr(gallery, helper_name)
        def record(layout, *args, **kwargs):
            helper(layout, *args, **kwargs)
            layouts.append(str(layout.introspect()))
        setattr(gallery, helper_name, record)
        with bpy.context.temp_override(window=window, area=area, region=header_region):
            operator('INVOKE_DEFAULT')
        yield
        assert layouts and all(token in layouts[-1] for token in expected), (expected, layouts)
        assert all(token not in layouts[-1] for token in excluded), layouts[-1]
        key('ESC', (region.x + region.width // 3, region.y + region.height // 2))
        yield
        setattr(gallery, helper_name, helper)

    def panel(panel_type, expected):
        layouts = []
        def record(self, context):
            layouts.append(str(self.layout.introspect()))
        panel_type.append(record)
        with bpy.context.temp_override(window=window, area=area, region=header_region):
            bpy.ops.wm.call_panel(name=panel_type.bl_idname)
        yield
        assert layouts and all(token in layouts[-1] for token in expected), (expected, layouts)
        key('ESC', (region.x + region.width // 2, region.y + region.height // 2))
        yield
        panel_type.remove(record)

    yield from popup(bpy.ops.blender_raw.gallery_filter, '_filter_ui', ('Rating', 'Capture Date', 'File Format'))
    with bpy.context.temp_override(window=window, area=area, region=header_region):
        bpy.ops.blender_raw.rate_photos(rating=2)
        bpy.ops.blender_raw.flag_photos(flagged=True)
    assert all(p.rating == 2 and p.flagged for p in lib.photos if p.selected)

    # Compact headers keep import, export and filter; only the smallest use a display popover.
    yield from move_edge(horizontal=False, delta=int(340 * grid()[0].scale) - area.width)
    assert all("blender_raw." + action in header_layouts[-1] for action in
               ("import_photos", "export_photos", "gallery_filter"))
    yield from move_edge(horizontal=False, delta=int(155 * grid()[0].scale) - area.width)
    yield from panel(gallery.BR_PT_gallery_display, ('Thumbnail Size', 'Preview'))
    assert header_layouts[-1].count("blender_raw.export_photos") == 1
    yield from move_edge(horizontal=False, delta=600)
    canvas_center = (region.x + region.width // 2, region.y + region.height // 2)
    sidebar_draws = []
    def record_sidebar(self, context):
        sidebar_draws.append(str(self.layout.introspect()))
    gallery.BR_PT_gallery_sidebar.append(record_sidebar)
    event('MOUSEMOVE', 'NOTHING', canvas_center)
    for shortcut, property_name in (('N', 'show_region_ui'), ('T', 'show_region_toolbar')):
        key(shortcut, canvas_center)
        yield
        assert getattr(area.spaces.active, property_name), (shortcut, property_name)
        area.tag_redraw()
        yield
        yield
        assert getattr(area.spaces.active, property_name), (shortcut, property_name)
    right_panel = next(r for r in area.regions if r.type == 'UI')
    assert right_panel.active_panel_category == gallery.GALLERY_NAME, right_panel.active_panel_category
    assert sidebar_draws, 'Photo Gallery sidebar did not draw'
    required = ('blender_raw.import_photos', 'blender_raw.export_photos', 'Thumbnail Size', 'Preview Resolution',
                'Pack Cache Images', 'blender_raw.gallery_navigate', 'blender_raw.rate_photos',
                'blender_raw.flag_photos', 'blender_raw.pack_selected', 'Filters', 'Export Settings')
    assert all(token in sidebar_draws[-1] for token in required), sidebar_draws[-1]
    gallery.BR_PT_gallery_sidebar.remove(record_sidebar)
    right_panel.active_panel_category = 'Tool'
    area.tag_redraw()
    yield
    yield
    assert right_panel.active_panel_category == 'Tool', 'Gallery overrode a manually chosen sidebar tab'
    lib.thumbnail_scale = 'S'
    yield
    layout, visible = grid()
    first_col = next(i for i in visible if i % layout.columns == 0 and
                     layout.card_h < visible[i][1] + layout.card_h <= layout.height)
    center = card_point(first_col)
    with bpy.context.temp_override(window=window, area=area, region=region):
        x, y = center[0] - region.x, center[1] - region.y
        assert [i for i, _ in gallery._hit_cards(bpy.context, (x, y, x, y))] == [first_col]
    for bar in area.regions:
        if bar.type in {'TOOLS', 'UI'} and bar.width > 2:
            assert not (bar.x <= center[0] < bar.x + bar.width and bar.y <= center[1] < bar.y + bar.height)
    event('MOUSEMOVE', 'NOTHING', center)
    yield
    key('N', center)
    yield
    yield
    assert area.spaces.active.show_region_toolbar and not area.spaces.active.show_region_ui, (
        center, area.spaces.active.show_region_toolbar, area.spaces.active.show_region_ui)
    area.ui_type = 'CompositorNodeTree'
    yield
    yield
    assert area.spaces.active.show_region_ui and area.spaces.active.show_region_toolbar
    area.ui_type = gallery.GALLERY_TYPE
    yield
    yield
    assert area.spaces.active.show_region_toolbar and not area.spaces.active.show_region_ui
    assert not gallery._boxes
    # X asks for confirmation and operates on the selection even when those
    # cards have never been developed or exported.
    nphotos = len(lib.photos)
    for i, p in enumerate(lib.photos):
        p.selected = i >= nphotos - 2
    event('MOUSEMOVE', 'NOTHING', canvas_center)
    key('X', canvas_center)
    yield
    key('ESC', canvas_center)
    yield
    assert len(lib.photos) == nphotos and len(selected()) == 2
    key('X', canvas_center)
    yield
    key('RET', canvas_center)
    yield
    assert len(lib.photos) == nphotos - 2 and not selected()
    # Saved user choices belong to the Gallery and survive opening the project again.
    with tempfile.TemporaryDirectory(prefix="BlenderRaw-gallery-") as temporary:
        filename = str(Path(temporary) / "sidebar.blend")
        assert bpy.ops.wm.save_as_mainfile(filepath=filename, copy=True) == {'FINISHED'}
        assert bpy.ops.wm.open_mainfile(filepath=filename, use_scripts=False) == {'FINISHED'}
        yield
        yield
        reopened = next(a for a in bpy.context.window_manager.windows[0].screen.areas if a.ui_type == gallery.GALLERY_TYPE)
        assert reopened.spaces.active.show_region_toolbar and not reopened.spaces.active.show_region_ui
        # Incremental GUI undo must keep the displaced private Developer groups
        # available while restoring both targets in the same undo step.
        def settings():
            photos = extension.catalog.library(bpy.context).photos[:3]
            return [next(n for n in p.tree.nodes if n.bl_idname == extension.compositor.NODE_TYPE) for p in photos]

        window = bpy.context.window_manager.windows[0]
        with bpy.context.temp_override(window=window, area=reopened):
            photos = extension.catalog.library(bpy.context).photos
            for i, photo in enumerate(photos):
                photo.selected = i in (1, 2)
            for photo in photos[:3]:
                extension.compositor.ensure_tree(bpy.context, photo)
            for i, developer in enumerate(settings()):
                developer.exposure = i + .5
            assert bpy.ops.blender_raw.copy_adjustments(uid=photos[0].uid) == {'FINISHED'}
            bpy.ops.ed.undo_push(message="Before Gallery batch")
            assert bpy.ops.blender_raw.paste_adjustments('EXEC_DEFAULT', True) == {'FINISHED'}
        yield
        assert [n.exposure for n in settings()] == [.5, .5, .5]
        with bpy.context.temp_override(window=window, area=reopened):
            assert bpy.ops.ed.undo() == {'FINISHED'}
        yield
        assert [n.exposure for n in settings()] == [.5, 1.5, 2.5]
        with bpy.context.temp_override(window=window, area=reopened):
            assert bpy.ops.ed.redo() == {'FINISHED'}
        yield
        assert [n.exposure for n in settings()] == [.5, .5, .5]
        reopened.ui_type = 'CompositorNodeTree'
        yield
    gallery._gallery_header = draw_header
    bpy.types.STATUSBAR_HT_header.remove(record_status)
    gallery.BR_MT_gallery_photo.remove(record_context_menu)
    gallery._thumbnail = draw_thumbnail
    if not installed:
        extension.unregister()
        assert gallery._header_override is None


steps = run()


def advance():
    try:
        next(steps)
        return .16
    except StopIteration:
        print("Gallery gesture regression passed", flush=True)
    except Exception:
        traceback.print_exc()
        sys.stderr.flush()
        os._exit(1)
    bpy.ops.wm.quit_blender()


bpy.app.timers.register(advance, first_interval=1.0, persistent=True)
