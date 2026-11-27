"""Check the built extension and create a history-free source ZIP from the same files."""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import tomllib
import zipfile


ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {
    "README.md", "CHANGELOG.md", "LICENSE", ".gitignore", ".gitattributes", "AGENTS.md",
    "docs/workflow.md", "docs/development.md", "docs/development-log.md",
    "docs/spektrafilm-beta5-mac-handoff.md",
    "docs/spektrafilm-beta6-mac-handoff.md",
    "docs/auto-level-beta7-mac-handoff.md",
}
STOCK_FILES = {
    "additional-stock-data.tar.gz", "pack_stock_data.py", "pack_stock_grain.py",
    "stock_grain.toml", "stock_grain_areas.json", "stock-tables.patch",
    "stock_defaults.json", "bw-stock-model.patch",
    "dyeworks-eterna-print-filters.json", "NOTICE.md", "CHANGELOG.txt",
}
STOCK_NOTICES = {
    "Legal/Negs/FOTUFILM_LICENSE.txt", "Legal/Negs/SPEKTRAFILM_LICENSE.txt",
    "Legal/Negs/CHANGELOG.txt", "Legal/Dyeworks/SPEKTRAFILM_LICENSE.txt",
    "Legal/Dyeworks/CHANGELOG.txt",
}


def inspect_file(name, data):
    parts = PurePosixPath(name.lower()).parts
    if PurePosixPath(name).is_absolute() or ".." in parts:
        raise ValueError(f"Invalid archive path: {name}")
    if any(p == ".git" or p.endswith((".dcp", ".ofx", ".ofx.bundle")) or
           p.startswith(("vcomp", "blender_photo_editor-")) and p.endswith((".dll", ".zip"))
           for p in parts):
        raise ValueError(f"Unexpected distribution file: {name}")
    if name.endswith((".whl", ".zip")):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for entry in archive.infolist():
                if not entry.is_dir():
                    inspect_file(entry.filename, archive.read(entry))
    elif name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for entry in archive:
                if entry.isfile():
                    inspect_file(entry.name, archive.extractfile(entry).read())


def build(artifact, source):
    with zipfile.ZipFile(artifact) as package:
        paths = set(package.namelist())
        manifest = tomllib.loads(package.read("blender_manifest.toml").decode())
        assert manifest["maintainer"] == "ShawnHu" and manifest["id"] == "raw_photo_editor"
        assert manifest["license"] == ["SPDX:GPL-3.0-or-later"]
        assert len(manifest["wheels"]) == 4 and all(p.removeprefix("./") in paths for p in manifest["wheels"])
        wheel, = [p for p in paths if p.startswith("wheels/rawpy-") and p.endswith("win_amd64.whl")]
        with zipfile.ZipFile(io.BytesIO(package.read(wheel))) as rawpy:
            assert {"rawpy/raw_r.dll", "rawpy/libomp.dll"} <= set(rawpy.namelist())
            assert any(p.endswith("/LICENSE.LLVM-OpenMP") for p in rawpy.namelist())
            assert any(p.endswith("/LICENSE.ittnotify") for p in rawpy.namelist())
        schema = json.loads(package.read("vendor/spektrafilm_ofx/parameters.json"))
        assert all("hint" not in row for row in schema["parameters"])
        assert set(schema["choice_dependencies"]) == {"film", "paper"}
        assert "quickPresetSelection" not in {p["name"] for p in schema["parameters"]}
        assert "10.25039/CIE.DS.xvudnb9b" in package.read("vendor/spektrafilm/NOTICE.md").decode()
        required = {"vendor/spektrafilm/" + name for name in STOCK_FILES}
        assert required <= paths, "Missing bundled stock sources: " + str(sorted(required - paths))
        assert not any(name.startswith("assets/film_presets/") for name in paths), "Retired built-in presets must not ship"
        with tarfile.open(fileobj=io.BytesIO(package.read("vendor/spektrafilm/additional-stock-data.tar.gz"))) as stocks:
            assert STOCK_NOTICES <= set(stocks.getnames()), "Missing stock license or attribution"
            assert all(stocks.extractfile(name).read().strip() for name in STOCK_NOTICES)
        for name in paths:
            if name.endswith("/"):
                continue
            data = package.read(name)
            assert data == (ROOT / "blender_raw" / name).read_bytes(), name
            inspect_file(name, data)

    # Include new, non-ignored working files before a release commit is made.
    # Keep the existing directory allowlist so dist and local investigation
    # material cannot enter the source archive through this discovery step.
    files = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT
    ).decode().split("\0")
    selected = {p for p in files if (ROOT / p).is_file() and
                (p in ROOT_FILES or p.startswith(("blender_raw/", "tools/", "tests/", ".github/workflows/")))}
    selected.add("blender_raw/" + wheel)
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for path in sorted(selected):
            data = (ROOT / path).read_bytes()
            inspect_file(path, data)
            output.write(ROOT / path, path)
    with zipfile.ZipFile(source) as archive, zipfile.ZipFile(artifact) as package:
        assert archive.testzip() is None
        for name in paths:
            if not name.endswith("/"):
                assert package.read(name) == archive.read("blender_raw/" + name), name
    print(f"Verified {len(paths)} installer entries and {len(selected)} source files.")
    for path in (artifact, source):
        print(hashlib.sha256(path.read_bytes()).hexdigest(), path.name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("installer", type=Path)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    build(args.installer, args.source)
