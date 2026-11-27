"""Check compiled stock IDs and physical calibration against the shipped source catalog.

Runs without Blender or GPU access; a successful report is NOT a pixel-render pass.
"""
import argparse
import ctypes as ct
import hashlib
import json
from pathlib import Path
import platform
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "blender_raw"))
import spektrafilm_engine as engine


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    films, papers = engine.stocks()
    catalog = engine._stock_catalog()
    assert len(films) == 64 and len(papers) == 14, (len(films), len(papers))
    assert set(films) == set(catalog["films"])
    assert set(papers) == set(catalog["papers"])
    read = engine._library().photo_film_stock_calibration
    read.argtypes = [ct.c_int32, ct.POINTER(engine._StockCalibration)]
    read.restype = ct.c_char_p
    checked = []
    for index, film in enumerate(films):
        record = catalog["films"][film]
        value = engine._StockCalibration()
        assert read(index, ct.byref(value)).decode() == record["type"], film
        for key, _kind in value._fields_:
            np.testing.assert_allclose(tuple(getattr(value, key)), record["parameters"][key],
                                       atol=1e-6, rtol=0, err_msg=f"{film}: {key}")
        for key, parameter in record["parameters"].items():
            engine.validate_parameter(key, parameter)
        if record["target_print"]:
            assert record["target_print"] in engine.compatible_papers(film), film
        else:
            assert record["type"] == "positive", film
        checked.append(film)
    vendor = ROOT / "blender_raw/vendor/spektrafilm"
    files = ("additional-stock-data.tar.gz", "stock_defaults.json", "bw-stock-model.patch",
             "libphoto_film.dylib" if sys.platform == "darwin" else "photo_film.dll")
    result = {"platform": platform.platform(), "machine": platform.machine(),
              "status": "PASS native catalog; no GPU pixels tested", "films": checked, "papers": papers,
              "sha256": {name: hashlib.sha256((vendor / name).read_bytes()).hexdigest() for name in files}}
    if args.report:
        args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(result["status"], len(films), len(papers), flush=True)


if __name__ == "__main__":
    main()
