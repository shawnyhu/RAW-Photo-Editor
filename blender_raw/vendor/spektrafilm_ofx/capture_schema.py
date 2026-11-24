# SPDX-License-Identifier: GPL-3.0-or-later
"""Capture public OFX descriptors and category menus from an intact official bundle.

This reads the supported host ABI only. No shader, profile or LUT resources are
extracted. Review version/contract changes before shipping new interface data.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def capture(bundle, helper, output):
    with tempfile.TemporaryDirectory(prefix="photo-ofx-schema-") as folder:
        folder = Path(folder)
        result, request = folder / "result.json", folder / "request.json"

        def describe(parameters=None):
            command = [str(helper), "describe", str(bundle), str(result)]
            if parameters is not None:
                request.write_text(json.dumps({"parameters": parameters}), encoding="utf-8")
                command.append(str(request))
            subprocess.run(command, check=True)
            return json.loads(result.read_text(encoding="utf-8"))

        metadata = describe()
        by_name = {p["name"]: p for p in metadata["parameters"]}
        metadata["version"] = by_name["infoVersion"]["value"]
        metadata["choice_dependencies"] = {}
        for target, control in (("film", "filmCategory"), ("paper", "printCategory")):
            choices = {}
            for index, label in enumerate(by_name[control]["options"]):
                native = {p["name"]: p for p in describe({control: index})["parameters"]}
                choices[str(index)] = {"options": native[target]["options"], "default": native[target]["value"]}
                print(f"{control}: {label} — {len(native[target]['options'])} options")
            metadata["choice_dependencies"][target] = {"control": control, "values": choices}
        for p in metadata["parameters"]:
            if p["name"] in {"grainSeed", "glareSeed"}:
                p["default"] = p["value"] = p["current"] = 0
        # Keep only the fields used to reopen and validate saved adjustments.
        # Preset names and tooltip prose are read from the user's installation.
        fields = ("name", "type", "label", "group", "secret", "enabled", "value",
                  "minimum", "maximum", "options")
        local_only = {"quickPresetSelection", "quickPresetCategory", "quickPresetCategoryId", "quickPresetId"}
        metadata["parameters"] = [{key: p[key] for key in fields if key in p}
                                  for p in metadata["parameters"] if p["name"] not in local_only]
        output.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--helper", type=Path, default=root / ("photo_ofx_host.exe" if sys.platform == "win32" else "photo_ofx_host"))
    parser.add_argument("--output", type=Path, default=root / "parameters.json")
    args = parser.parse_args()
    capture(args.bundle, args.helper, args.output)
