# SPDX-License-Identifier: GPL-3.0-or-later
"""Editable native processing fields whose saved representation remains parameters."""

# Names and positions match the pinned SpektraAppBridge option sets.
RGB_TO_RAW_METHODS = ("Hanatos 2026", "Hanatos 2025", "Mallett 2019")
FILM_FORMATS = ("Standard 8", "Super 8", "Standard 16", "Super 16",
                "Standard 35", "Super 35", "Standard 65", "IMAX 70")
CHOICES = {"rgbToRawMethod": RGB_TO_RAW_METHODS, "filmFormat": FILM_FORMATS}
PARAMETERS = (*CHOICES, "filmPushPullStops")


def explicit_parameters(node):
    """Only write RNA edits; untouched legacy JSON retains its original precision."""
    values = {}
    for key in PARAMETERS:
        if node.is_property_set(key):
            value = getattr(node, key)
            values[key] = CHOICES[key].index(value) if key in CHOICES else value
    return values
