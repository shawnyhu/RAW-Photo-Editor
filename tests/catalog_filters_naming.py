"""Run in Blender with the installed extension: --python tests/catalog_filters_naming.py -- INPUT_DIR"""
from collections import Counter
from pathlib import Path
import sys
import tempfile

import bpy
from bl_ext.user_default.raw_photo_editor import catalog, filters, naming


root = Path(sys.argv[sys.argv.index("--") + 1]).resolve()
lib = catalog.library(bpy.context)
for path in sorted(root.iterdir()):
    if path.suffix.lower() in {".arw", ".nef", ".cr3", ".dng"}:
        catalog.add_photo(bpy.context, path)
assert len(lib.photos) == 20 and all(p.capture_date for p in lib.photos)
assert lib.thumbnail_scale == 'M' and not lib.rename_exports and not lib.overwrite_exports


def matches():
    return [p for _, p in filters.filtered(lib)]


for suffix, count in {"ARW": 4, "NEF": 6, "CR3": 3, "DNG": 7}.items():
    lib.format_filter = suffix
    assert len(matches()) == count
lib.format_filter = "NEF"
nef = next(p for p in lib.photos if p.name.endswith(".nef"))
absolute = nef.filepath
for relative in ("//" + nef.name, "//photos/" + nef.name):
    nef.filepath = relative
    assert len(matches()) == 6 and catalog.original_filename(nef) == nef.name
nef.filepath = absolute
lib.date_filter = "ON"
lib.date_start = "2026-04-19"
assert len(matches()) == 3
matches()[0].rating = 5
lib.min_rating = 5
assert len(matches()) == 1
lib.min_rating = 0
lib.date_filter = "RANGE"
lib.date_end = "2026-04-23"
assert len(matches()) == 5
lib.date_start = ""
assert len(matches()) == 5
lib.date_start = "2026-04-23"
lib.date_end = ""
assert len(matches()) == 3
lib.date_start = "2026-02-31"
assert not matches() and filters._date_error(lib)
lib.date_start, lib.date_end = "2026-04-23", "2026-04-19"
assert not matches() and filters._date_error(lib)
lib.date_filter = "UNKNOWN"
lib.format_filter = "ALL"
assert not matches()
p = lib.photos.add()
p.uid, p.name, p.filepath = "missing", "Missing.NEF", "//Missing.NEF"
assert [p.uid for p in matches()] == ["missing"]
lib.photos.remove(len(lib.photos)-1)
lib.date_filter = "ALL"
print("CAPTURE DATES", sorted(Counter(p.capture_date for p in lib.photos).items()), flush=True)

with tempfile.TemporaryDirectory(prefix="BlenderRaw-filenames-") as tmp:
    directory = Path(tmp)
    photos = list(lib.photos)[:3]

    def filenames():
        return [Path(item["path"]).name for item in naming.export_items(photos, directory, ".jpg", lib)]

    stems = [Path(p.name).stem for p in photos]
    assert filenames() == [stem + ".jpg" for stem in stems]
    lib.rename_exports = True
    assert not bpy.data.filepath
    assert filenames() == [f"Untitled_{stem}_{i:03d}.jpg" for i, stem in enumerate(stems, 1)]
    project = directory / "Gallery.v2.blend"
    assert bpy.ops.wm.save_as_mainfile(filepath=str(project)) == {"FINISHED"}
    expected = [f"Gallery.v2_{stem}_{i:03d}.jpg" for i, stem in enumerate(stems, 1)]
    assert filenames() == expected
    occupied = directory / expected[0]
    occupied.write_bytes(b"belongs to an earlier export")
    assert filenames() == [occupied.stem + "-2.jpg", *expected[1:]]
    lib.overwrite_exports = True
    assert filenames() == expected
    assert occupied.read_bytes() == b"belongs to an earlier export"
    lib.rename_exports = False
    # Two selected originals can legitimately share a filename. Each gets an output.
    duplicate = naming.export_items([photos[0], photos[0]], directory, ".jpg", lib)
    assert [Path(item["path"]).name for item in duplicate] == [stems[0] + ".jpg", stems[0] + "-2.jpg"]
    lib.rename_exports = True
    lib.thumbnail_scale, lib.format_filter, lib.date_filter = 'L', "ARW", "ON"
    lib.date_start = "2026-09-13"
    assert bpy.ops.wm.save_as_mainfile(filepath=str(project)) == {"FINISHED"}
    assert bpy.ops.wm.open_mainfile(filepath=str(project), load_ui=False) == {"FINISHED"}
    lib = catalog.library(bpy.context)
    assert len(matches()) == 4 and lib.thumbnail_scale == 'L'
    assert lib.rename_exports and lib.overwrite_exports
    photos = list(lib.photos)[:3]
    assert filenames() == expected
    print("FILTERS, PROJECT NAMING, OVERWRITE POLICY AND REOPEN PASSED", flush=True)
