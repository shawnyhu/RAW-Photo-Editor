"""Build the Windows rawpy wheel with the LLVM OpenMP runtime.

Run from an x64 Visual Studio developer prompt, with upstream LLVM and Ninja
installed. Pass a new build directory and the unmodified rawpy 0.28.0 wheel.
"""
import argparse
import base64
import csv
import hashlib
import io
from pathlib import Path
import subprocess
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.28.0+sh1"
WHEEL = f"rawpy-{VERSION}-cp313-cp313-win_amd64.whl"
UPSTREAM_HASH = "5dddd07b8c126402962f75562a66f94b30ee554405d5b4ae4d4ca2dbad8c5aca"


def checkout(directory, project, revision):
    directory.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=directory, check=True)
    subprocess.run(["git", "fetch", "-q", "--depth=1", f"https://github.com/{project}.git", revision],
                   cwd=directory, check=True)
    subprocess.run(["git", "checkout", "-q", "--detach", "FETCH_HEAD"], cwd=directory, check=True)


def build(work, llvm):
    vcpkg, cmake = work / "vcpkg", work / "libraw-cmake"
    checkout(vcpkg, "microsoft/vcpkg", "9e593bb18ea69cc5095e012465dcd675a822ed0d")
    checkout(cmake, "LibRaw/LibRaw-cmake", "6e26c9e73677dc04f9eb236a97c6a4dc225ba7e8")
    subprocess.run([str(vcpkg / "bootstrap-vcpkg.bat"), "-disableMetrics"], cwd=vcpkg, check=True)
    triplet = "x64-windows-static-md"
    subprocess.run([str(vcpkg / "vcpkg.exe"), "install", "zlib", "libjpeg-turbo[jpeg8]", "jasper", "lcms",
                    "--triplet", triplet, "--recurse"], cwd=vcpkg, check=True)
    with tarfile.open(ROOT / "blender_raw/vendor/LibRaw-0.22.2-source.tar.gz") as source:
        source.extractall(work / "source", filter="data")
    source_dir, = (work / "source").glob("*/libraw/libraw.h")
    compiler = llvm / "bin/clang-cl.exe"
    subprocess.run([str(compiler), "--version"], check=True)
    build_dir = work / "build"
    subprocess.run(["cmake", "-G", "Ninja", "-S", str(cmake), "-B", str(build_dir),
                    "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreadedDLL",
                    f"-DCMAKE_CXX_COMPILER={compiler.as_posix()}",
                    f"-DCMAKE_TOOLCHAIN_FILE={(vcpkg / 'scripts/buildsystems/vcpkg.cmake').as_posix()}",
                    f"-DVCPKG_TARGET_TRIPLET={triplet}", f"-DCMAKE_PREFIX_PATH={(vcpkg / 'installed' / triplet).as_posix()}",
                    f"-DLIBRAW_PATH={source_dir.parents[1].as_posix()}", "-DENABLE_X3FTOOLS=ON",
                    "-DENABLE_6BY9RPI=ON", "-DENABLE_EXAMPLES=OFF", "-DENABLE_RAWSPEED=OFF",
                    "-DENABLE_OPENMP=ON", "-DOpenMP_CXX_FLAGS=-openmp", "-DOpenMP_CXX_LIB_NAMES=libomp",
                    f"-DOpenMP_libomp_LIBRARY={(llvm / 'lib/libomp.lib').as_posix()}"], check=True)
    subprocess.run(["cmake", "--build", str(build_dir), "--target", "raw_r", "--parallel", "4"], check=True)
    dll, = build_dir.rglob("raw_r.dll")
    runtime = llvm / "bin/libomp.dll"
    imports = subprocess.check_output(["dumpbin", "/dependents", str(dll)], text=True).lower()
    if "libomp.dll" not in imports or "vcomp" in imports:
        raise RuntimeError(f"Unexpected RAW decoder import table: {imports}")
    return dll, runtime


def repack(original, destination, dll, runtime):
    if hashlib.sha256(original.read_bytes()).hexdigest() != UPSTREAM_HASH:
        raise ValueError("Upstream Windows wheel checksum mismatch")
    info = f"rawpy-{VERSION}.dist-info"
    with zipfile.ZipFile(original) as upstream:
        files = {name.replace("rawpy-0.28.0.dist-info", info): upstream.read(name)
                 for name in upstream.namelist() if name != "rawpy/vcomp140.dll" and not name.endswith("/RECORD")}
    files["rawpy/raw_r.dll"] = dll.read_bytes()
    files["rawpy/libomp.dll"] = runtime.read_bytes()
    for license_name in ("LICENSE.LLVM-OpenMP", "LICENSE.ittnotify"):
        files[f"{info}/licenses/{license_name}"] = (ROOT / "blender_raw/vendor" / license_name).read_bytes()
    metadata = f"{info}/METADATA"
    files[metadata] = files[metadata].replace(b"Version: 0.28.0", f"Version: {VERSION}".encode(), 1)
    record = io.StringIO(newline="")
    writer = csv.writer(record)
    for name, data in sorted(files.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        writer.writerow([name, "sha256=" + digest, len(data)])
    writer.writerow([f"{info}/RECORD", "", ""])
    files[f"{info}/RECORD"] = record.getvalue().encode()
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as result:
        for name, data in sorted(files.items()):
            result.writestr(name, data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work", type=Path)
    parser.add_argument("upstream_wheel", type=Path)
    parser.add_argument("--llvm", type=Path, default=Path("C:/Program Files/LLVM"))
    args = parser.parse_args()
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=False)
    repack(args.upstream_wheel, work / WHEEL, *build(work, args.llvm))
    print(work / WHEEL)
