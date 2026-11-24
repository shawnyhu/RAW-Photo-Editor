# RAW decoder distribution

The optional Spektrafilm OFX node uses a separately installed official plugin. Its small bundled OpenFX host, corresponding source, upstream revisions and license notices are documented in [spektrafilm_ofx/NOTICE.md](spektrafilm_ofx/NOTICE.md). The embedded Spektrafilm Lite engine retains its existing [notices](spektrafilm/NOTICE.md).

RAW Photo Editor uses the upstream rawpy 0.28.0 CPython 3.13 bindings. The Apple Silicon wheel is unmodified. The Windows wheel uses a project build of LibRaw 0.22.2 and LLVM OpenMP 22.1.8. Blender selects the wheel for the operating system and CPU, and supplies NumPy and OpenImageIO. [Upstream release files and hashes](https://pypi.org/project/rawpy/0.28.0/#files)

| Upstream wheel input | SHA-256 |
|---|---|
| Windows x86-64 | `5dddd07b8c126402962f75562a66f94b30ee554405d5b4ae4d4ca2dbad8c5aca` |
| macOS Apple Silicon | `2628942390a8acf9811cfa29430c8716f1c4ee559f836e94745d0795aacef58c` |

* **rawpy** is distributed under the MIT License, copyright Maik Riechert. The wheel includes the license, Cython wrapper source and public type declarations. [Upstream release](https://github.com/letmaik/rawpy/tree/v0.28.0) · [PyPI](https://pypi.org/project/rawpy/0.28.0/)
* **LibRaw 0.22.2** is included as a separate dynamically loaded library (`raw_r.dll` on Windows and `libraw_r.25.dylib` on macOS). The LGPL 2.1 license is in each wheel. The corresponding upstream source archive accompanies this notice. The library can be replaced with a compatible build. [LibRaw source revision](https://github.com/LibRaw/LibRaw/tree/b93f6e45c194f5df9b02a43b1af9a54b4f41f33f) · [CMake integration](https://github.com/LibRaw/LibRaw-cmake/tree/6e26c9e73677dc04f9eb236a97c6a4dc225ba7e8)
* The Windows build uses zlib, libjpeg-turbo, JasPer, Little CMS and LLVM's `libomp.dll`. The wheel includes the LLVM license. [LLVM 22.1.8 sources](https://github.com/llvm/llvm-project/tree/llvmorg-22.1.8/openmp) · [Build script](../../tools/build_windows_rawpy.py). Run the script from an x64 VS developer prompt with the upstream Windows wheel, LLVM 22.1.8 and Ninja. The release workflow verifies all upstream hashes and decoder flags before packaging. [Upstream build recipe](https://github.com/letmaik/rawpy/blob/v0.28.0/.github/scripts/build-windows.ps1)
* The Apple Silicon wheel bundles libjpeg-turbo, JasPer and Little CMS in `rawpy/.dylibs` and links to the system's zlib. Their license notices are included in this directory.

The offline installer includes two MIT-licensed daylight matrices and model identifiers for embedded DNG calibration. See the [camera data notices](camera_profiles/NOTICE.md) and [profile index](camera_profiles/index.json). External DCP files are selected by the user.

User-supplied RGB camera ICC profiles are converted with Little CMS 2.20. The [ICC engine notices](lcms2/NOTICE.md) list the offline Windows and Mac libraries, license, fixed versions and hashes.

## JPEG metadata

* **pyexiv2 2.16.1** is distributed under GPLv3. Both wheels include the Python and C++ binding sources. The Windows x64 wheel is the unmodified upstream release with SHA-256 `16a3f442341bcd36f4a829a48fbc97d243376fbe2a8a682df0752c5f6b77ac6f`. The Apple Silicon wheel is a project build with SHA-256 `f031ee1c8f6431dd21adffdc991db845c3b1d2b2a6ab85da19899d288696bac1`. It uses the same binding source and a Mac loader that imports the module directly. The module finds the bundled Exiv2 library through `@loader_path`. The pybind11 license accompanies this file. [Upstream package](https://pypi.org/project/pyexiv2/2.16.1/) · [Binding source](https://github.com/LeoHsiao1/pyexiv2)
* **Exiv2 0.28.9**, copyright the Exiv2 authors, is distributed under GPL-2.0-or-later. The unmodified Windows DLL matches the upstream 0.28.9 MSVC AMD64 release. Its SHA-256 is `7f79a1de4a8132de7376ccf4e38bf11acbfacf81c878da215d5bfd51bf4c0f17`. Exiv2 and its embedded Adobe XMP Toolkit notices accompany this package. [Corresponding source archive](https://github.com/Exiv2/exiv2/archive/refs/tags/v0.28.9.tar.gz) · [Build instructions and binary release](https://github.com/Exiv2/exiv2/releases/tag/v0.28.9)
* The Mac build of Exiv2 links the **Brotli 1.2.0** decoder statically. It reads BMFF/CR3 and supports XMP. macOS supplies libexpat, zlib, libiconv, libc++ and libSystem. Brotli is distributed under the MIT license in `LICENSE.Brotli`. [Corresponding Brotli source](https://github.com/google/brotli/tree/v1.2.0)

### Rebuilding the macOS wheel

The checked-in wheel targets CPython 3.13 and macOS 13.0 on arm64. To rebuild it on an Apple Silicon development machine, install the Xcode Command Line Tools and use Blender 5.3's bundled Python. Place the following three archives in a directory named `sources`, using the linked filenames.

* [exiv2-0.28.9.tar.gz](https://codeload.github.com/Exiv2/exiv2/tar.gz/refs/tags/v0.28.9)
* [brotli-1.2.0.tar.gz](https://codeload.github.com/google/brotli/tar.gz/refs/tags/v1.2.0)
* [Python-3.13.13.tar.xz](https://www.python.org/ftp/python/3.13.13/Python-3.13.13.tar.xz)

From the project root, run these commands. The build script checks the archive and upstream wheel hashes, applies the deployment target to each native library and rejects non-system dependencies outside the wheel. Only the resulting wheel is needed on end users' machines.

```sh
BLENDER_PYTHON="/Applications/Blender.app/Contents/Resources/5.3/python/bin/python3.13"
BUILD_TOOLS="$(mktemp -d)"
"$BLENDER_PYTHON" -m pip install --target "$BUILD_TOOLS" cmake==3.31.6 pybind11==3.0.1 wheel==0.45.1
PYTHONPATH="$BUILD_TOOLS" PATH="$BUILD_TOOLS/cmake/data/bin:$PATH" \
    "$BLENDER_PYTHON" tools/build_macos_metadata.py sources
```
