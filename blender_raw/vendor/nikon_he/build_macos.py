# SPDX-License-Identifier: LGPL-2.1-or-later
"""Build the camera bridge from the pinned LibRaw sources on a development Mac."""
import argparse
from hashlib import sha256
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zig", help="Use Zig 0.14.1 instead of the macOS SDK compiler")
    parser.add_argument("--windows", action="store_true", help="Cross-compile the x64 DLL with Zig")
    parser.add_argument("--output", type=Path, help="Destination library path")
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    filename = "blenderraw_nikon_he.dll" if args.windows else "libblenderraw_nikon_he.dylib"
    output = (args.output or here / filename).resolve()
    archive = here / "LibRaw-70513b5-source.tar.gz"
    expected = "26015c279de35d9bd307876ea36480e1c5efabd4fdbad9e515c643294c2096c5"
    if sha256(archive.read_bytes()).hexdigest() != expected:
        raise ValueError("LibRaw archive differs from the pinned source")
    with tempfile.TemporaryDirectory(prefix="photo-editor-libraw-") as tmp:
        with tarfile.open(archive) as source:
            source.extractall(tmp, filter="data")
        root = Path(tmp) / "LibRaw-70513b5fd839614626f665265487e6856bed3c92"
        makefile = (root / "Makefile.am").read_text().replace("\\\n", " ")
        line = next(l for l in makefile.splitlines() if l.startswith("lib_libraw_a_SOURCES ="))
        sources = re.findall(r"src/[\w/]+\.cpp", line)
        if "src/decoders/nikonhe.cpp" not in sources:
            raise ValueError("LibRaw build does not include the Nikon HE decoder")
        artifact = root / filename
        link = ["-Wl,-dead_strip", "-Wl,-install_name,@rpath/" + filename]
        if args.windows:
            compiler = [args.zig or "zig", "c++", "-target", "x86_64-windows-gnu", "-DLIBRAW_NODLL", "-D_USE_MATH_DEFINES"]
            link = ["-lws2_32", "-s"]
        elif sys.platform == "darwin" and not args.zig:
            compiler = ["xcrun", "--sdk", "macosx", "clang++", "-arch", "arm64",
                        "-mmacosx-version-min=13.0"]
        else:
            compiler = [args.zig or "zig", "c++", "-target", "aarch64-macos.13.0"]
        subprocess.run([
            *compiler, "-std=c++17",
            "-shared", "-O2", "-g0", "-fvisibility=hidden", "-fvisibility-inlines-hidden",
            "-DLIBRAW_NOTHREADS", "-I.", "-w", str(here / "bridge.cpp"), *sources,
            *link,
            "-o", str(artifact),
        ], cwd=root, check=True)
        output.write_bytes(artifact.read_bytes())
    print(f"{output} ({output.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
