"""Exercise the still CR3 as-shot WB reader using synthetic metadata only."""
from pathlib import Path
import importlib.util
import struct
from tempfile import TemporaryDirectory

source = Path(__file__).resolve().parents[1] / 'blender_raw' / 'canon_wb.py'
spec = importlib.util.spec_from_file_location('canon_wb', source)
canon_wb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(canon_wb)


def box(kind, payload):
    return struct.pack('>I4s', 8 + len(payload), kind) + payload


def still(*, version=67, levels=(1896, 1024, 1026, 1672), offset=8, chunk_tag=b'co64'):
    # The first top-level box holds one timed metadata sample starting at byte 8.
    # Its type-8 record contains a Canon MakerNote TIFF with a ColorData entry.
    color = struct.pack('<H', version) + bytes(104 * 2) + struct.pack('<4h', *levels)
    ifd = struct.pack('<HHHIII', 1, 0x4001, 3, len(color) // 2, 26, 0)
    tiff = b'II' + struct.pack('<HI', 42, 8) + ifd + color
    note = struct.pack('<II', len(tiff) + 8, 0x927C) + tiff
    sample = struct.pack('<IH6x', len(note) + 12, 8) + note
    desc = box(b'stsd', struct.pack('>II', 0, 1) + box(b'CTMD', bytes(8)))
    sizes = box(b'stsz', struct.pack('>IIII', 0, 0, 1, len(sample)))
    pos_format = '>IIQ' if chunk_tag == b'co64' else '>III'
    chunk = box(chunk_tag, struct.pack(pos_format, 0, 1, offset))
    header = box(b'stbl', desc + sizes + chunk)
    for kind in (b'minf', b'mdia', b'trak', b'moov'):
        header = box(kind, header)
    return box(b'mdat', sample) + header


with TemporaryDirectory() as tmp:
    raw = Path(tmp) / 'camera.CR3'
    for chunk_tag in (b'co64', b'stco'):
        raw.write_bytes(still(chunk_tag=chunk_tag))
        assert canon_wb.as_shot(raw) == (1896, 1025, 1672)

    malformed = {
        'empty': b'',
        'truncated media': still()[:20],
        'invalid sample address': still(offset=0xFFFFFF),
        'sample crosses media boundary': still(offset=9),
        'invalid container size': bytes.fromhex('00000002') + still()[4:],
        'unsupported track type': still().replace(b'CTMD', b'CRAW'),
        'unknown metadata version': still(version=68),
        'invalid channel': still(levels=(0, 1024, 1024, 1672)),
    }
    for case, data in malformed.items():
        raw.write_bytes(data)
        assert canon_wb.as_shot(raw) is None, case

print('Verified Canon as-shot WB and safe fallback for unsupported or damaged metadata')
