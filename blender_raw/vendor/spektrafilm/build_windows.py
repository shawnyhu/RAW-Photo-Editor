# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the x64 Blender Vulkan backend from the two accompanying source archives."""
import argparse
from filecmp import cmp
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile


HERE = Path(__file__).resolve().parent
TABLES = ("SpektraHanatos2025Spectra.f32", "SpektraOutputGamutCompression.f32")


def build(args):
    with tempfile.TemporaryDirectory(prefix="photo-film-vulkan-") as temporary:
        root = Path(temporary)

        def run(*command):
            subprocess.run(command, cwd=root, check=True)

        for name in ("core-source.tar.gz", "vulkan-source.tar.gz", "additional-stock-data.tar.gz"):
            with tarfile.open(HERE / name) as archive:
                archive.extractall(root, filter="data")

        run("git", "apply", "--no-index", HERE / "stock-tables.patch")
        run(sys.executable, "tools/generate_profile_curves.py",
            "--output", "SpektraGeneratedProfileCurves.cpp",
            "--counts-output", "SpektraGeneratedProfileCounts.h",
            "--hanatos-output", TABLES[0], "--output-gamut-compression-output", TABLES[1])
        for table in TABLES:
            if not cmp(root / table, HERE / table, shallow=False):
                raise ValueError(f"{table} differs from the shared Metal/Vulkan runtime table")

        run("git", "apply", "--no-index", HERE / "windows.patch")
        shaders = root / "shaders"
        binaries = []
        for source in sorted((shaders / "vulkan").glob("*.comp")):
            binary = shaders / (source.name + ".spv")
            run(args.glslang, "-V", source, "-o", binary)
            binaries.append(binary)
        compiler = [args.cxx, "-std=c++17", "-O2", "-static", "-Wl,--no-insert-timestamp",
                    "-s", "-Isrc", "-I.", "-I" + str(args.vulkan_include.resolve())]
        renderer = ["src/SpektraVulkanRenderer.cpp", "SpektraGeneratedProfileCurves.cpp"]
        loader = args.vulkan_lib.resolve()
        if args.test:
            exe = root / "SpektraVulkanCopyHarness.exe"
            run(*compiler, "tools/SpektraVulkanCopyHarness.cpp", *renderer, loader, "-o", exe)
            run(exe, "--width", "72", "--height", "32", "--iterations", "1")
            run(exe, "--width", "320", "--height", "240", "--iterations", "1",
                "--core-pass", "--tile-mode", "legacy", "--float-only")
            run(exe, "--width", "3267", "--height", "1081", "--iterations", "1",
                "--compare-tile-parity", "--core-pass", "--float-only", "--print-scan-pass",
                "--camera-diffusion-pass", "--print-diffusion-pass", "--scanner-post-pass")

        dll = root / "photo_film.dll"
        run(*compiler, "-shared", "-x", "c++", "src/SpektraAppBridge.mm", HERE / "photo_bridge.mm",
            *renderer, "-x", "none", HERE / "photo_film.def", loader, "-o", dll)
        if args.test:
            import ctypes
            lib = ctypes.CDLL(str(dll))
            probe = lib.photo_film_probe
            probe.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
            probe.restype = ctypes.c_int
            status = ctypes.create_string_buffer(2048)
            if not probe(status, len(status)):
                raise RuntimeError(status.value.decode(errors="replace"))
            print("Vulkan GPU:", status.value.decode())
            # Windows keeps the DLL on disk while this process holds the handle.
            import _ctypes
            _ctypes.FreeLibrary(lib._handle)

        (args.output_dir / "shaders").mkdir(parents=True, exist_ok=True)
        for binary in binaries:
            shutil.copyfile(binary, args.output_dir / "shaders" / binary.name)
        shutil.copyfile(dll, args.output_dir / dll.name)


if __name__ == "__main__":
    sdk = Path(os.environ.get("VULKAN_SDK", "."))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cxx", default="x86_64-w64-mingw32-clang++")
    parser.add_argument("--glslang", default="glslangValidator")
    parser.add_argument("--vulkan-include", type=Path, default=sdk / "Include")
    parser.add_argument("--vulkan-lib", type=Path, default=sdk / "Lib" / "vulkan-1.lib")
    parser.add_argument("--output-dir", type=Path, default=HERE)
    parser.add_argument("--test", action="store_true", help="Run the pinned GPU harness and probe")
    build(parser.parse_args())
