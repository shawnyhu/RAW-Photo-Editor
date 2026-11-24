"""Check per-node preview status and stable sidebar controls using a real RAW."""
from pathlib import Path
import shutil
import sys
import tempfile
import time
from unittest.mock import Mock

import bpy
from bl_ext.user_default.raw_photo_editor import catalog, compositor, jobs


def sidebar_rows(node):
    layout = Mock()
    layout.column.return_value = layout
    layout.row.return_value = layout
    # Keep a row for each value while the disabled WB readouts await a decode.
    catalog.draw_develop(layout, node, compositor.developer_source(node))
    rows = []
    for method, args, kwargs in layout.mock_calls:
        if method == "prop":
            rows.append(kwargs.get("text", args[1]))
        elif method == "label":
            rows.append(kwargs["text"].split("     ")[0])
    return rows


def settle(photo, node, independent=None):
    phases = []
    deadline = time.monotonic() + 180
    while jobs.developing(photo.uid):
        phase, _ = compositor.developer_status(node)
        if not phases or phase != phases[-1]:
            phases.append(phase)
        if independent is not None:
            assert compositor.developer_status(independent)[0] == "READY"
        assert time.monotonic() < deadline, phases
        jobs.tick()
        time.sleep(.03)
    return phases


def ready(node):
    phase, text = compositor.developer_status(node)
    image = node.node_tree.nodes["Developed RAW"].image
    assert phase == "READY" and image["photo_editor_profile"] in text, (phase, text)
    return text


def run(source, folder):
    # A unique linked original prevents a previous run's decode cache from
    # skipping the background workers and their processing state.
    local = folder / source.name
    shutil.copyfile(source, local)
    photo, _ = catalog.add_photo(bpy.context, local)
    tree = compositor.ensure_tree(bpy.context, photo)
    reader = next(n for n in tree.nodes if n.bl_idname == compositor.READER_TYPE)
    first, = compositor.developers_for(photo)
    rows = sidebar_rows(first)
    jobs.preview_later(photo, immediate=True)
    phases = settle(photo, first)
    assert "QUEUED" in phases and "PROCESSING" in phases, phases
    first_ready = ready(first)
    assert sidebar_rows(first) == rows

    second = tree.nodes.new(compositor.NODE_TYPE)
    assert compositor.developer_status(second) is None
    link = tree.links.new(reader.outputs["Image"], second.inputs["Image"])
    compositor._sync_developer_sources()
    lut = folder / "test.cube"
    lut.write_text("invalid table", encoding="utf-8")
    second.lut_path = str(lut)
    second.look = "LUT"
    rows = sidebar_rows(second)
    # Background Blender has no UI timers, so drive the same scheduler used
    # by the live node's property update and keep the first node ready.
    jobs.developer_later(photo, second)
    phases = settle(photo, second, first)
    assert phases[:2] == ["QUEUED", "PROCESSING"], phases
    phase, text = compositor.developer_status(second)
    assert phase == "ERROR" and second.error and "cube" in text.lower(), text
    assert sidebar_rows(second) == rows

    shutil.copyfile(Path(__file__).parent / "fixtures" / "half-green.cube", lut)
    second.lut_path = str(lut)
    jobs.developer_later(photo, second)
    phases = settle(photo, second, first)
    assert phases[:2] == ["QUEUED", "PROCESSING"], phases
    assert not second.error and ready(second)
    assert sidebar_rows(second) == rows
    assert ready(first) == first_ready

    second.mute = True
    assert compositor.developer_status(second) is None
    second.mute = False
    tree.links.remove(link)
    assert compositor.developer_status(second) is None
    print("Developer status: independent workers, calibration, LUT error, recovery, mute, disconnect and stable sidebar",
          first_ready, flush=True)


source = Path(sys.argv[sys.argv.index("--") + 1]).resolve()
with tempfile.TemporaryDirectory(prefix="Photo Editor status ") as temp:
    run(source, Path(temp))
