"""Compare rawpy builds in separate processes so their native DLLs cannot mix."""
import gzip
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

import numpy as np


def sample(folder, source, output):
    sys.path.insert(0, str(folder))
    import rawpy
    with rawpy.imread(str(source)) as raw:
        np.save(output, raw.postprocess(output_bps=16, use_camera_wb=True, no_auto_bright=True))
    print(json.dumps([rawpy.libraw_version, rawpy.flags], sort_keys=True))


if __name__ == "__main__" and sys.argv[1] == "--sample":
    sample(*map(Path, sys.argv[2:]))
elif __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        fixture = Path(__file__).resolve().parents[1] / "tests/fixtures/highlight-chart.dng.gz"
        dng = work / "chart.dng"
        dng.write_bytes(gzip.decompress(fixture.read_bytes()))
        flags = []
        pixels = []
        for index, wheel in enumerate(sys.argv[1:]):
            folder = work / str(index)
            with zipfile.ZipFile(wheel) as package:
                package.extractall(folder)
            out = folder / "rgb.npy"
            flags.append(json.loads(subprocess.check_output(
                [sys.executable, __file__, "--sample", str(folder), str(dng), str(out)], text=True)))
            pixels.append(np.load(out))
        assert flags[0] == flags[1] and flags[1][1]["OPENMP"], flags
        np.testing.assert_array_equal(*pixels)
        print("Identical 16-bit RAW pixels, LibRaw version and decoder feature flags.", flags[1])
