"""Verify that a UI-only installer revision retains the exact rendered implementation.

python tests/spektrafilm_display_equivalence.py rendered.zip final.zip
"""
import ast
import sys
import zipfile


DISPLAY_FUNCTIONS = {"film_node.py": {"_draw_stocks"}, "film_stock_ui.py": {"_draw_picker"}}


def without_display(source, names):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            node.body = [ast.Pass()]
    return ast.dump(tree, include_attributes=False)


with zipfile.ZipFile(sys.argv[1]) as rendered, zipfile.ZipFile(sys.argv[2]) as final:
    assert set(rendered.namelist()) == set(final.namelist()), "Installer entries changed"
    changed = []
    for name in rendered.namelist():
        before, after = rendered.read(name), final.read(name)
        if before == after:
            continue
        assert name in DISPLAY_FUNCTIONS, f"Unverified runtime change: {name}"
        assert without_display(before, DISPLAY_FUNCTIONS[name]) == without_display(after, DISPLAY_FUNCTIONS[name]), name
        changed.append(name)
print("PASS identical native binaries, models, recipe and persistence code; only display function bodies differ:", changed)
