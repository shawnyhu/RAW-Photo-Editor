# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the isolated OFX host; no plugin, GPU SDK or Python package is needed."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent


def build(compiler=None):
    windows = sys.platform == 'win32'
    mac = sys.platform == 'darwin'
    compiler = compiler or ('x86_64-w64-mingw32-clang++' if windows else 'clang++')
    output = ROOT / ('photo_ofx_host.exe' if windows else 'photo_ofx_host')
    sources = ['src/photo_ofx_host.cpp', 'src/ofx/OfxPlugins.cpp', 'src/ofx/OfxSuites.cpp',
               'src/ofx/OfxPropsSuite.cpp', 'src/ofx/OfxRuntimeSuites.cpp',
               'src/core/RenderContext.cpp', 'src/core/Json.cpp']
    sources.append('src/ofx/OfxMetal.mm' if mac else 'src/ofx/OfxMetalStub.cpp')
    flags = ['-std=c++17', '-O2', '-Isrc', '-Iinclude']
    if windows:
        flags += ['-static', '-s', '-municode', '-Wl,--no-insert-timestamp']
    elif mac:
        flags += ['-arch', 'arm64', '-mmacosx-version-min=14.0', '-framework', 'Metal',
                  '-framework', 'Foundation']
    else:
        flags += ['-pthread', '-ldl']
    with tempfile.TemporaryDirectory(prefix='photo-ofx-host-') as temporary:
        target = Path(temporary) / output.name
        subprocess.run([compiler, *flags, *sources, '-o', str(target)], cwd=ROOT, check=True)
        shutil.copyfile(target, output)
    if mac:
        output.chmod(0o755)
        subprocess.run(['codesign', '--force', '--sign', '-', str(output)], check=True)
    print(output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cxx')
    build(parser.parse_args().cxx)
