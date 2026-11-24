# SPDX-License-Identifier: GPL-3.0-or-later
"""Rebuild the offline pyexiv2 wheel with Blender 5.3's macOS Python.

Pass a directory containing the three upstream archives listed in THIRD_PARTY.md.
The developer environment needs cmake on PATH, pybind11 and wheel.
"""
import argparse
import hashlib
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

import pybind11


ARCHIVES = {
    "exiv2-0.28.9.tar.gz": "700b76b97695b2fab4ef8c79619c68ae57d09e0c130724791cafbd39e0eb4aef",
    "brotli-1.2.0.tar.gz": "816c96e8e8f193b40151dad7e8ff37b1221d019dbcb9c35cd3fadbfe6477dfec",
    "Python-3.13.13.tar.xz": "2ab91ff401783ccca64f75d10c882e957bdfd60e2bf5a72f8421793729b78a71",
}


def check_hash(path, expected):
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
            raise ValueError(f"{path.name} does not match its pinned SHA-256")


def run(*cmd):
    subprocess.run(cmd, check=True)


def build(work, archives, destination):
    for name, expected in ARCHIVES.items():
        path = archives / name
        check_hash(path, expected)
        with tarfile.open(path) as source:
            source.extractall(work, filter="data")

    prefix = work / "prefix"
    common = ["-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_OSX_DEPLOYMENT_TARGET=13.0",
              "-DCMAKE_OSX_ARCHITECTURES=arm64", f"-DCMAKE_INSTALL_PREFIX={prefix}",
              "-DCMAKE_IGNORE_PREFIX_PATH=/opt/homebrew;/usr/local"]
    for name, options in [
        ("brotli-1.2.0", ["-DBUILD_SHARED_LIBS=OFF", "-DBROTLI_BUILD_TOOLS=OFF"]),
        ("exiv2-0.28.9", [f"-DCMAKE_PREFIX_PATH={prefix}", "-DBUILD_SHARED_LIBS=ON",
                          "-DEXIV2_BUILD_EXIV2_COMMAND=OFF", "-DEXIV2_ENABLE_INIH=OFF",
                          "-DEXIV2_ENABLE_NLS=OFF", "-DEXIV2_ENABLE_BMFF=ON",
                          "-DEXIV2_ENABLE_BROTLI=ON"]),
    ]:
        out = work / (name + "-build")
        run("cmake", "-S", work / name, "-B", out, *common, *options)
        run("cmake", "--build", out, "--parallel", "8")
        run("cmake", "--install", out)

    wheels = Path(__file__).resolve().parents[1] / "blender_raw" / "wheels"
    upstream = wheels / "pyexiv2-2.16.1-cp313-none-win_amd64.whl"
    check_hash(upstream, "16a3f442341bcd36f4a829a48fbc97d243376fbe2a8a682df0752c5f6b77ac6f")
    stage = work / "wheel"
    with zipfile.ZipFile(upstream) as source:
        for name in source.namelist():
            if not name.endswith((".dll", ".pyd")):
                source.extract(name, stage)
    lib = stage / "pyexiv2/lib"
    dylib = lib / "libexiv2.dylib"
    module = lib / "exiv2api.so"
    shutil.copyfile(prefix / "lib/libexiv2.0.28.9.dylib", dylib)
    run("install_name_tool", "-id", "@loader_path/libexiv2.dylib", dylib)
    run("codesign", "--force", "--sign", "-", dylib)
    run("xcrun", "clang++", "-std=c++17", "-arch", "arm64", "-mmacosx-version-min=13.0",
        "-O2", "-fvisibility=hidden", "-fvisibility-inlines-hidden", "-bundle",
        "-undefined", "dynamic_lookup", "-I", work / "Python-3.13.13/Include",
        "-I", Path(sys.prefix) / "include/python3.13", "-I", pybind11.get_include(),
        "-I", prefix / "include", lib / "exiv2api.cpp", dylib, "-o", module)
    for binary in (module, dylib):
        run("codesign", "--verify", binary)
        for line in subprocess.check_output(["otool", "-L", binary], text=True).splitlines()[1:]:
            dependency = line.strip().split(" (", 1)[0]
            if dependency != "@loader_path/libexiv2.dylib" and not dependency.startswith("/usr/lib/"):
                raise RuntimeError(f"External dependency in {binary.name}: {dependency}")
    (lib / "__init__.py").write_text(
        "# The extension module loads the adjacent libexiv2.dylib via @loader_path.\n"
        "from . import exiv2api\n")
    (stage / "pyexiv2-2.16.1.dist-info/WHEEL").write_text(
        "Wheel-Version: 1.0\nGenerator: Blender Photo Editor\nRoot-Is-Purelib: false\n"
        "Tag: cp313-cp313-macosx_13_0_arm64\n")
    run(sys.executable, "-m", "wheel", "pack", stage, "-d", destination or wheels)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archives", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if sys.platform != "darwin" or platform.machine() != "arm64" or sys.version_info[:2] != (3, 13):
        parser.error("Use Blender 5.3's Apple Silicon Python 3.13 interpreter.")
    with tempfile.TemporaryDirectory(prefix="photo-editor-metadata-build-") as folder:
        build(Path(folder), args.archives, args.output_dir)
