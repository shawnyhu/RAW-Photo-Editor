"""Read the links Blender drops when a registered custom group has lost its data."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import bpy


def link_record(link):
    return {"from_node": link.from_node.name, "from_socket": link.from_socket.identifier,
            "to_node": link.to_node.name, "to_socket": link.to_socket.identifier,
            "muted": link.is_muted, "multi_input_sort_id": link.multi_input_sort_id}


def read(filepath, tree_names):
    """Return the saved links of the requested trees, indexed by tree name."""
    names = sorted(set(tree_names))
    if not names:
        return {}
    with tempfile.TemporaryDirectory(prefix="photo-editor-saved-links-") as directory:
        folder = Path(directory)
        request, result = folder / "trees.json", folder / "links.json"
        request.write_text(json.dumps(names), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith("BLENDER_USER_")}
        env["BLENDER_USER_RESOURCES"] = str(folder / "profile")
        command = [bpy.app.binary_path, "--background", "--factory-startup", "--disable-autoexec",
                   "--offline-mode", "--python-exit-code", "1", "--python", __file__, "--",
                   str(filepath), str(request), str(result)]
        execution = subprocess.run(command, env=env, capture_output=True, text=True, errors="replace")
        if execution.returncode or not result.is_file():
            detail = (execution.stdout + execution.stderr).strip()[-2000:]
            raise RuntimeError(f"Could not read the saved compositor links. {detail}")
        return json.loads(result.read_text(encoding="utf-8"))


def _write_links(filepath, request_path, result_path):
    types = ("PhotoRawReader", "BlenderRawDevelop", "BlenderPhotoSpektrafilm", "BlenderPhotoSpektrafilmOFX")
    if any(bpy.types.Node.bl_rna_get_subclass_py(name) is not None for name in types):
        raise RuntimeError("The saved-link reader needs unregistered Photo Editor node types")
    # Undefined custom nodes keep their serialized sockets, including branches Blender
    # would discard while rebuilding the interface of a registered custom group.
    bpy.ops.wm.open_mainfile(filepath=filepath, load_ui=False, use_scripts=False)
    links = {}
    for name in json.loads(Path(request_path).read_text(encoding="utf-8")):
        tree = bpy.data.node_groups.get(name)
        if tree is not None:
            links[name] = [link_record(link) for link in tree.links]
    Path(result_path).write_text(json.dumps(links), encoding="utf-8")


if __name__ == "__main__":
    _write_links(*sys.argv[sys.argv.index("--") + 1:])
