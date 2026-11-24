# SPDX-License-Identifier: GPL-3.0-or-later
"""Encode the color-managed film output as a full-chroma, maximum-quality JPEG."""
from contextlib import closing
import shutil

import numpy as np
import OpenImageIO as oiio


_STAMPS = {'File', 'Note', 'Date', 'Marker', 'Time', 'Frame', 'FrameRange', 'Camera',
           'Lens', 'Scene', 'Strip', 'RenderTime', 'Memory', 'Hostname'}


def encode(source, destination):
    # Blender's 16-bit PNG preserves the selected display transform, ICC and stamp text.
    # The JPEG writer in Blender always subsamples chroma, even at maximum quality.
    reader = oiio.ImageInput.open(str(source))
    if not reader:
        raise OSError(oiio.geterror())
    intermediate = source.with_suffix('.444.jpg')
    with closing(reader):
        original = reader.spec()
        spec = oiio.ImageSpec(original.width, original.height, 3, oiio.UINT8)
        stamps = []
        for item in original.extra_attribs:
            if item.name in {'ICCProfile', 'XResolution', 'YResolution', 'oiio:ColorSpace'}:
                spec.extra_attribs.append(item)
            elif item.name in _STAMPS:
                stamps.append(f'Blender:{item.name}:{item.value}'.encode('utf-8'))
        unit = original.get_string_attribute('ResolutionUnit')
        spec.attribute('ResolutionUnit', 'in' if unit == 'inch' else unit)
        spec.attribute('jpeg:subsampling', '4:4:4')
        spec.attribute('CompressionQuality', 100)
        writer = oiio.ImageOutput.create(str(intermediate))
        if not writer or not writer.open(str(intermediate), spec):
            raise OSError(writer.geterror() if writer else oiio.geterror())
        try:
            for y in range(0, spec.height, 64):
                end = min(y + 64, spec.height)
                rgb = reader.read_scanlines(0, 0, y, end, 0, 0, 3, oiio.UINT16)
                if rgb is None:
                    raise OSError(reader.geterror())
                rgb8 = ((rgb.astype(np.uint32) + 128) // 257).astype(np.uint8)
                if not writer.write_scanlines(y, end, 0, rgb8):
                    raise OSError(writer.geterror())
        finally:
            if not writer.close():
                raise OSError(writer.geterror())

    # Blender stores each render stamp in a separate JPEG COM segment. OIIO's
    # encoder supports one generic comment, so retain Blender's original convention.
    with intermediate.open('rb') as jpeg, destination.open('xb') as out:
        out.write(jpeg.read(2))  # SOI
        if jpeg.peek(2)[:2] == b'\xff\xe0':  # Keep JFIF immediately after SOI.
            out.write(jpeg.read(2))
            size = jpeg.read(2)
            out.write(size + jpeg.read(int.from_bytes(size, 'big') - 2))
        # Blender's APP1 marker records JPEG quality for reopening the file.
        out.write(b'\xff\xe1\x00\x0cNeoGeo' + (100).to_bytes(4, 'big'))
        for stamp in stamps:
            stamp = stamp[:65533]
            out.write(b'\xff\xfe' + (len(stamp) + 2).to_bytes(2, 'big') + stamp)
        shutil.copyfileobj(jpeg, out)
