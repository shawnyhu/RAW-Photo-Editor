# SPDX-License-Identifier: GPL-3.0-or-later
"""Read the EOS R8 Mark II's as-shot white balance from a still CR3."""
import mmap
import struct


def _boxes(data, start, end):
    """Iterate ISO BMFF siblings inside their parent, skipping payloads."""
    while start + 8 <= end:
        size, kind = struct.unpack_from('>I4s', data, start)
        header = 8
        if size == 1:
            if start + 16 > end:
                return
            size, = struct.unpack_from('>Q', data, start + 8)
            header = 16
        elif size == 0:
            size = end - start
        if not header <= size <= end - start:
            return
        yield kind, start + header, start + size
        start += size


def _sample_tables(data, start, end, path):
    for kind, body, stop in _boxes(data, start, end):
        if kind == path[0]:
            if len(path) == 1:
                yield {tag: (pos, limit) for tag, pos, limit in _boxes(data, body, stop)
                       if tag in {b'stsd', b'stsz', b'co64', b'stco'}}
            else:
                yield from _sample_tables(data, body, stop, path[1:])


def _sample_bounds(data, table):
    # Still CR3s have one CTMD sample in one chunk.
    desc, end = table.get(b'stsd', (0, 0))
    if end - desc < 16 or data[desc+4:desc+8] != b'\0\0\0\1':
        return None
    entry_size, kind = struct.unpack_from('>I4s', data, desc + 8)
    if kind != b'CTMD' or not 8 <= entry_size <= end - desc - 8:
        return None
    pos, end = table.get(b'stsz', (0, 0))
    if end - pos < 12:
        return None
    size, count = struct.unpack_from('>II', data, pos + 4)
    if count != 1:
        return None
    if size == 0:
        if end - pos < 16:
            return None
        size, = struct.unpack_from('>I', data, pos + 12)
    for tag, width in ((b'co64', 8), (b'stco', 4)):
        pos, end = table.get(tag, (0, 0))
        if end - pos >= 8 + width and data[pos+4:pos+8] == b'\0\0\0\1':
            offset = int.from_bytes(data[pos+8:pos+8+width], 'big')
            return offset, offset + size
    return None


def _maker_white_balance(data, start, end):
    # Layout documented at https://exiftool.org/TagNames/Canon.html#ColorData12
    if end - start < 8 or data[start:start+4] != b'II*\0':
        return None
    relative, = struct.unpack_from('<I', data, start + 4)
    pos = start + relative
    if relative < 8 or pos + 2 > end:
        return None
    count, = struct.unpack_from('<H', data, pos)
    pos += 2
    if count * 12 > end - pos:
        return None
    for entry in range(pos, pos + count * 12, 12):
        tag, typ, words, relative = struct.unpack_from('<HHII', data, entry)
        if tag != 0x4001 or typ != 3 or words < 109:
            continue
        color = start + relative
        if color + 2 * words > end or struct.unpack_from('<H', data, color)[0] != 67:
            continue
        r, g1, g2, b = struct.unpack_from('<4h', data, color + 105 * 2)
        if min(r, g1, g2, b) > 0:
            return r, (g1 + g2) / 2, b
    return None


def _records(data, start, end, header, fmt):
    while start + header <= end:
        size, kind = struct.unpack_from(fmt, data, start)
        if not header <= size <= end - start:
            return
        yield kind, start + header, start + size
        start += size


def as_shot(path):
    """Return RGB multipliers for known ColorData 67, or None for other layouts."""
    try:
        with open(path, 'rb') as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
            media = [(body, end) for kind, body, end in _boxes(data, 0, len(data)) if kind == b'mdat']
            table_path = (b'moov', b'trak', b'mdia', b'minf', b'stbl')
            for table in _sample_tables(data, 0, len(data), table_path):
                bounds = _sample_bounds(data, table)
                if bounds is None or not any(start <= bounds[0] < bounds[1] <= end for start, end in media):
                    continue
                for kind, body, end in _records(data, *bounds, 12, '<IH'):
                    if kind == 8:
                        for tag, start, stop in _records(data, body, end, 8, '<II'):
                            if tag == 0x927C:
                                wb = _maker_white_balance(data, start, stop)
                                if wb is not None:
                                    return wb
    except (OSError, ValueError):
        pass  # Empty files and files which cannot be mapped have no usable match.
    return None
