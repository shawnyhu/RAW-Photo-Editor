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
# Appended after beta.5's colour order; these positions are serialized in .blend files.
BW_FILMS = ("kodak_doublex", "kodak_trix", "ilford_hp5_plus", "ilford_fp4_plus",
            "ilford_delta_3200", "kodak_tmax_100", "fujifilm_acros_100_ii",
            "kodak_trix400_d76_8min")
BW_PAPERS = ("kodak_2302", "ilford_mgrc_deluxe_grade2")
GAMMAS = {
    "same_layer_rgb": "gamma_samelayer_rgb", "r_to_gb": "gamma_interlayer_r_to_gb",
    "g_to_rb": "gamma_interlayer_g_to_rb", "b_to_rg": "gamma_interlayer_b_to_rg",
}
DATA = "Resources/data/"


def read_json(path):
    return json.loads(path.read_bytes())


def couplers(info, presets):
    model = info["channel_model"]
    defaults = presets["defaults"][model][info["type"]]
    values = dict(defaults)
    if model == "color" and info["type"] == "negative" and info["use"] == "cine":
        temperature = "tungsten" if info["reference_illuminant"].upper().startswith("T") else "daylight"
        values.update(defaults["cine"][temperature])
    values.update(presets.get(info["stock"], {}))
    result = {native: values[source] for native, source in GAMMAS.items()}
    if model == "bw":
        # Negs uses only channel zero. Lite broadcasts the scalar to three lanes;
        # zero cross terms prevent fictitious inter-colour inhibition.
        result = {key: ([result[key][0]] * 3 if key == "same_layer_rgb" else [0.0, 0.0])
                  for key in result}
    return result


def reference_time(profile):
    """Negs profile.rs development_index: explicit normal time, else floor-middle."""
    data = profile["data"]
    times = data["development_time"]
    return data.get("default_development_time", times[(len(times) - 1) // 2])


def git_bytes(checkout, path):
    """Read pinned source objects even when the local checkout is sparse."""
    return subprocess.check_output(["git", "-C", str(checkout), "show", f"HEAD:{path}"])


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
    colour_films, colour_papers = sorted(films), sorted(papers)
    bw_times = {}
    for stock in (*BW_FILMS, *BW_PAPERS):
        path = source / "profiles" / (stock + ".json")
        profile = read_json(path)
        if profile["info"].get("channel_model") != "bw":
            raise ValueError("Expected a single-channel B&W profile: " + stock)
        profiles[stock] = profile["info"], path
        bw_times[stock] = reference_time(profile)
        if stock in BW_FILMS:
            dir_calibration[stock] = couplers(profile["info"], presets)
    # B&W materials have no chromatic filtration calibration. The neutral
    # enlarger setting is meaningful only on supported B&W negative/B&W print
    # pairs. The remaining entries are table-shape sentinels, never claims of
    # measured colour-negative printing; the application rejects those pairs.
    compatibility = {paper: sorted(all_films) for paper in sorted(all_papers)}
    for paper in sorted(all_papers):
        pairs[paper].update({film: [0.0, 50.0, 50.0] for film in BW_FILMS})
    bw_negatives = [film for film in BW_FILMS if profiles[film][0]["type"] == "negative"]
    for paper in BW_PAPERS:
        compatibility[paper] = bw_negatives
        pairs[paper] = {film: [0.0, 50.0, 50.0] for film in sorted(all_films) + list(BW_FILMS)}
    films.update(BW_FILMS)
    papers.update(BW_PAPERS)
    catalog = {
        "films": colour_films + list(BW_FILMS), "papers": colour_papers + list(BW_PAPERS), "print_filters": pairs,
        "couplers": dir_calibration,
        "bw_films": list(BW_FILMS), "bw_papers": list(BW_PAPERS),
        "bw_reference_time": bw_times, "compatible_films": compatibility,
        "bw_print_filter_note": "[0,50,50] is the neutral enlarger setting; unsupported cross-model entries are sentinels, not calibration.",
    }
    entries[DATA + "stock_catalog.json"] = json.dumps(catalog, indent=2).encode()
    for stock in papers | films:
        path = profiles[stock][1]
        entries[DATA + "profiles/" + path.name] = path.read_bytes().replace(b"\r\n", b"\n")
    for name in ("FOTUFILM_LICENSE.txt", "SPEKTRAFILM_LICENSE.txt", "CHANGELOG.txt"):
        entries["Legal/Negs/" + name] = (source / name).read_bytes().replace(b"\r\n", b"\n")
    for name in ("CHANGELOG.txt", "SPEKTRAFILM_LICENSE.txt"):
        entries["Legal/Dyeworks/" + name] = (dyeworks / name).read_bytes().replace(b"\r\n", b"\n")
    # Keep the experimental preparations auditable without redistributing the
    # manufacturers' raster/PDF excerpts as independently licensed artwork.
    source_records = ("README.md", "recipe.json", "trix400-d76-8min.csv",
                      "trix400-sensitivity.csv", "ilford-mgrc-grade2.csv")
    for name in source_records:
        path = "data/profile-sources/bw-expansion/" + name
        entries["Sources/Negs/" + path] = git_bytes(negs, path).replace(b"\r\n", b"\n")
    for name in ("prepare.py", "extract_sources.py", "requirements.txt"):
        path = "scripts/bw_expansion/" + name
        entries["Sources/Negs/" + path] = git_bytes(negs, path).replace(b"\r\n", b"\n")
    entries["Sources/Negs/data/presets/couplers.toml"] = (source / "presets/couplers.toml").read_bytes().replace(b"\r\n", b"\n")
    entries["Sources/README.txt"] = (
        "Pinned source: https://github.com/noahhaasis/negs/tree/" + NEGS_REVISION + "\n"
        "B&W profile JSONs and source records are retained without modification.\n"
        "The experimental preparation records retain all source conditions and limitations.\n"
        "Manufacturer PDF/raster excerpts are not included in this distribution.\n"
        "Running the upstream preparation/overlay scripts requires the complete pinned\n"
        "checkout, its spectral LUT, and the source excerpts acquired under their own rights.\n"
        "Lite's native rebuild only needs the supplied profile JSONs, not those excerpts.\n"
        "The upstream preparation/profile data licence is CC BY-SA 4.0; manufacturer\n"
        "source rights remain with the respective holders. See each source record.\n"
    ).encode()
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
