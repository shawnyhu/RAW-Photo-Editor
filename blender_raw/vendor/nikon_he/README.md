# LibRaw camera calibration and Nikon High Efficiency decoder

Blender Photo Editor uses the bundled Windows x64 DLL or Apple Silicon dylib to read the pinned LibRaw version's camera matrices for RAWs without compatible embedded calibration. Matrix lookup opens the metadata without decoding the sensor. The same library also decodes Nikon High Efficiency NEF sensor data when the standard decoder cannot unpack it. The bridge checks LibRaw's decoder name before processing, uses the original sensor data, and returns demosaiced 16-bit camera RGB, a camera matrix and the recorded white balance. The same Blender Photo Editor color pipeline then processes both editing proxies and full-resolution photographs.

The [LibRaw November 2026 snapshot](https://www.libraw.org/node/2882) introduced Nikon HE and HE★ decoding. The unmodified upstream source archive is included as `LibRaw-70513b5-source.tar.gz` (SHA-256 `26015c279de35d9bd307876ea36480e1c5efabd4fdbad9e515c643294c2096c5`). The bridge is distributed under LGPL-2.1-or-later. The linked library is distributed under the upstream LGPL-2.1 license, which is included in the source archive. [Pinned upstream source](https://github.com/LibRaw/LibRaw/tree/70513b5fd839614626f665265487e6856bed3c92)

## Rebuild or replace the macOS dylib

On a Mac with the Apple Command Line Tools installed, run the following command from this directory using Blender 5.3's bundled Python.

```sh
/Applications/Blender.app/Contents/Resources/5.3/python/bin/python3.13 build_macos.py
otool -L libblenderraw_nikon_he.dylib
codesign --verify libblenderraw_nikon_he.dylib
```

The script verifies the source archive and builds an arm64 dylib for macOS 13 or later. The binary links LibRaw into the bridge and uses the system C++ runtime. Blender loads it alongside `bridge.cpp`. Replacing this file with an ABI-compatible build also replaces its LibRaw decoder.

## Rebuild or replace the DLL

The distributed DLL was built with Zig 0.14.1 targeting x86_64-windows-gnu. From this directory on a Mac, run the following command with Blender's Python.

```sh
/Applications/Blender.app/Contents/Resources/5.3/python/bin/python3.13 build_macos.py --windows --zig /path/to/zig
```

For a native build, use the Windows x64 build of [w64devkit 2.11.0](https://github.com/skeeto/w64devkit/releases/tag/v2.11.0). Start its shell in this directory. Extract and compile in a temporary directory of your choice. The following commands use paths relative to the extracted source tree:

```sh
bridge_dir=$(pwd)
build_dir=$(mktemp -d)
tar -xzf LibRaw-70513b5-source.tar.gz -C "$build_dir"
cd "$build_dir"/LibRaw-70513b5fd839614626f665265487e6856bed3c92
# Correct an upstream filename typo in the make prerequisite.
sed -i 's/sony_arw6c\.cpp/sony_arw6.cpp/g' Makefile.mingw
make -j8 -f Makefile.mingw 'CFLAGS=-O2 -I. -w -DLIBRAW_NODLL' library
g++ -O2 -shared -static -s -DLIBRAW_NODLL -DLIBRAW_NOTHREADS -I. \
    "$bridge_dir/bridge.cpp" lib/libraw.a -lws2_32 -o "$bridge_dir/blenderraw_nikon_he.dll"
```

The built DLL imports Windows system libraries only. Replace it alongside `bridge.cpp` to use a compatible modified build.
