# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline catalog of camera calibrations shipped with the extension."""
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path


DIRECTORY = Path(__file__).parent / "vendor" / "camera_profiles"


def normalized(name):
    return "".join(ch for ch in name.casefold() if ch.isalnum())


@lru_cache(maxsize=1)
def _catalog():
    data = (DIRECTORY / "index.json").read_bytes()
    cameras = {(normalized(row["make"]), normalized(row["model"])): row
               for row in json.loads(data)}
    return cameras, sha256(data).hexdigest()


def match(make, model):
    """Match the complete make and model. Never substitute a similar body."""
    return _catalog()[0].get((normalized(make), normalized(model)))


def catalog_fingerprint():
    return _catalog()[1]
