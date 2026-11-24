"""Run in a Blender window with --enable-event-simulate and an isolated profile."""
import os
from pathlib import Path
import sys
import traceback

import bpy
import gpu

if "bl_ext.user_default.raw_photo_editor" in bpy.context.preferences.addons:
    from bl_ext.user_default import raw_photo_editor as addon
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import blender_raw as addon
    addon.register()


def test():
    window = bpy.context.window_manager.windows[0]
    bpy.context.preferences.view.show_splash = False
    window.event_simulate(type='ESC', value='PRESS')
    window.event_simulate(type='ESC', value='RELEASE')
    yield
    area = max(window.screen.areas, key=lambda a: a.width * a.height)
    area.ui_type = addon.gallery.GALLERY_TYPE
    yield
    region = next(r for r in area.regions if r.type == 'WINDOW')
    lib = addon.catalog.library(bpy.context)
    assert lib.thumbnail_scale == 'M'
    for i in range(201):
        p = lib.photos.add()
        p.uid, p.name, p.capture_date_indexed = str(i), f"Photo {i:03}.NEF", True
    lib.active_index = 0
    lib.photos[0].selected = True

    def grid():
        with bpy.context.temp_override(window=window, area=area, region=region):
            return addon.gallery._layout(bpy.context, addon.gallery._filtered(lib))

    def send(kind, value, x, y):
        return window.event_simulate(type=kind, value=value, x=int(region.x + x), y=int(region.y + y))

    # Observe the actual grip drawn for each editor, including its theme alpha.
    # Reading only the hover flag would miss a permanently expanded/opaque drawing.
    gallery = addon.gallery
    draw_scrollbar, draw_shape = gallery._draw_scrollbar, gallery._scroll_shape
    drawings = {}
    blend_modes = set()

    def record_shape(x, y, width, height, roundness, color):
        blend_modes.add(gpu.state.blend_get())
        drawings[bpy.context.area.as_pointer()].append((width, height, color[3]))
        return draw_shape(x, y, width, height, roundness, color)

    def record_scrollbar(layout, theme):
        drawings[bpy.context.area.as_pointer()] = []
        return draw_scrollbar(layout, theme)

    gallery._draw_scrollbar, gallery._scroll_shape = record_scrollbar, record_shape

    def grip(editor=area):
        shapes = drawings[editor.as_pointer()]
        return min(shapes, key=lambda shape: shape[1]) if shapes else (0, 0, 0)

    def row_layout():
        with bpy.context.temp_override(window=window, area=area, region=region):
            items = gallery._filtered(lib)
            layout = gallery._layout(bpy.context, items)
            return [(i, x, y) for i, _, x, y in gallery._visible_cards(items, layout)]

    def approach(x, y):
        send('MOUSEMOVE', 'NOTHING', x, y)
        area.tag_redraw()
        yield
        yield

    for scale in (1., 1.5):
        bpy.context.preferences.view.ui_scale = scale
        lib.thumbnail_scale = 'M'
        yield
        yield
        g = grid()
        assert g.maximum and g.scroll_width and g.offset == 0, g
        assert g.width == region.width and g.scroll_left < g.left + g.width <= region.width
        # Removing the scrollbar by filtering must not reflow a row.
        card_sizes = {}
        columns = []
        for mode, maximum in (('S', 200), ('M', 300), ('L', 400)):
            lib.thumbnail_scale = mode
            overlay = grid()
            assert overlay.maximum and overlay.scroll_width
            assert round(50 * g.scale) <= overlay.card_w <= round(maximum * g.scale)
            assert overlay.columns * overlay.card_w + (overlay.columns + 1) * overlay.pad <= overlay.width
            card_sizes[mode] = overlay.card_w
            columns.append(overlay.columns)
            lib.query = "Photo 199"
            fitted = grid()
            assert not fitted.scroll_width and fitted.columns == overlay.columns and fitted.card_w == overlay.card_w
            lib.query = ""
        assert card_sizes['S'] < card_sizes['L'] and columns == sorted(columns, reverse=True)
        lib.thumbnail_scale = 'M'
        g = grid()
        x, y = g.left + g.width // 2, g.height // 2
        # Moving towards the overlay progressively strengthens the grip.
        # The hover animation and the track occupy no additional grid width.
        bx = g.scroll_left + g.scroll_width // 2
        rows = row_layout()
        yield from approach(x, y)
        idle = grip()
        strengths = []
        for distance in (32, 24, 16, 8, 0):
            yield from approach(bx - round(distance * g.scale), y)
            strengths.append(grip()[2])
            assert row_layout() == rows
        full = grip()
        assert blend_modes == {'ALPHA'}, blend_modes
        assert 0 < idle[2] < full[2] and full[0] > idle[0], (idle, full)
        assert strengths == sorted(strengths) and any(idle[2] < a < full[2] for a in strengths), strengths
        assert abs(idle[2] / full[2] - .4) < .03, (idle, full)
        # GHOST supplies the final, device-scaled pixel delta on native trackpads.
        # Send that same window event through Blender's keymap in both directions.
        for dy in (13, 7, -9):
            before = grid().offset
            send('MOUSEMOVE', 'NOTHING', x, y)
            event = send('TRACKPADPAN', 'NOTHING', x, y + dy)
            assert event.mouse_y - event.mouse_prev_y == dy
            yield
            assert grid().offset == before + dy, (before, dy, grid())

        send('WHEELUPMOUSE', 'PRESS', x, y)
        yield
        assert grid().offset == 0
        bx = g.scroll_left + g.scroll_width // 2
        bottom, size = grid().thumb
        start = bottom + size // 2
        send('MOUSEMOVE', 'NOTHING', bx, start)
        yield
        assert grip()[2] == full[2]
        send('LEFTMOUSE', 'PRESS', bx, start)
        yield
        assert area.as_pointer() in addon.gallery._pressed_bars, (bx, start, g, region.width)
        # Finish outside the gallery in the adjacent editor. Dragging must
        # retain full visibility, then release capture and the hover state.
        other_area = next(a for a in window.screen.areas if a.x >= area.x + area.width - 1)
        outside_x = other_area.x + other_area.width // 2 - region.x
        send('MOUSEMOVE', 'NOTHING', outside_x, 0)
        yield
        assert grip()[2] == full[2]
        send('LEFTMOUSE', 'RELEASE', outside_x, 0)
        yield
        assert grid().offset == g.maximum, grid()
        assert area.as_pointer() not in addon.gallery._pressed_bars
        area.tag_redraw()
        yield
        assert grip() == idle, (grip(), idle)
        # A track click pages the view; neither track nor handle select cards.
        send('MOUSEMOVE', 'NOTHING', bx, g.height - 5)
        send('LEFTMOUSE', 'PRESS', bx, g.height - 5)
        send('LEFTMOUSE', 'RELEASE', bx, g.height - 5)
        yield
        assert grid().offset == g.maximum - round(.8 * g.height), grid()
        send('MIDDLEMOUSE', 'PRESS', bx, g.height // 2)
        send('MIDDLEMOUSE', 'RELEASE', bx, g.height // 2)
        yield
        assert abs(grid().offset - g.maximum / 2) <= g.maximum / g.height + 1
        assert [p.uid for p in lib.photos if p.selected] == ["0"] and lib.active_index == 0
        send('MOUSEMOVE', 'NOTHING', x, y)
        yield
        assert grip() == idle

        lib.thumbnail_scale = 'L'
        yield
        assert grid().card_h == card_sizes['L']
        lib.query = "Photo 199"
        yield
        assert grid().offset == 0 and grid().scroll_width == 0
        yield from approach(bx, y)
        assert not drawings[area.as_pointer()]
        lib.query = ""
        yield
        assert grid().offset == 0 and grid().scroll_width > 0
        # When the sidebar overlaps the canvas, the bar belongs to its visible edge.
        area.spaces.active.show_region_ui = True
        yield
        yield
        side = next(r for r in area.regions if r.type == 'UI')
        g = grid()
        assert region.x + g.scroll_left + g.scroll_width <= side.x + 1
        area.spaces.active.show_region_ui = False
        yield
        yield
        print(f"PASS wheel, pixel pan, drag, page, preserved selection and filtered grid at scale {g.scale:g}", flush=True)

    # Recreate the initial single-column gallery and exercise both adjoining
    # hit targets with window events: the floating grip and the native divider.
    lib.thumbnail_scale = 'M'
    g = grid()
    edge_x, edge_y = area.x + area.width, area.y + area.height // 2
    window.event_simulate(type='MOUSEMOVE', value='NOTHING', x=edge_x, y=edge_y)
    yield
    with bpy.context.temp_override(window=window, area=area):
        target = round(300 * g.scale) + 2 * g.pad
        assert bpy.ops.screen.area_move(x=edge_x, y=edge_y, delta=target - g.width) == {'FINISHED'}
    yield
    g = grid()
    before = area.width
    assert g.columns == 1 and abs(g.card_w - round(300 * g.scale)) <= round(5 * g.scale), g
    bx = g.scroll_left + g.scroll_width // 2
    bottom, size = g.thumb
    send('MOUSEMOVE', 'NOTHING', bx, bottom + size // 2)
    yield
    send('LEFTMOUSE', 'PRESS', bx, bottom + size // 2)
    yield
    send('MOUSEMOVE', 'NOTHING', bx, 0)
    yield
    send('LEFTMOUSE', 'RELEASE', bx, 0)
    yield
    assert grid().offset == g.maximum and area.width == before
    edge_x = area.x + area.width
    for kind, value, px in (('MOUSEMOVE', 'NOTHING', edge_x), ('LEFTMOUSE', 'PRESS', edge_x),
                            ('MOUSEMOVE', 'NOTHING', edge_x + 65), ('LEFTMOUSE', 'RELEASE', edge_x + 65)):
        window.event_simulate(type=kind, value=value, x=px, y=edge_y)
        yield
    widened = grid()
    assert area.width > before and widened.columns == 2 and widened.card_w < g.card_w, widened
    assert round(50 * g.scale) <= widened.card_w <= round(300 * g.scale)
    print(f"PASS narrow single-column gallery, native divider dragged {before} to {area.width}px", flush=True)
    # Two Blender windows can each host a Gallery. A hot scrollbar in the
    # first window must return to idle when the pointer is in the second.
    original_window_ids = {w.as_pointer() for w in bpy.context.window_manager.windows}
    with bpy.context.temp_override(window=window, area=area, region=region):
        assert bpy.ops.wm.window_new() == {'FINISHED'}
    yield
    yield
    second_window = next(w for w in bpy.context.window_manager.windows if w.as_pointer() not in original_window_ids)
    second_area = max(second_window.screen.areas, key=lambda a: a.width * a.height)
    second_area.ui_type = gallery.GALLERY_TYPE
    yield
    second_region = next(r for r in second_area.regions if r.type == 'WINDOW')
    near_x = widened.scroll_left + widened.scroll_width // 2
    yield from approach(near_x, widened.height // 2)
    assert grip()[2] == full[2]
    send('WINDOW_DEACTIVATE', 'NOTHING', near_x, widened.height // 2)
    yield
    assert grip()[2] == idle[2], (grip(), idle)
    yield from approach(near_x, widened.height // 2)
    assert grip()[2] == full[2]
    second_window.event_simulate(type='MOUSEMOVE', value='NOTHING', x=second_region.x + second_region.width // 2,
                                 y=second_region.y + second_region.height // 2)
    yield
    area.tag_redraw()
    yield
    assert grip()[2] == idle[2], (grip(), idle)
    with bpy.context.temp_override(window=second_window, area=second_area, region=second_region):
        other_grid = gallery._layout(bpy.context, gallery._filtered(addon.catalog.library(bpy.context)))
    second_window.event_simulate(type='MOUSEMOVE', value='NOTHING',
                                 x=second_region.x + other_grid.scroll_left + other_grid.scroll_width // 2,
                                 y=second_region.y + second_region.height // 2)
    yield
    assert grip(second_area)[2] == full[2]
    yield from approach(near_x, widened.height // 2)
    second_area.tag_redraw()
    yield
    assert grip()[2] == full[2] and grip(second_area)[2] == idle[2]
    assert blend_modes == {'ALPHA'}, blend_modes
    with bpy.context.temp_override(window=second_window, area=second_area):
        assert bpy.ops.wm.window_close() == {'FINISHED'}
    yield
    print("PASS independent scrollbar hover across Blender windows", flush=True)
    gallery._draw_scrollbar, gallery._scroll_shape = draw_scrollbar, draw_shape


steps = test()


def advance():
    try:
        next(steps)
        return .22
    except StopIteration:
        bpy.ops.wm.quit_blender()
    except Exception:
        traceback.print_exc()
        sys.stderr.flush()
        os._exit(1)


bpy.app.timers.register(advance, first_interval=1)
