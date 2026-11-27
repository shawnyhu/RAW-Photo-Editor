# SPDX-License-Identifier: GPL-3.0-or-later
"""Reproduce the additional measured stock tables used by both native builds."""
import argparse
import gzip
import io
import json
import math
from pathlib import Path
import subprocess
import tarfile
import tomllib


HERE = Path(__file__).resolve().parent
NEGS_REVISION = "2f5b5703d807e9490801d9155ac30b2c19460551"
DYEFILMS = ("kodak_ultramax_800", "kodak_vision2_50d", "kodak_vision2_100t", "kodak_vision2_500t")
GAMMAS = {
    "same_layer_rgb": "gamma_samelayer_rgb", "r_to_gb": "gamma_interlayer_r_to_gb",
    "g_to_rb": "gamma_interlayer_g_to_rb", "b_to_rg": "gamma_interlayer_b_to_rg",
}
DATA = "Resources/data/"


def read_json(path):
    return json.loads(path.read_bytes())


def couplers(info, presets):
    defaults = presets["defaults"]["color"][info["type"]]
    values = dict(defaults)
    if info["type"] == "negative" and info["use"] == "cine":
        temperature = "tungsten" if info["reference_illuminant"].upper().startswith("T") else "daylight"
        values.update(defaults["cine"][temperature])
    values.update(presets.get(info["stock"], {}))
    return {native: values[source] for native, source in GAMMAS.items()}


def pack(negs, dyeworks):
    revision = subprocess.check_output(["git", "-C", str(negs), "rev-parse", "HEAD"], text=True).strip()
    if revision != NEGS_REVISION:
        raise ValueError("Use the pinned Negs source revision " + NEGS_REVISION)
    with tarfile.open(HERE / "core-source.tar.gz") as archive:
        originals = {}
        for member in archive:
            path = Path(member.name)
            if path.parent.as_posix() == DATA + "profiles" and path.suffix == ".json":
                info = json.load(archive.extractfile(member))["info"]
                originals[info["stock"]] = info["stage"]

    source = negs / "data"
    presets = tomllib.loads((source / "presets/couplers.toml").read_text(encoding="utf8"))
    filters = read_json(source / "filters/neutral_print_filters.json")
    profiles = {}
    entries = {}
    for path in sorted((source / "profiles").glob("*.json")):
        if path.stem in originals:
            continue
        profile = read_json(path)
        info = profile["info"]
        if info.get("channel_model") != "color" or profile["metadata"].get("approximate"):
            continue
        profiles[path.stem] = (info, path)
    papers = {stock for stock, (info, _) in profiles.items() if info["stage"] == "printing"}
    all_papers = papers | {stock for stock, stage in originals.items() if stage == "printing"}
    # A selectable film needs a characterised enlarger setting on every selectable print.
    films = {stock for stock, (info, _) in profiles.items()
             if info["stage"] == "filming" and all(stock in filters[p]["TH-KG3"] for p in all_papers)}
    dir_calibration = {stock: couplers(profiles[stock][0], presets) for stock in sorted(films)}
    additions = read_json(dyeworks / "neutral_print_filters.json")
    dyeworks_couplers = tomllib.loads((dyeworks / "couplers.toml").read_text(encoding="utf8"))
    measured_cross = read_json(HERE / "dyeworks-eterna-print-filters.json")
    for stock in DYEFILMS:
        path = dyeworks / (stock + ".json")
        info = read_json(path)["info"]
        if info["stock"] != stock or info["channel_model"] != "color":
            raise ValueError("Unexpected Dyeworks profile " + stock)
        profiles[stock] = info, path
        films.add(stock)
        dir_calibration[stock] = couplers(info, dyeworks_couplers)
        for paper in all_papers:
            table = measured_cross if paper in papers else additions
            filters[paper]["TH-KG3"][stock] = table[paper]["TH-KG3"][stock]
    all_films = films | {stock for stock, stage in originals.items() if stage == "filming"}
    pairs = {}
    for paper in sorted(all_papers):
        pairs[paper] = {}
        for film in sorted(all_films if paper in papers else films):
            values = filters[paper]["TH-KG3"][film]
            if len(values) != 3 or not all(math.isfinite(v) for v in values):
                raise ValueError(f"Invalid TH-KG3 calibration for {film} on {paper}")
            pairs[paper][film] = values
    catalog = {
        "films": sorted(films), "papers": sorted(papers), "print_filters": pairs,
        "couplers": dir_calibration,
    }
    entries[DATA + "stock_catalog.json"] = json.dumps(catalog, indent=2).encode()
    for stock in papers | films:
        path = profiles[stock][1]
        entries[DATA + "profiles/" + path.name] = path.read_bytes().replace(b"\r\n", b"\n")
    for name in ("FOTUFILM_LICENSE.txt", "SPEKTRAFILM_LICENSE.txt", "CHANGELOG.txt"):
        entries["Legal/Negs/" + name] = (source / name).read_bytes().replace(b"\r\n", b"\n")
    for name in ("CHANGELOG.txt", "SPEKTRAFILM_LICENSE.txt"):
        entries["Legal/Dyeworks/" + name] = (dyeworks / name).read_bytes().replace(b"\r\n", b"\n")
    with (HERE / "additional-stock-data.tar.gz").open("wb") as output:
        with gzip.GzipFile(fileobj=output, filename="", mode="wb", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for name, contents in sorted(entries.items()):
                    entry = tarfile.TarInfo(name)
                    entry.size = len(contents)
                    archive.addfile(entry, io.BytesIO(contents))
    print(f"Packed {len(films)} additional film stocks and {len(papers)} print stocks")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("negs", type=Path)
    parser.add_argument("dyeworks", type=Path,
                        help="Dyeworks 2.2.3 profile JSONs, neutral_print_filters.json, couplers.toml and notices")
    args = parser.parse_args()
    pack(args.negs, args.dyeworks)
