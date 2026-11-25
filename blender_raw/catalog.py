# SPDX-License-Identifier: GPL-3.0-or-later
"""Project-owned photo records, embedded originals and reproducible image caches."""
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
import uuid

import bpy
import bpy.utils.previews
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty, StringProperty
from . import camera_profiles, filters


_previews = None
CACHE_VERSION = "v1"
THUMBNAIL_DEFAULTS = {"S": 200, "M": 300, "L": 400}
_recent_images = []
_CACHE_KEY = "photo_editor_decode_cache"

# Saved 0.8 projects use enum values 0 and 1, including unset properties.
PROFILE_MODES = [
    ("AUTO", "Auto", "Select the camera profile and apply its color and tone calibration", 2),
    ("EMBEDDED", "Camera Calibration", "Use the embedded calibration or the decoder's camera matrix", 0),
    ("CUSTOM", "Custom DCP", "Use a compatible external camera profile", 1),
]


# Zero is reserved for records saved before Camera Input and Look were separate.
CAMERA_INPUTS = (
    ("AUTO", "LibRaw", "Use the embedded camera calibration or LibRaw camera matrix", 1),
    ("DCP", "Custom DCP", "Use a DNG camera profile for this camera", 2),
    ("ICC", "Custom ICC", "Use an RGB camera input ICC or ICM profile", 3),
)
APPLE_INPUT = ("APPLE", "Apple RAW (Core Image)", "Develop the original using the macOS camera calibration and crop", 4)
_OLD_APPLE_AUTO_NUMBER = 5
MAC_INPUTS = (CAMERA_INPUTS[0], APPLE_INPUT, *CAMERA_INPUTS[1:])
_APPLE_RAW_SUPPORTED = sys.platform == 'darwin' and platform.machine() == 'arm64'
DEFAULT_CAMERA_INPUT = 'AUTO'
DEFAULT_INPUT_NUMBER = 1
_LEGACY = (("LEGACY", "Previous Version", "Use the camera settings saved by an earlier version", 0),)
APPLE_WB = (("CAMERA", "As Shot", "Use the white balance recorded by the camera"),
            ("DAYLIGHT", "Daylight", "Use D65 daylight at 6504 K with zero tint"),
            ("CUSTOM", "Custom", "Set Core Image white balance in kelvins and tint units"),
            ("AUTO", "Auto", "Balance the mean color of well-exposed RAW pixels"),
            ("SAMPLE", "Neutral Sample", "Use the neutral point picked from the original"))
LOOKS = (
    ("NONE", "None", "Keep the camera input without an additional look", 1),
    ("LUT", "Custom 3D LUT", "Apply a .cube color lookup table after camera calibration", 3),
)
DCP_LOOKS = LOOKS + (("PROFILE", "From DCP", "Use the DCP's own look table and tone curve", 2),)
_BASELOOK = (("BASELOOK", "BaseLook (Previous Version)", "Use the look saved by version 0.10", 0),)
LUT_SPACES = (
    ("SRGB", "sRGB", "The table expects and returns sRGB-encoded Rec.709 colors"),
    ("LINEAR", "Linear Rec.709", "The table expects and returns scene-linear Rec.709 colors"),
)


def get_camera_input(recipe):
    # Old projects have no input stored. Blender's native reset uses the modern default.
    value = recipe.get("photo_editor_camera_input", 0)
    return APPLE_INPUT[-1] if value == _OLD_APPLE_AUTO_NUMBER else value


def set_camera_input(recipe, value):
    if value and not get_camera_input(recipe) and "photo_editor_look" not in recipe:
        # An older, unsplit recipe choosing a modern input starts with no look.
        set_look(recipe, 1)
    recipe["photo_editor_camera_input"] = APPLE_INPUT[-1] if value == _OLD_APPLE_AUTO_NUMBER else value


def camera_input_items(recipe, context):
    selected = get_camera_input(recipe)
    choices = MAC_INPUTS if _APPLE_RAW_SUPPORTED or selected == APPLE_INPUT[-1] else CAMERA_INPUTS
    return choices if selected else choices + _LEGACY


def look_items(recipe, context):
    choices = DCP_LOOKS if recipe.camera_input == "DCP" else LOOKS
    return choices + _BASELOOK if get_look(recipe) == 0 else choices


def uses_apple_input(recipe):
    return recipe.camera_input == 'APPLE'


def reader_camera_input(photo):
    saved = photo.get('reader_camera_input')
    if saved is None:
        return 'APPLE' if photo.get('apple_raw_reader') else 'LEGACY'
    return 'APPLE' if saved == 'APPLE_AUTO' else saved


def reader_needs_libraw(photo):
    return not _APPLE_RAW_SUPPORTED and reader_camera_input(photo) == 'APPLE'


@persistent
def migrate_apple_inputs(*_args):
    """Persist the single Core Image choice on editable photos and developer nodes."""
    from . import compositor
    for scene in bpy.data.scenes:
        if scene.library:
            continue
        for photo in scene.blender_raw.photos:
            if photo.get('photo_editor_camera_input') == _OLD_APPLE_AUTO_NUMBER:
                set_camera_input(photo, APPLE_INPUT[-1])
            if photo.get('reader_camera_input') == 'APPLE_AUTO' or (
                    'reader_camera_input' not in photo and photo.get('apple_raw_reader')):
                photo['reader_camera_input'] = 'APPLE'
    for tree in bpy.data.node_groups:
        if tree.library or tree.bl_idname != 'CompositorNodeTree':
            continue
        for node in tree.nodes:
            if node.bl_idname == compositor.DEVELOPER_TYPE and node.get('photo_editor_camera_input') == _OLD_APPLE_AUTO_NUMBER:
                node.camera_input = 'APPLE'


def get_look(recipe):
    # Version 0.10 saved BaseLook as zero or left it unset. New recipes store None explicitly.
    return recipe.get("photo_editor_look", 0)


def set_look(recipe, value):
    recipe["photo_editor_look"] = value


def has_custom_input(recipe):
    return recipe.camera_input in {"DCP", "ICC"} or (
        recipe.camera_input == "LEGACY" and recipe.profile_mode == "CUSTOM")


def cache_dir():
    folder = Path(tempfile.gettempdir()) / "Blender Photo Editor" / CACHE_VERSION
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _cache_file(name):
    path = cache_dir() / name
    if not path.exists():
        legacy = Path(tempfile.gettempdir()) / "BlenderRaw" / CACHE_VERSION / name
        if legacy.is_file():
            return legacy
    return path


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def resolved(path):
    return Path(bpy.path.abspath(path)).resolve()


def stored_path(path):
    path = str(Path(path).resolve())
    if bpy.data.filepath:
        try:
            return bpy.path.relpath(path)
        except ValueError:
            pass  # Windows drive letters can differ.
    return path


def original_filename(photo):
    # A Blender path such as //photos/image.NEF is relative to the .blend.
    # Windows pathlib would otherwise interpret its first two components as a UNC root.
    return (photo.filepath or photo.name).replace("\\", "/").rsplit("/", 1)[-1]


def _set_changed(photo, context):
    if photo.uid and (photo.scene or photo.image) and not bpy.app.background:
        from . import jobs
        jobs.preview_later(photo)


def _photo_input_changed(photo, context):
    if photo.camera_input != "DCP" and get_look(photo) == 2:
        photo.look = "NONE"
    _set_changed(photo, context)


def _photo_legacy_changed(photo, context):
    set_camera_input(photo, 0)
    _set_changed(photo, context)


def _profile_changed(photo, context):
    photo["profile_absolute"] = str(resolved(photo.profile_path)) if photo.profile_path else ""
    _set_changed(photo, context)


def remember_lut_path(recipe):
    # Blender can remap relative paths during Save As. Explicitly relinking via
    # the property releases an embedded LUT; automatic path remapping keeps it.
    recipe.lut_blob = None
    recipe["lut_absolute"] = str(resolved(recipe.lut_path)) if recipe.lut_path else ""


def _photo_lut_changed(photo, context):
    remember_lut_path(photo)
    _set_changed(photo, context)


def _gallery_setting_changed(lib, context):
    if context and context.window_manager and not bpy.app.background:
        from . import jobs
        jobs.redraw()


def _preview_resolution_changed(lib, context):
    full = lib.preview_resolution == "100"
    if lib.full_preview != full:
        lib.full_preview = full
    if not bpy.app.background and 0 <= lib.active_index < len(lib.photos):
        photo = lib.photos[lib.active_index]
        if photo.scene or photo.image:
            from . import jobs
            jobs.preview_later(photo, immediate=True)


def _legacy_resolution_changed(lib, context):
    if lib.full_preview != (lib.preview_resolution == "100"):
        lib.preview_resolution = "100" if lib.full_preview else "PROXY"


def _cache_images():
    return (image for image in bpy.data.images if not image.library and image.get(_CACHE_KEY))


def has_packed_cache_images():
    return any(image.packed_file for image in _cache_images())


def identify_legacy_caches():
    """Recognize older images by their actual Reader/Developer binding and decode key."""
    from . import compositor

    def identify(image):
        if image and not image.library and not image.get(_CACHE_KEY):
            path = Path(image.filepath)
            key = path.stem
            if path.suffix.lower() == ".exr" and len(key) == 64 and all(c in "0123456789abcdef" for c in key):
                image[_CACHE_KEY] = key

    for scene in bpy.data.scenes:
        if not scene.library:
            for photo in scene.blender_raw.photos:
                identify(photo.image)
    for tree in bpy.data.node_groups:
        if tree.library or tree.bl_idname != "CompositorNodeTree":
            continue
        for node in tree.nodes:
            if node.bl_idname == compositor.DEVELOPER_TYPE and node.node_tree:
                for name in ("Developed RAW", "As Shot RAW"):
                    source = node.node_tree.nodes.get(name)
                    identify(getattr(source, "image", None))


def _matches_payload(path, data):
    if not path.is_file() or path.stat().st_size != len(data):
        return False
    offset = 0
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            if block != data[offset:offset + len(block)]:
                return False
            offset += len(block)
    return True


def _externalize_image(image):
    # The serialized image path may point to another machine or an unwritable folder.
    # Only publish into our local cache. Retain the packed copy until its disk copy exists.
    key = image.get(_CACHE_KEY, "")
    if not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
        raise ValueError("Invalid cache key")
    path = _cache_file(key + ".exr")
    data = memoryview(image.packed_file.data)
    if not _matches_payload(path, data):
        if path.exists():
            restored = Path(tempfile.mkdtemp(prefix="restored-", dir=cache_dir()))
            path = restored / (key + ".exr")
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
        try:
            temporary.write_bytes(data)
            try:
                # The Windows release uses no-replace rename for atomic publication.
                if os.name == "nt":
                    os.rename(temporary, path)
                else:
                    os.link(temporary, path)
            except FileExistsError:
                if not _matches_payload(path, data):
                    raise OSError("A conflicting cache file appeared while moving the image")
        finally:
            temporary.unlink(missing_ok=True)
    image.unpack(method="REMOVE")
    image.filepath_raw = str(path)


def externalize_caches(lib):
    failures = []
    moved = 0
    for image in _cache_images():
        if image.packed_file:
            try:
                _externalize_image(image)
                moved += 1
            except (RuntimeError, ValueError, OSError) as exc:
                failures.append(str(exc))
    lib.cache_storage_error = f"{len(failures)} cache(s) kept packed: {failures[0]}" if failures else ""
    return moved


def _get_pack_caches(lib):
    return bool(lib.get("pack_cache_images", False) or bpy.data.use_autopack)


def _set_pack_caches(lib, enabled):
    lib.owner = True
    lib["pack_cache_images"] = enabled
    lib.cache_storage_error = ""
    if not enabled:
        bpy.data.use_autopack = False
        externalize_caches(lib)


@persistent
def _save_caches(_filepath):
    if not bpy.context.scene:
        return
    lib = library(bpy.context)
    if not lib.get("pack_cache_images") or bpy.data.use_autopack:
        return
    failures = []
    for image in _cache_images():
        if image.users and not image.packed_file:
            try:
                if not resolved(image.filepath).is_file():
                    raise FileNotFoundError("Decoded image is missing from disk")
                image.pack()
                if not image.packed_file:
                    raise OSError("Blender could not pack the decoded image")
            except (OSError, RuntimeError) as exc:
                failures.append(str(exc))
    lib.cache_storage_error = f"{len(failures)} cache(s) could not be packed: {failures[0]}" if failures else ""


@persistent
def migrate_preview_settings(*_args):
    for scene in bpy.data.scenes:
        if scene.library:
            continue
        lib = scene.blender_raw
        # Blender stores RNA enums as integers. Value 2 was the 75% preview.
        if lib.get("preview_resolution") == 2:
            lib.preview_resolution = "50"
        elif lib.photos and not lib.is_property_set("preview_resolution"):
            previous = None
            if lib.is_property_set("full_preview"):
                previous = lib.full_preview
            elif 0 <= lib.active_index < len(lib.photos):
                previous = lib.photos[lib.active_index].get("full_preview")
            if previous is not None:
                lib.preview_resolution = "100" if previous else "PROXY"
        for photo in lib.photos:
            if Path(original_filename(photo)).suffix.lower() == ".arw" and not photo.get("arw_camera_frame"):
                if photo.white_balance == "SAMPLE":
                    photo["legacy_sample_frame"] = True
                if photo.thumbnail_path:
                    # The camera thumbnail remains available while the corrected edit develops.
                    for field in ("edited_thumbnail_path", "edited_thumbnail_bytes"):
                        if field in photo:
                            del photo[field]
                photo["arw_camera_frame"] = True


class BR_Photo(bpy.types.PropertyGroup):
    uid: StringProperty()
    process_version: IntProperty(name="Processing Version", default=1)
    filepath: StringProperty(name="Original")
    source_absolute: StringProperty(options={"HIDDEN"})
    packed: BoolProperty(name="Embedded", default=False)
    source_blob: PointerProperty(type=bpy.types.Text)
    profile_blob: PointerProperty(type=bpy.types.Text)
    lut_blob: PointerProperty(type=bpy.types.Text)
    scene: PointerProperty(type=bpy.types.Scene)
    image: PointerProperty(type=bpy.types.Image)
    selected: BoolProperty(name="Select for Export")
    rating: IntProperty(name="Rating", min=0, max=5)
    flagged: BoolProperty(name="Flagged", description="Mark this photo as a pick")
    tags: StringProperty(name="Tags", description="Separate tags with commas")
    capture_date: StringProperty(name="Capture Date")
    capture_date_indexed: BoolProperty(options={"HIDDEN"})
    thumbnail_path: StringProperty()
    status: StringProperty(default="Imported", options={"SKIP_SAVE"})
    error: StringProperty(options={"SKIP_SAVE"})
    decode_key: StringProperty(options={"SKIP_SAVE"})
    white_balance: EnumProperty(name="White Balance", items=[
        ("CAMERA", "As Shot", "Use the white balance recorded by the camera"),
        ("AUTO", "Auto", "Estimate a white balance for this photo"),
        ("CUSTOM", "Custom", "Set the color temperature and tint"),
        ("SAMPLE", "Neutral Sample", "Balance a neutral patch picked from the original"),
    ], update=_set_changed)
    apple_white_balance: EnumProperty(name="White Balance", items=APPLE_WB, default="CAMERA", update=_set_changed)
    apple_lens_correction: BoolProperty(name="Vendor Lens Correction", description="Apply Apple's correction when the camera and lens support it", update=_set_changed)
    temperature: FloatProperty(name="Temperature", description="White balance in kelvins", min=2000, max=50000, soft_max=25000, default=5500, precision=0, update=_set_changed)
    tint: FloatProperty(name="Tint", min=-150, max=150, soft_min=-100, soft_max=100, default=0, precision=1, update=_set_changed)
    neutral_x: FloatProperty(name="Sample X", min=0, max=1, default=.5)
    neutral_y: FloatProperty(name="Sample Y", min=0, max=1, default=.5)
    camera_input: EnumProperty(name="Camera Input", items=camera_input_items, default=DEFAULT_INPUT_NUMBER, get=get_camera_input,
                               set=set_camera_input, update=_photo_input_changed)
    look: EnumProperty(name="Look", items=look_items, default=1, get=get_look, set=set_look, update=_set_changed)
    lut_path: StringProperty(name="LUT File", description="A 3D color lookup table in .cube format",
                             subtype="FILE_PATH", options={"PATH_SUPPORTS_BLEND_RELATIVE"}, update=_photo_lut_changed)
    lut_space: EnumProperty(name="LUT Space", items=LUT_SPACES, default="SRGB", update=_set_changed)
    profile_mode: EnumProperty(name="Camera Profile", items=PROFILE_MODES, default="EMBEDDED", update=_photo_legacy_changed)
    profile_path: StringProperty(name="Camera Profile File", subtype="FILE_PATH", update=_profile_changed)
    use_dcp_look: BoolProperty(name="DCP Color Tables", description="Apply color correction and look tables supplied by the profile", update=_set_changed)
    use_dcp_curve: BoolProperty(name="DCP Tone Curve", description="Apply the tone curve supplied by the profile", update=_set_changed)


class BR_Library(bpy.types.PropertyGroup):
    owner: BoolProperty()
    photos: CollectionProperty(type=BR_Photo)
    active_index: IntProperty(default=-1)
    pack_cache_images: BoolProperty(
        name="Pack Cache Images", get=_get_pack_caches, set=_set_pack_caches,
        description="Include decoded photo caches when saving. Switching off moves packed caches to disk and disables Blender's automatic resource packing")
    cache_storage_error: StringProperty(options={"SKIP_SAVE"})
    thumbnail_scale: EnumProperty(name="Thumbnail Size", description="Maximum thumbnail width in UI pixels",
        items=[("S", "S", "Use the small thumbnail size"), ("M", "M", "Use the medium thumbnail size"),
               ("L", "L", "Use the large thumbnail size")],
        default="M", update=_gallery_setting_changed)
    format_filter: EnumProperty(name="File Format", items=filters.FORMAT_ITEMS, default="ALL", update=_gallery_setting_changed)
    date_filter: EnumProperty(name="Capture Date", items=[
        ("ALL", "All Dates", "Show all capture dates"),
        ("ON", "On Date", "Show photos taken on a specific date"),
        ("RANGE", "Date Range", "Include both endpoints; leave either blank for an open range"),
        ("UNKNOWN", "Unknown Date", "Show photos without a recorded capture date"),
    ], update=_gallery_setting_changed)
    date_start: StringProperty(name="From", description="Camera-local date (YYYY-MM-DD)", search=filters.date_choices, update=_gallery_setting_changed)
    date_end: StringProperty(name="Through", description="Camera-local date (YYYY-MM-DD)", search=filters.date_choices, update=_gallery_setting_changed)
    rename_exports: BoolProperty(name="Batch Rename", description="Name exported files Project_Original_001, numbered in Gallery order")
    overwrite_exports: BoolProperty(name="Overwrite Existing Files", default=False, description="Replace matching files after export succeeds. When disabled, keep both files using a numbered suffix")
    query: StringProperty(name="Search", description="Filter by filename or tag")
    min_rating: IntProperty(name="Minimum Rating", min=0, max=5)
    flag_filter: EnumProperty(name="Flags", items=[
        ("ALL", "All Photos", "Show all photos"),
        ("FLAGGED", "Flagged", "Show flagged photos"),
        ("UNFLAGGED", "Unflagged", "Show photos without a flag"),
    ])
    preview_resolution: EnumProperty(name="Preview Resolution", items=[
        ("PROXY", "Proxy", "Quick editing preview, up to 1600 pixels on the long edge", 0),
        ("50", "Half", "Preview at half the original width and height", 1),
        ("100", "Full", "Preview at the full developed resolution", 3),
    ], default="PROXY", update=_preview_resolution_changed)
    # Keep the 0.2 property readable for saved files and scripts.
    full_preview: BoolProperty(options={"HIDDEN"}, update=_legacy_resolution_changed)


def library(context):
    for scene in bpy.data.scenes:
        if scene.blender_raw.owner and not scene.library:
            return scene.blender_raw
    return context.scene.blender_raw


def lookup_photo(scene, uid):
    if not uid:
        return None
    for candidate in (scene, *bpy.data.scenes):
        if candidate and hasattr(candidate, "blender_raw"):
            lib = candidate.blender_raw
            if 0 <= lib.active_index < len(lib.photos):
                active = lib.photos[lib.active_index]
                if active.uid == uid:
                    return active
            for photo in lib.photos:
                if photo.uid == uid:
                    return photo
    return None


def photo_path_index(photos):
    """Index the first matching entry, just as a single-file import does."""
    by_path = {}
    for index, photo in enumerate(photos):
        try:
            path = linked_file(photo)
        except (OSError, RuntimeError, ValueError):
            # A broken link in a saved catalog cannot identify an incoming file.
            continue
        by_path.setdefault(path, index)
    return by_path


def add_photo(context, path, embed=False, *, path_index=None):
    path = Path(path).resolve()
    lib = library(context)
    lib.owner = True
    if path_index is None:
        photo = next((p for p in lib.photos if linked_file(p) == path), None)
    else:
        index = path_index.get(path)
        photo = lib.photos[index] if index is not None else None
    if photo is not None:
        if embed:
            pack_photo(photo)
        return photo, False
    if not path.is_file():
        raise FileNotFoundError(path)
    photo = lib.photos.add()
    photo.camera_input = DEFAULT_CAMERA_INPUT
    photo['reader_camera_input'] = DEFAULT_CAMERA_INPUT
    photo.uid = uuid.uuid4().hex
    photo.process_version = 1
    photo.name = path.name
    photo.filepath = stored_path(path)
    if path.suffix.lower() == ".arw":
        photo["arw_camera_frame"] = True
    photo.source_absolute = str(path)
    try:
        if embed:
            pack_photo(photo)
        filters.index_imported_photo(photo, len(lib.photos) - 1, path)
    except Exception:
        lib.photos.remove(len(lib.photos) - 1)
        raise
    if lib.active_index < 0:
        lib.active_index = len(lib.photos) - 1
    if path_index is not None:
        path_index[path] = len(lib.photos) - 1
    return photo, True


def _pack(path):
    data = path.read_bytes()
    fingerprint = hashlib.sha256(data).hexdigest()
    # Blender can truncate long ID names; identify embedded files by the full digest.
    name = ".Blender Photo Editor." + fingerprint[:24]
    blob = next((text for text in bpy.data.texts if text.get("sha256") == fingerprint), None)
    if blob is None or blob.get("sha256") != fingerprint:
        blob = bpy.data.texts.new(name)
        blob["sha256"] = fingerprint
        blob["payload"] = data
        blob["extension"] = path.suffix.lower()
    return blob


def _unpack(blob):
    digest = blob.get("sha256", "")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("Invalid embedded file fingerprint")
    ext = blob.get("extension", "")
    if not ext.startswith(".") or not ext.isascii() or not ext[1:].isalnum():
        ext = ".raw"
    path = _cache_file(digest + ext)
    if not path.exists():
        data = blob["payload"]
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("The embedded file failed its integrity check")
        temp = path.with_name(path.name + "." + uuid.uuid4().hex)
        temp.write_bytes(data)
        os.replace(temp, path)
    return path


def pack_photo(photo):
    from . import compositor
    source = photo.source_blob if photo.packed and photo.source_blob else _pack(linked_file(photo))
    recipes = list(compositor.developers_for(photo))
    if not recipes:
        recipes.append(photo)
    packed_profiles = [(recipe, _pack(profile_file(recipe)))
                       for recipe in recipes if has_custom_input(recipe) and recipe.profile_path]
    packed_luts = [(recipe, _pack(lut_file(recipe)))
                   for recipe in recipes if recipe.look == "LUT" and recipe.lut_path]
    for recipe, profile in packed_profiles:
        recipe.profile_blob = profile
        recipe["embedded_profile_path"] = recipe.profile_path
    for recipe, lut in packed_luts:
        recipe.lut_blob = lut
    photo.source_blob = source
    photo.packed = True


def linked_file(photo):
    path = resolved(photo.filepath)
    if not path.is_file() and photo.source_absolute and Path(photo.source_absolute).is_file():
        return Path(photo.source_absolute)
    return path


def source_file(photo):
    path = _unpack(photo.source_blob) if photo.packed and photo.source_blob else linked_file(photo)
    if not path.is_file():
        raise FileNotFoundError(f"Original missing: {photo.filepath}")
    return path


def _linked_asset(filename, fallback):
    path = resolved(filename)
    if not path.is_file() and fallback and Path(fallback).is_file():
        return Path(fallback)
    return path


def linked_profile(recipe):
    return _linked_asset(recipe.profile_path, recipe.get("profile_absolute", ""))


def profile_file(photo):
    if not photo.profile_path:
        raise ValueError("Choose a camera input profile file")
    if photo.profile_blob and photo.get("embedded_profile_path") == photo.profile_path:
        return _unpack(photo.profile_blob)
    path = linked_profile(photo)
    if not path.is_file():
        raise FileNotFoundError(f"Profile missing: {photo.profile_path}")
    return path


def linked_lut(recipe):
    return _linked_asset(recipe.lut_path, recipe.get("lut_absolute", ""))


def lut_file(recipe):
    if not recipe.lut_path:
        raise ValueError("Choose a .cube LUT file")
    if recipe.lut_blob:
        return _unpack(recipe.lut_blob)
    path = linked_lut(recipe)
    if not path.is_file():
        raise FileNotFoundError(f"LUT missing: {recipe.lut_path}")
    return path


def file_stamp(path):
    info = path.stat()
    return f"{path}:{info.st_size}:{info.st_mtime_ns}"


def request(photo, mode, recipe=None):
    source = source_file(photo)
    stamp = photo.source_blob["sha256"] if photo.packed and photo.source_blob else file_stamp(source)
    source_stamp = stamp
    settings = {}
    resolution = photo.id_data.blender_raw.preview_resolution if mode == "preview" else "100"
    if mode != "thumb":
        if recipe is None:
            # Readers saved before the explicit input field used either the 0.15
            # Apple flag or the original, unsplit RAW recipe.
            camera_input = reader_camera_input(photo)
        else:
            camera_input = recipe.camera_input
        apple = camera_input == 'APPLE'
        wb = recipe.white_balance if recipe is not None else 'CAMERA'
        if apple and recipe is not None:
            wb = recipe.apple_white_balance
        settings = dict(white_balance=wb,
                        temperature=recipe.temperature if wb == "CUSTOM" else 5500.0,
                        tint=recipe.tint if wb == "CUSTOM" else 0.0,
                        preview=(resolution == "PROXY"))
        if apple:
            settings['apple_lens_correction'] = bool(recipe and recipe.apple_lens_correction)
            # Apple ships the camera profiles and RAW decoder with the operating system.
            stamp += ':apple-raw-v3:' + platform.mac_ver()[0]
            from . import raw_backend
            if raw_backend.apple_zero_shadow_bias(source):
                stamp += ':fp2-linear-shadows-1'
        # Older, unsplit recipes and their Reader caches can exist only as packed EXRs.
        # Keep their original key and display an unavailable readout if that EXR predates WB metadata.
        if camera_input != 'LEGACY':
            stamp += ':wb-metadata-1'
        if camera_input in {'AUTO', 'DCP', 'ICC'}:
            # These camera-space developments now blend clipped highlights before resizing.
            stamp += ':highlight-blend-1'
        if resolution == "50":
            settings["preview_percent"] = 50
        if source.suffix.lower() == ".arw":
            # Old cached developments included the sensor's optical-black margins.
            stamp += ":active-crop-v1"
        if wb == "SAMPLE":
            settings.update(neutral_x=recipe.neutral_x, neutral_y=recipe.neutral_y)
            if recipe.get("legacy_sample_frame") and source.suffix.lower() == ".arw":
                settings["legacy_sample_frame"] = True
        if camera_input != "LEGACY":
            settings.update(camera_input=camera_input, look=recipe.look if recipe is not None else 'NONE')
            stamp += ":camera-input-1"
            if settings['look'] == "LUT":
                lut = lut_file(recipe)
                with lut.open("rb") as stream:
                    fingerprint = hashlib.file_digest(stream, "sha256").hexdigest()
                settings.update(lut_path=str(lut), lut_space=recipe.lut_space, lut_sha256=fingerprint)
        else:
            settings.update(use_dcp_look=bool(recipe and recipe.use_dcp_look),
                            use_dcp_curve=bool(recipe and recipe.use_dcp_curve))
            if recipe is not None and recipe.profile_mode == "AUTO":
                settings.update(profile_mode="AUTO", use_dcp_look=True, use_dcp_curve=True)
                stamp += ":auto-2:" + camera_profiles.catalog_fingerprint()
        if recipe is not None and has_custom_input(recipe):
            profile = profile_file(recipe)
            settings["profile_path"] = str(profile)
            stamp += file_stamp(profile)
    kind = "full" if mode == "preview" and resolution == "100" else mode
    key = _digest(json.dumps([CACHE_VERSION, photo.process_version, kind, stamp, settings], sort_keys=True))
    ext = ".jpg" if mode == "thumb" else ".exr"
    path = _cache_file(key + ext)
    if mode != "thumb":
        # A recovered cache may live in its own folder when the usual path
        # already contained different bytes. Follow that image across reloads.
        previous = _bound_decode(photo, recipe)
        if (previous and previous.packed_file and recipe is not None
                and not path.is_file() and not previous.get('photo_editor_wb_checked')):
            # A project can contain the only copy of an EXR created before WB metadata.
            # Reuse it only if the historical key proves settings, input and resolution match.
            suffix = stamp[len(source_stamp):].replace(':wb-metadata-1', '').replace(':apple-raw-v3:', ':apple-raw-v2:')
            previous_key = _digest(json.dumps([CACHE_VERSION, photo.process_version, kind, source_stamp + suffix, settings],
                                               sort_keys=True))
            if previous.get(_CACHE_KEY) == previous_key and Path(previous.filepath).stem == previous_key:
                key, path = previous_key, _cache_file(previous_key + ext)
        if previous and previous.get(_CACHE_KEY) == key:
            current = resolved(previous.filepath)
            if current.name == key + ext and current.is_file():
                path = current
    return dict(mode=mode, source=str(source), output=str(path), settings=settings, key=key)


def _bound_decode(photo, recipe):
    if recipe is None:
        return photo.image
    developed = recipe.node_tree.nodes.get("Developed RAW")
    return developed.image if developed else None


def packed_decode(photo, req, recipe=None):
    """Blender's Pack Resources can keep a decoded EXR after disk-cache cleanup."""
    image = _bound_decode(photo, recipe)
    if image and image.packed_file and Path(image.filepath).stem == req["key"]:
        return image
    return None


def ensure_cache_pixels(image):
    """Recover an Image that Blender already tried to read before its EXR was restored."""
    if image.has_data or all(image.size):
        return True
    if not image.packed_file and not Path(bpy.path.abspath(image.filepath)).is_file():
        return False
    # Blender remembers a failed file read on this ID, even after a worker
    # recreates the missing file. Keeping the ID preserves its graph bindings.
    image.reload()
    return all(image.size)


def _trim_cache_buffers():
    # A cancelled compositor job can still be sampling the previous photo.
    if bpy.app.is_job_running("COMPOSITE") or bpy.app.is_job_running("RENDER"):
        return .25
    while len(_recent_images) > 5:
        previous = bpy.data.images.get(_recent_images.pop(0))
        if previous and previous.get(_CACHE_KEY) and not previous.is_dirty:
            previous.buffers_free()
    return None


def _cached_image(req, packed=None):
    image = packed
    if image is None:
        # Blender compares path spellings. Windows TEMP may use an 8.3 name,
        # while a repeat request or reopened project resolves to the long path.
        output = Path(req["output"]).resolve()
        image = next((cached for cached in _cache_images()
                      if cached.get(_CACHE_KEY) == req["key"] and not cached.packed_file
                      and resolved(cached.filepath) == output), None)
        if image is None:
            image = bpy.data.images.load(str(output), check_existing=True)
    if image.get(_CACHE_KEY) != req["key"]:
        image[_CACHE_KEY] = req["key"]
    if image.colorspace_settings.name != "Linear Rec.709":
        image.colorspace_settings.name = "Linear Rec.709"
    if not ensure_cache_pixels(image):
        raise RuntimeError(f"Could not load cached preview: {image.filepath}")
    if 'photo_editor_wb_checked' not in image and Path(req['output']).is_file():
        from . import raw_backend
        profile, wb = raw_backend.decode_metadata(req['output'])
        if profile:
            image['photo_editor_profile'] = profile
        if wb:
            image['photo_editor_white_balance'] = wb
        image['photo_editor_wb_checked'] = True
    if image.name in _recent_images:
        _recent_images.remove(image.name)
    _recent_images.append(image.name)
    if len(_recent_images) > 5 and not bpy.app.timers.is_registered(_trim_cache_buffers):
        if _trim_cache_buffers() is not None and not bpy.app.background:
            bpy.app.timers.register(_trim_cache_buffers, first_interval=.25)
    return image


def apply_image(photo, req):
    from . import compositor
    image = _cached_image(req, packed_decode(photo, req))
    old = photo.image
    if old != image:
        photo.image = image
    photo.decode_key = req["key"]
    photo.status, photo.error = "Ready", ""
    compositor.refresh_photo(photo, preview=req["mode"] == "preview")
    if old and old != image and old.users == 0 and not old.library:
        bpy.data.images.remove(old)
    return image


def apply_developer_image(photo, node, req):
    from . import compositor
    image = _cached_image(req, packed_decode(photo, req, node))
    if "photo_editor_profile" not in image:
        from . import raw_backend
        if "camera_input" in req["settings"] and Path(req["output"]).is_file():
            image["photo_editor_profile"] = raw_backend.camera_input_name(req["output"])
        elif req["settings"].get("profile_mode") == "AUTO":
            image["photo_editor_profile"] = raw_backend.auto_profile_name(req["source"])
    compositor.apply_developer_image(node, photo, image)
    node.error = ""
    return image


def _full_request(photo, scratch, recipe=None):
    from . import worker
    req = request(photo, "full", recipe=recipe)
    if not Path(req["output"]).is_file() and not packed_decode(photo, req, recipe):
        if scratch is not None:
            req["output"] = str(Path(scratch) / (req["key"] + ".exr"))
            req["settings"]["temporary_output"] = True
        if not Path(req["output"]).is_file():
            worker.process_image(req)
    return req


def ensure_full(context, photo, scratch=None):
    return apply_image(photo, _full_request(photo, scratch))


def ensure_developer_full(context, photo, node, scratch=None):
    return apply_developer_image(photo, node, _full_request(photo, scratch, recipe=node))


def thumbnail_preview(photo):
    if _previews is None:
        return None
    candidates = ((photo.get("edited_thumbnail_path"), "edited_thumbnail_bytes"),
                  (photo.thumbnail_path, "thumbnail_bytes"))
    for filename, field in candidates:
        if not filename:
            continue
        if filename in _previews:
            return _previews[filename]
        path = Path(filename)
        try:
            if not path.is_file():
                payload = photo.get(field)
                if not payload:
                    continue
                path = cache_dir() / (hashlib.sha256(payload).hexdigest() + path.suffix)
                if not path.exists():
                    path.write_bytes(payload)
            if len(_previews) >= 256:
                del _previews[next(iter(_previews))]
            return _previews.load(filename, str(path), "IMAGE")
        except (OSError, RuntimeError):
            continue
    return None


def thumbnail_icon(photo):
    item = thumbnail_preview(photo)
    return item.icon_id if item else 0


def draw_camera_input(col, node):
    col.prop(node, "camera_input")
    if uses_apple_input(node):
        if _APPLE_RAW_SUPPORTED:
            col.prop(node, "apple_lens_correction")
        else:
            col.label(text="Apple RAW requires Apple Silicon", icon="INFO")
    if node.camera_input == "LEGACY":
        col.prop(node, "profile_mode", text="Saved Profile")
    else:
        col.prop(node, "look")
    if has_custom_input(node):
        col.prop(node, "profile_path", text="Profile File")
    if node.camera_input != "LEGACY" and node.look == "LUT":
        col.prop(node, "lut_path")
        col.prop(node, "lut_space")
    if node.camera_input == "LEGACY" and node.profile_mode != "AUTO":
        col.prop(node, "use_dcp_look")
        col.prop(node, "use_dcp_curve")


def applied_white_balance(node):
    if not node.node_tree or node.get('recipe_dirty'):
        return ()
    from . import compositor
    image = compositor._meter_image(node)
    return image.get('photo_editor_white_balance', ()) if image else ()


def draw_develop(layout, node, photo):
    from . import compositor
    col = layout.column()
    draw_camera_input(col, node)
    wb = 'apple_white_balance' if uses_apple_input(node) else 'white_balance'
    row = col.row(align=True)
    row.prop(node, wb, text="WB")
    compositor._pick_button(row, photo, node, '')
    if getattr(node, wb) == "CUSTOM":
        col.prop(node, "temperature", text='Temperature (K)')
        col.prop(node, "tint")
    else:
        fields = col.column()
        fields.enabled = False
        if applied_white_balance(node):
            fields.prop(node, 'applied_temperature', text='Temperature (K)')
            fields.prop(node, 'applied_tint', text='Tint')
        else:
            fields.label(text='Temperature (K)     —')
            fields.label(text='Tint     —')


class BR_OT_externalize_cache_images(bpy.types.Operator):
    bl_idname = "blender_raw.externalize_cache_images"
    bl_label = "Move Packed Caches to Disk"
    bl_description = "Keep decoded EXRs in the local cache and remove their packed copies on the next project save"

    @classmethod
    def poll(cls, context):
        return bool(context.scene and not library(context).pack_cache_images and has_packed_cache_images())

    def execute(self, context):
        lib = library(context)
        moved = externalize_caches(lib)
        if lib.cache_storage_error:
            self.report({"WARNING"}, lib.cache_storage_error)
        else:
            self.report({"INFO"}, f"Moved {moved} packed cache image(s) to disk")
        return {"FINISHED"} if moved else {"CANCELLED"}


def register():
    global _previews
    bpy.utils.register_class(BR_Photo)
    bpy.utils.register_class(BR_Library)
    bpy.utils.register_class(BR_OT_externalize_cache_images)
    bpy.types.Scene.blender_raw = PointerProperty(type=BR_Library)
    _previews = bpy.utils.previews.new()
    bpy.app.handlers.save_pre.append(_save_caches)
    bpy.app.handlers.blend_import_post.append(migrate_apple_inputs)
    bpy.app.handlers.blend_import_post.append(migrate_preview_settings)
    bpy.app.handlers.save_pre.append(migrate_apple_inputs)


def unregister():
    global _previews
    if _previews is not None:
        bpy.utils.previews.remove(_previews)
        _previews = None
    if bpy.app.timers.is_registered(_trim_cache_buffers):
        bpy.app.timers.unregister(_trim_cache_buffers)
    _recent_images.clear()
    bpy.app.handlers.save_pre.remove(_save_caches)
    bpy.app.handlers.blend_import_post.remove(migrate_apple_inputs)
    bpy.app.handlers.blend_import_post.remove(migrate_preview_settings)
    bpy.app.handlers.save_pre.remove(migrate_apple_inputs)
    bpy.utils.unregister_class(BR_OT_externalize_cache_images)
    del bpy.types.Scene.blender_raw
    bpy.utils.unregister_class(BR_Library)
    bpy.utils.unregister_class(BR_Photo)
