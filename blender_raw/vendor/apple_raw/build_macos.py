# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the Apple Silicon Core Image RAW bridge using the macOS SDK."""
from pathlib import Path
import subprocess


if __name__ == '__main__':
    folder = Path(__file__).resolve().parent
    library = folder / 'libphoto_apple_raw.dylib'
    subprocess.run(['xcrun', '--sdk', 'macosx', 'clang++', '-std=c++17', '-fobjc-arc',
                    '-arch', 'arm64', '-mmacosx-version-min=13.0', '-O2', '-dynamiclib',
                    str(folder / 'bridge.mm'), '-framework', 'CoreImage', '-framework',
                    'Foundation', '-framework', 'CoreGraphics', '-framework', 'ImageIO',
                    f'-Wl,-install_name,@rpath/{library.name}', '-o', str(library)], check=True)
