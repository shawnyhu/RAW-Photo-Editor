# SPDX-License-Identifier: GPL-3.0-or-later
"""Brand-column material menus, bound to the card that opened the selector."""
import bpy
from bpy.props import EnumProperty, StringProperty

from . import spektrafilm_engine as engine


_BRANDS = ("Kodak", "Fujifilm", "Ilford", "Agfa", "CineStill", "Konica Minolta")
_CATEGORIES = ("Color Negative", "Slide", "B&W Negative", "B&W Reversal")


def _brand(record):
    name = record["name"]
    return next((brand for brand in _BRANDS if name.startswith(brand + " ")), "Other")


def _category(record, kind):
    if kind == "paper":
        return "B&W Print" if record["channel_model"] == "bw" else "Color Print"
    if record["channel_model"] == "bw":
        return "B&W Reversal" if record["type"] == "positive" else "B&W Negative"
    return "Slide" if record["type"] == "positive" else "Color Negative"


def _choices(node, kind):
    if kind == "film":
        return engine.stocks()[0]
    # In Print only mode the input may already be a negative scan; film does
    # not define its channel model. Match the existing manual-print control.
    if node.process == "Print only":
        return engine.stocks()[1]
    return engine.compatible_papers(node.film)


def _selected(node, kind):
    if kind == "film":
        return node.film or engine.default_film()
    return node.paper or engine.stocks()[1][engine._descriptors()["paper"].default_int]


def _records(kind):
    return engine._stock_catalog()["films" if kind == "film" else "papers"]


def _short_name(record, brand):
    name = record["name"]
    prefix = brand + " "
    name = name[len(prefix):] if name.startswith(prefix) else name
    if record["channel_model"] == "bw" and record["type"] == "positive":
        name = name.replace(" Reversal Film ", " ")
    if record["channel_model"] == "bw" and record["type"] == "negative":
        name = name.replace(" Negative Film ", " ")
    return name


def _columns(groups, kind):
    """Keep the picker within a laptop window, while preserving brand grouping."""
    columns = []
    if kind == "film":
        for brand in _BRANDS[:2]:
            if brand not in groups:
                continue
            categories = groups[brand]
            negatives = categories.get("Color Negative", ())
            if len(negatives) > 14:
                columns.append(((brand, {"Color Negative": negatives[:14]}),))
                remainder = dict(categories)
                remainder["Color Negative"] = negatives[14:]
                columns.append(((brand, remainder),))
            else:
                columns.append(((brand, categories),))
        others = tuple((brand, groups[brand]) for brand in (*_BRANDS[2:], "Other") if brand in groups)
        if others:
            columns.append(others)
    else:
        columns = [((brand, groups[brand]),) for brand in (*_BRANDS, "Other") if brand in groups]
    return columns


def _draw_picker(layout, node, kind):
    records = _records(kind)
    selected = _selected(node, kind)
    groups = {}
    for stock in _choices(node, kind):
        record = records[stock]
        brand = _brand(record)
        groups.setdefault(brand, {}).setdefault(_category(record, kind), []).append(stock)
    if not groups:
        layout.label(text="This reversal film is scanned directly", icon="INFO")
        return
    layout.operator_context = "EXEC_DEFAULT"
    row = layout.row()
    for brands in _columns(groups, kind):
        column = row.column(align=True)
        column.scale_y = .8 if kind == "film" else .9
        for brand_index, (brand, brand_categories) in enumerate(brands):
            if brand_index:
                column.separator()
            heading = column.row()
            heading.alignment = "LEFT"
            # Menu labels use the menu-background text colour and ignore the
            # disabled state. The native unembossed-label style restores the
            # normal theme text colour for brand headings, without a button.
            heading.emboss = "NONE"
            heading.label(text=brand.upper())
            column.separator(factor=.4)
            categories = _CATEGORIES if kind == "film" else ("Color Print", "B&W Print")
            for category in categories:
                if category not in brand_categories:
                    continue
                header = column.row()
                header.enabled = False
                header.label(text=category)
                for stock in brand_categories[category]:
                    choice = column.row(align=True)
                    choice.alignment = "LEFT"
                    op = choice.operator(BR_OT_choose_film_stock.bl_idname,
                                         text=_short_name(records[stock], brand),
                                         icon="CHECKMARK" if stock == selected else "BLANK1", emboss=False)
                    # Names identify the actual clicked card, regardless of
                    # compositor_effects.active or the node editor selection.
                    op.tree_name, op.node_name = node.id_data.name, node.name
                    op.kind, op.stock = kind, stock
                column.separator(factor=.5)


class BR_OT_choose_film_stock(bpy.types.Operator):
    bl_idname = "blender_raw.choose_film_stock"
    bl_label = "Choose Film or Print"
    bl_options = {"UNDO"}
    tree_name: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    node_name: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    kind: EnumProperty(items=(("film", "Film", ""), ("paper", "Print", "")), options={"HIDDEN", "SKIP_SAVE"})
    stock: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def description(cls, context, properties):
        record = _records(properties.kind).get(properties.stock)
        if not record:
            return "Choose a film or print material"
        if properties.kind == "film":
            return record["name"] + ": load its recommended print, processing and effect settings"
        return record["name"] + ": use this print material"

    def execute(self, context):
        from . import film_node
        if not self.tree_name or not self.node_name:
            return {"CANCELLED"}
        node = film_node._operator_node(self, context, film_node.FILM_TYPE)
        if node is None:
            return {"CANCELLED"}
        try:
            # Recheck after opening the menu: another action may have
            # changed the film, making a formerly visible print incompatible.
            if self.stock not in _choices(node, self.kind):
                self.report({"ERROR"}, "This material is unavailable for the current film")
                return {"CANCELLED"}
            if self.kind == "film":
                node.film = self.stock
            else:
                node.paper = self.stock
        except (KeyError, OSError, RuntimeError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


class BR_MT_film_stock_picker(bpy.types.Menu):
    bl_idname = "BR_MT_film_stock_picker"
    bl_label = "Film"

    def draw(self, context):
        node = getattr(context, "spektrafilm_stock_node", None)
        if node:
            self.layout.ui_units_x = 44
            _draw_picker(self.layout, node, "film")


class BR_MT_print_stock_picker(bpy.types.Menu):
    bl_idname = "BR_MT_print_stock_picker"
    bl_label = "Print"

    def draw(self, context):
        node = getattr(context, "spektrafilm_stock_node", None)
        if node:
            self.layout.ui_units_x = 37
            _draw_picker(self.layout, node, "paper")


def draw_stock(layout, node, kind="film"):
    """Draw a stock button, retaining IDs in RNA without an editable text field."""
    if kind not in {"film", "paper"}:
        raise ValueError("Stock picker kind must be film or paper")
    label = "Film" if kind == "film" else "Print"
    split = layout.split(factor=.18, align=True)
    split.label(text=label)
    button = split.row(align=True)
    button.context_pointer_set("spektrafilm_stock_node", node)
    try:
        stock = _selected(node, kind)
        name = _records(kind).get(stock, {}).get("name", stock or "Choose Material")
        button.menu(BR_MT_film_stock_picker.bl_idname if kind == "film" else BR_MT_print_stock_picker.bl_idname,
                    text=name)
    except (KeyError, OSError, RuntimeError, ValueError):
        button.label(text="Materials unavailable", icon="ERROR")


_CLASSES = (BR_OT_choose_film_stock, BR_MT_film_stock_picker, BR_MT_print_stock_picker)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
