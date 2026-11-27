# SPDX-License-Identifier: GPL-3.0-or-later
"""Rebuild the arm64 Metal backend from the accompanying corresponding source."""
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory


def build():
    vendor = Path(__file__).resolve().parent
    spectra = "SpektraHanatos2025Spectra.f32"
    gamut = "SpektraOutputGamutCompression.f32"
    dylib = "libphoto_film.dylib"
    with TemporaryDirectory(prefix="photo-spektrafilm-build-") as temporary:
        root = Path(temporary)
        for name in ("core-source.tar.gz", "additional-stock-data.tar.gz"):
            with tarfile.open(vendor / name) as archive:
                archive.extractall(root, filter="data")
        subprocess.run(["patch", "-p1", "-i", str(vendor / "extension.patch")], cwd=root, check=True)
        subprocess.run(["patch", "-p1", "-i", str(vendor / "stock-tables.patch")], cwd=root, check=True)
        subprocess.run([sys.executable, "tools/generate_profile_curves.py",
                        "--output", "SpektraGeneratedProfileCurves.cpp",
                        "--counts-output", "SpektraGeneratedProfileCounts.h",
                        "--hanatos-output", spectra, "--output-gamut-compression-output", gamut],
                       cwd=root, check=True)
        subprocess.run(["xcrun", "--sdk", "macosx", "clang++", "-std=c++17", "-arch", "arm64",
                        "-mmacosx-version-min=14.0", "-O2", "-fobjc-arc", "-dynamiclib",
                        "-Wl,-install_name,@rpath/" + dylib, "-Isrc", "-I.",
                        "src/SpektraMetalRenderer.mm", "src/SpektraAppBridge.mm", str(vendor / "photo_bridge.mm"),
                        "SpektraGeneratedProfileCurves.cpp", "-framework", "Foundation", "-framework", "Metal",
                        "-framework", "MetalPerformanceShaders", "-o", dylib], cwd=root, check=True)
        subprocess.run(["codesign", "--force", "--sign", "-", str(root / dylib)], check=True)
        for name in (dylib, spectra, gamut, "shaders/SpektraFilm.metal"):
            shutil.copyfile(root / name, vendor / Path(name).name)


if __name__ == "__main__":
    build()
