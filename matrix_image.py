"""Crisp, code-drawn destiny matrix; no network access or guessed AI geometry."""
from __future__ import annotations

import io
import math
import os
import tempfile
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from matrix_model import METHOD_VERSION, calc_matrix, parse_birth_date

IMG_W, IMG_H = 1400, 2320
CX, CY, R = 700, 768, 475
INK = "#25243C"
MUTED = "#6D7083"
PALE = "#F4F3FA"
LINE = "#CFD0DF"
COLORS = {
    "violet": ("#7560BC", "#EEE8FC"), "indigo": ("#556DC5", "#E9EDFD"),
    "sky": ("#3A88BC", "#E4F3FC"), "green": ("#287969", "#E5F3ED"),
    "gold": ("#8E6825", "#FBEECB"), "orange": ("#B16B36", "#FCEBDC"),
    "coral": ("#B35D67", "#FBE9EB"), "blue": ("#557EAF", "#EDF2F9"),
    "rose": ("#B46992", "#F8EEF4"),
}


@lru_cache(maxsize=64)
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    custom = os.getenv("MATRIX_FONT_BOLD" if bold else "MATRIX_FONT_REGULAR")
    candidates = [custom] if custom else []
    candidates += [
        str(Path(__file__).parent / "assets" / "fonts" / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    # Pillow's default font does not reliably contain Cyrillic. Fail clearly so
    # paid delivery can be retried instead of shipping unreadable glyphs.
    raise RuntimeError("Установите fonts-dejavu-core или укажите MATRIX_FONT_REGULAR/BOLD")


def _center(draw, xy, text, font, fill=INK):
    draw.text(xy, str(text), font=font, fill=fill, anchor="mm")


def _fit(draw, text, width, size, bold=False):
    text = " ".join(str(text).split())
    while size > 21 and draw.textlength(text, font=_font(size, bold)) > width:
        size -= 1
    while text and draw.textlength(text, font=_font(size, bold)) > width:
        text = text[:-2] + "…"
    return text, _font(size, bold)


def _point(node):
    return CX + R * node["x"], CY + R * node["y"]


def _dashed(draw, start, end, color, width=3, dash=12, gap=9):
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy)
    for offset in range(0, int(length), dash + gap):
        stop = min(length, offset + dash)
        draw.line([(start[0] + dx * offset / length, start[1] + dy * offset / length),
                   (start[0] + dx * stop / length, start[1] + dy * stop / length)], fill=color, width=width)


def _node(draw, node):
    x, y = _point(node)
    key = node["id"]
    radius = 51 if key == "E" else 43 if len(key) == 1 and key not in "QLM" else 31
    edge, fill = COLORS[node["color"]]
    if key == "E":
        draw.ellipse((x-radius-8, y-radius-8, x+radius+8, y+radius+8), outline="#EAD9A7", width=2)
    draw.ellipse((x-radius, y-radius+4, x+radius, y+radius+4), fill="#E7E5EF")
    draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=fill, outline=edge, width=2)
    _center(draw, (x, y-14 if radius > 40 else y-11), key, _font(15 if radius > 40 else 12, True), edge)
    _center(draw, (x, y+11 if radius > 40 else y+8), node["value"], _font(43 if radius > 40 else 30, True), INK)


def _card(draw, box, title, eyebrow=None):
    draw.rounded_rectangle(box, radius=24, fill="#FFFFFF", outline="#E4E1ED", width=2)
    x, y, _, _ = box
    if eyebrow:
        draw.text((x+30, y+22), eyebrow, font=_font(17, True), fill=COLORS["violet"][0])
    draw.text((x+30, y+48 if eyebrow else y+24), title, font=_font(29, True), fill=INK)


def generate_matrix_image(date_str: str, name: str = "") -> bytes:
    matrix = calc_matrix(date_str)
    nodes = matrix["nodes"]
    image = Image.new("RGB", (IMG_W, IMG_H), "#F8F7FC")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((30, 28, IMG_W-30, IMG_H-28), radius=40, fill="#FCFBFF", outline="#E4E0EF", width=2)
    _center(draw, (CX, 63), "ЛИЧНАЯ КАРТА · 22 АРКАНА", _font(20, True), COLORS["violet"][0])
    _center(draw, (CX, 121), "Матрица судьбы", _font(56, True))
    nice_date = parse_birth_date(date_str).strftime("%d.%m.%Y")
    name = " ".join(str(name or "").split())[:120]
    subtitle = f"{name}  ·  {nice_date}" if name else nice_date
    subtitle, font = _fit(draw, subtitle, 1150, 27)
    _center(draw, (CX, 174), subtitle, font, MUTED)

    # Correct age orientation: left=0, top=20, right=40, bottom=60.
    perimeter = "AFBGCHDK"
    for index, key in enumerate(perimeter):
        vertex = _point(nodes[key])
        next_vertex = _point(nodes[perimeter[(index+1) % 8]])
        start = (CX+(vertex[0]-CX)*1.135, CY+(vertex[1]-CY)*1.135)
        end = (CX+(next_vertex[0]-CX)*1.135, CY+(next_vertex[1]-CY)*1.135)
        draw.line([start, end], fill="#E8E4F0", width=1)
        for tick in range(1, 10):
            t = tick / 10
            x = start[0] + (end[0]-start[0])*t
            y = start[1] + (end[1]-start[1])*t
            dx, dy = x-CX, y-CY
            length = math.hypot(dx, dy)
            ux, uy = dx/length, dy/length
            draw.line([(x, y), (x+ux*8, y+uy*8)], fill="#BDB8CD", width=2)
            if tick == 5:
                _center(draw, (x+ux*28, y+uy*28), f"{index*10+5}", _font(21), MUTED)
        dx, dy = vertex[0]-CX, vertex[1]-CY
        length = math.hypot(dx, dy)
        label = "0 / 80 лет" if index == 0 else f"{index*10} лет"
        offset = 122 if key == "A" else 100 if key == "C" else 82
        _center(draw, (vertex[0]+dx/length*offset, vertex[1]+dy/length*offset), label, _font(22, True), MUTED)

    for keys in ("ABCD", "FGHK"):
        for i, key in enumerate(keys):
            draw.line([_point(nodes[key]), _point(nodes[keys[(i+1) % 4]])], fill=LINE, width=2)
    draw.line([_point(nodes["A"]), _point(nodes["C"])], fill="#C9C5D6", width=2)
    draw.line([_point(nodes["B"]), _point(nodes["D"])], fill="#C9C5D6", width=2)
    for start, end, color in (("F", "H", "blue"), ("G", "K", "rose")):
        draw.line([_point(nodes[start]), _point(nodes[end])], fill=COLORS[color][0], width=3)
    _dashed(draw, _point(nodes["D1"]), _point(nodes["Q"]), COLORS["rose"][0], 4)
    _dashed(draw, _point(nodes["Q"]), _point(nodes["C1"]), COLORS["green"][0], 4)
    _center(draw, (CX+30, CY+167), "ОТНОШЕНИЯ", _font(16, True), COLORS["rose"][0])
    _center(draw, (CX+245, CY+135), "ДЕНЬГИ", _font(17, True), COLORS["green"][0])
    # Clean uninterrupted labels in empty quadrants.
    _center(draw, (CX-211, CY+83), "ЛИНИЯ МАТЕРИ", _font(17, True), COLORS["rose"][0])
    _center(draw, (CX-211, CY-83), "ЛИНИЯ ОТЦА", _font(17, True), COLORS["blue"][0])
    for node in nodes.values():
        _node(draw, node)

    _center(draw, (CX, 1362), "Число в круге — аркан. Буква — адрес точки в разборе.", _font(23), MUTED)
    _center(draw, (CX, 1399), "Внешняя шкала — возрастные ориентиры; штрих = 1 год.", _font(21), MUTED)

    _card(draw, (85, 1450, 675, 1740), "Твои направления", "ИТОГИ СХЕМЫ")
    for index, key in enumerate(("personal", "social", "spiritual", "planetary")):
        y = 1545 + index*43
        item = matrix["summaries"][key]
        draw.text((115, y), item["title"], font=_font(23), fill=MUTED)
        draw.text((637, y), str(item["value"]), font=_font(26, True), fill=INK, anchor="ra")
    _card(draw, (700, 1450, 1315, 1740), "Твоя опора", "ВЗАИМОСВЯЗИ")
    for index, (label, keys) in enumerate((("Небо · Земля", ("sky", "earth")),
                                         ("Отец · Мать", ("father", "mother")),
                                         ("Личная · Родовая", ("E", "family_center")),
                                         ("Общая опора", ("total_center",)))):
        y = 1545+index*43
        draw.text((730, y), label, font=_font(23), fill=MUTED)
        draw.text((1278, y), " · ".join(str(matrix[k]) for k in keys), font=_font(26, True), fill=INK, anchor="ra")

    _card(draw, (85, 1780, 1315, 2202), "Ресурсные уровни", "СИМВОЛИЧЕСКАЯ КАРТА")
    columns = (124, 744, 943, 1170)
    for x, label in zip(columns, ("Сфера", "Действие", "Восприятие", "Баланс")):
        draw.text((x, 1871), label, font=_font(21, True), fill=MUTED)
    for index, row in enumerate(matrix["resources"]):
        y = 1916 + index*36
        if index % 2 == 0:
            draw.rounded_rectangle((110, y-5, 1289, y+30), radius=8, fill="#F6F4FA")
        draw.text((columns[0], y), row["title"], font=_font(23), fill=INK)
        for x, field in zip(columns[1:], ("left", "right", "balance")):
            draw.text((x+30, y), str(row[field]), font=_font(24, True), fill=INK)
    _center(draw, (CX, 2243), "Карта для самопознания. Значения — темы для размышления, не предсказания.", _font(19), MUTED)
    _center(draw, (CX, 2276), "Расчёт по дате рождения · 8 — Справедливость · 11 — Сила", _font(18), MUTED)
    buffer = io.BytesIO()
    image.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


def build_matrix_image(user: dict) -> str:
    """Return an independent temporary PNG; the caller deletes it after sending."""
    user = dict(user)
    png = generate_matrix_image(user.get("birth_date"), user.get("name", ""))
    with tempfile.NamedTemporaryFile(prefix="matrix_", suffix=".png", delete=False) as handle:
        handle.write(png)
        return handle.name


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("date", nargs="?", default="1984-10-31")
    parser.add_argument("--name", default="Александра")
    parser.add_argument("--output", default="matrix-preview.png")
    args = parser.parse_args()
    Path(args.output).write_bytes(generate_matrix_image(args.date, args.name))
    print(args.output)
