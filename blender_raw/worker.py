# SPDX-License-Identifier: GPL-3.0-or-later
"""Entry point for isolated decoding and for Blender's background export process."""
import importlib.util
from importlib import import_module
import json
import os
from pathlib import Path
import sys
import tempfile
import traceback
import uuid


def process_image(req):
    if __package__:
        from . import raw_backend
    else:
        import raw_backend
    out = Path(req["output"])
    temp = out.with_name(f".{out.stem}.{uuid.uuid4().hex}{out.suffix}")
    try:
        if req["mode"] in {"thumb", "import_thumb"}:
            if raw_backend.thumbnail(req["source"], str(temp), import_preview=req["mode"] == "import_thumb") is False:
                raise ValueError("This file has no usable thumbnail")
        else:
            raw_backend.decode(req["source"], str(temp), req["settings"])
        if not temp.is_file() or temp.stat().st_size == 0:
            raise OSError("The decoder did not produce an image")
        os.replace(temp, out)
    finally:
        temp.unlink(missing_ok=True)


def _render_inputs(package, photo, scene):
    """Resolve full-resolution sources through all enabled native scene effects."""
    pipeline = import_module('.scene_pipeline', package.__name__)
    photos = {photo.uid: photo}
    developers = {}
    compositor = package.compositor
    for _effect, node, _parents in pipeline.upstream(scene, mode='RENDER'):
        if node.bl_idname == compositor.READER_TYPE:
            source = package.catalog.lookup_photo(scene, node.photo_uid)
            if not source:
                raise ValueError("A Raw Reader references a missing Photo Gallery original")
            photos[source.uid] = source
        elif node.bl_idname == compositor.DEVELOPER_TYPE:
            source = compositor.developer_source(node, mode='RENDER')
            if source and compositor.uses_camera_adjustments(node):
                developers[node.as_pointer()] = (source, node)
    return photos.values(), developers.values()


def _prune_import_thumbnails(directory):
    # Run in the background decoder, outside the UI thread. Only this versioned
    # import-preview directory contains files managed by this limit.
    try:
        with os.scandir(directory) as listing:
            previews = [entry for entry in listing if entry.name.endswith('.jpg')]
        if len(previews) > 1024:
            previews.sort(key=lambda entry: entry.stat().st_mtime_ns)
            for entry in previews[:-960]:
                Path(entry.path).unlink(missing_ok=True)
    except OSError:
        pass  # Another Blender instance can be reading or pruning.


def _report_event(log, event):
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")


def _load_snapshot(req):
    import bpy
    # Register the same extension before loading the snapshot so custom nodes
    # and property groups are available during Blender's file versioning.
    package = sys.modules.get(req["package"])
    if not hasattr(bpy.types.Scene, "blender_raw"):
        root = Path(__file__).resolve().parent
        spec = importlib.util.spec_from_file_location("blenderraw_worker", root / "__init__.py",
                                                     submodule_search_locations=[str(root)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = package
        spec.loader.exec_module(package)
        package.register()
    if package is None:
        raise RuntimeError("The RAW Photo Editor extension could not be loaded")
    bpy.ops.wm.open_mainfile(filepath=req["snapshot"], load_ui=False, use_scripts=False)
    return package


def film_preview(req):
    import bpy
    package = _load_snapshot(req)
    film_bake = import_module('.film_bake', package.__name__)
    tree = bpy.data.node_groups[req['tree']]
    film_bake.preview(bpy.data.scenes[req['scene']], tree, tree.nodes[req['node']], req['output'])


def export_batch(req):
    import bpy
    package = _load_snapshot(req)
    log = Path(req["report"])
    for item in req["items"]:
        result = {"name": item["name"], "path": item["path"]}
        dest = Path(item["path"])
        temp = dest.with_name(f".photo-editor-{uuid.uuid4().hex}{dest.suffix}")
        photo = None
        export_scene = None
        try:
            _report_event(log, {"name": item["name"], "stage": "Developing"})
            photo = package.catalog.lookup_photo(bpy.context.scene, item["uid"])
            if not photo:
                raise RuntimeError("The photo is missing from the snapshot")
            source_scene = package.compositor.ensure_photo_scene(bpy.context, photo)
            with tempfile.TemporaryDirectory(dir=log.parent, prefix="pixels-") as scratch:
                sources, developers = _render_inputs(package, photo, source_scene)
                for source in sources:
                    binding = req["bindings"].get(source.uid, {})
                    if binding.get("source"):
                        source.filepath = binding["source"]
                    package.catalog.ensure_full(bpy.context, source, scratch=scratch)
                variants = []
                for source, node in developers:
                    binding = req["bindings"].get(source.uid, {})
                    profile = binding.get("profiles", {}).get(node.instance_uid)
                    if profile:
                        node.profile_path = profile
                    lut = binding.get("luts", {}).get(node.instance_uid)
                    if lut:
                        node.lut_path = lut
                    variants.append(package.catalog.ensure_developer_full(bpy.context, source, node, scratch=scratch))
                _report_event(log, {"name": item["name"], "stage": "Rendering"})
                output_scene = bpy.data.scenes.get(req.get("output_settings_scene", ""))
                if req.get("output_settings_scene") and output_scene is None:
                    raise RuntimeError("The batch output settings are missing from the snapshot")
                export_scene = package.compositor.export_scene(
                    source_scene, req["format_override"],
                    output_settings=output_scene.render.image_settings if output_scene else None)
                package.compositor.export_photo(bpy.context, photo, str(temp), export_scene)
                if export_scene.render.image_settings.file_format == "JPEG":
                    _report_event(log, {"name": item["name"], "stage": "Copying metadata for"})
                    metadata = import_module(package.__name__ + ".metadata")
                    warning = metadata.copy_to_jpeg(package.catalog.source_file(photo), temp)
                    if warning:
                        result["warning"] = warning
                for source in sources:
                    if source.image and not source.image.is_dirty:
                        source.image.buffers_free()
                for image in variants:
                    if not image.is_dirty:
                        image.buffers_free()
            # Publish only once the render and metadata work have finished.
            try:
                if req.get("overwrite", False):
                    os.replace(temp, dest)
                elif os.name == "nt":
                    os.rename(temp, dest)
                else:
                    os.link(temp, dest)
            except FileExistsError as exc:
                raise FileExistsError(f"Destination already exists: {dest.name}") from exc
            result["ok"] = True
        except Exception as exc:
            traceback.print_exc()
            result.update(ok=False, error=str(exc))
        finally:
            temp.unlink(missing_ok=True)
            if export_scene is not None:
                camera_object = export_scene.camera
                bpy.data.scenes.remove(export_scene)
                if camera_object and not camera_object.users:
                    camera_data = camera_object.data
                    bpy.data.objects.remove(camera_object)
                    if camera_data and not camera_data.users:
                        bpy.data.cameras.remove(camera_data)
        _report_event(log, result)


if __name__ == "__main__":
    reqfile = Path(sys.argv[-1])
    data = json.loads(reqfile.read_text(encoding="utf-8"))
    for folder in reversed(data.get("python_paths", [])):
        if folder and folder not in sys.path:
            sys.path.insert(0, folder)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    dll_handles = []
    if hasattr(os, "add_dll_directory"):
        for path in data.get("dll_paths", []):
            if os.path.isdir(path):
                dll_handles.append(os.add_dll_directory(path))
    try:
        if data["mode"] == "export":
            export_batch(data)
        elif data["mode"] == "film_preview":
            film_preview(data)
        elif data["mode"] == "film_raw_preview":
            import film_backend
            film_backend.process(data["source"], data["output"], data["recipe"], raw_exposure=data["exposure"])
        elif data["mode"] == "import_thumbnails":
            for item in data['items']:
                try:
                    process_image(item)
                    if 'cache' in item:
                        # Publish only a finished thumbnail matching the file the
                        # browser listed. Interrupted jobs leave their work in the
                        # dialog's private scratch directory.
                        stat = Path(item['source']).stat()
                        if (stat.st_size, stat.st_mtime_ns) != (item['size'], item['modified']):
                            raise ValueError('Original changed while reading its thumbnail')
                        os.replace(item['output'], item['cache'])
                except Exception as error:
                    Path(item['output']).with_suffix('.failed').write_text(str(error), encoding='utf-8')
            if data.get('cache_dir'):
                _prune_import_thumbnails(data['cache_dir'])
        else:
            process_image(data)
    except Exception as exc:
        Path(data["error_path"]).write_text(str(exc), encoding="utf-8")
        traceback.print_exc()
        raise
