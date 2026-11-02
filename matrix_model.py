"""One versioned, deterministic source of truth for the symbolic 22-arcanum matrix.

The date-only matrix does not replace the bot's date/place-based daily readings.
See docs/MATRIX_METHOD.md for equations, sources and interpretation boundaries.
"""
from __future__ import annotations

from datetime import date
import re

METHOD_VERSION = "matrix-22-v2"
ARCANA = {
    1: "Маг", 2: "Жрица", 3: "Императрица", 4: "Император", 5: "Иерофант",
    6: "Влюблённые", 7: "Колесница", 8: "Справедливость", 9: "Отшельник",
    10: "Колесо Фортуны", 11: "Сила", 12: "Повешенный", 13: "Трансформация",
    14: "Умеренность", 15: "Дьявол", 16: "Башня", 17: "Звезда", 18: "Луна",
    19: "Солнце", 20: "Суд", 21: "Мир", 22: "Шут",
}

# x/y are normalized to the radius. The renderer never invents extra numbers.
NODE_SPECS = {
    "A": ("Личность", -1, 0, "violet", "core"),
    "B": ("Таланты", 0, -1, "violet", "core"),
    "C": ("Реализация", 1, 0, "coral", "core"),
    "D": ("Уроки опыта", 0, 1, "coral", "core"),
    "E": ("Внутренняя опора", 0, 0, "gold", "core"),
    "F": ("Отцовские ценности", -.7071, -.7071, "blue", "family"),
    "G": ("Материнские ценности", .7071, -.7071, "rose", "family"),
    "H": ("Отцовские действия", .7071, .7071, "blue", "family"),
    "K": ("Материнские действия", -.7071, .7071, "rose", "family"),
    "A2": ("Образ себя", -.825, 0, "indigo", "personal"),
    "A1": ("Самовыражение", -.65, 0, "sky", "personal"),
    "A3": ("Внутренний ресурс", -.345, 0, "green", "personal"),
    "B2": ("Видение", 0, -.825, "indigo", "talents"),
    "B1": ("Развитие таланта", 0, -.65, "sky", "talents"),
    "B3": ("Желание сердца", 0, -.345, "green", "talents"),
    "C2": ("Привычки в делах", .825, 0, "coral", "money"),
    "C1": ("Вход в денежную линию", .65, 0, "orange", "money"),
    "D2": ("Повторяющийся урок", 0, .825, "coral", "lessons"),
    "D1": ("Вход в отношения", 0, .65, "orange", "love"),
    "F2": ("Отцовская установка", -.55, -.55, "blue", "family"),
    "F1": ("Осмысление отцовского опыта", -.39, -.39, "blue", "family"),
    "G2": ("Материнская установка", .55, -.55, "rose", "family"),
    "G1": ("Осмысление материнского опыта", .39, -.39, "rose", "family"),
    "H2": ("Отцовская привычка", .55, .55, "blue", "family"),
    "H1": ("Применение отцовского опыта", .43, .43, "blue", "family"),
    "K2": ("Материнская привычка", -.55, .55, "rose", "family"),
    "K1": ("Применение материнского опыта", -.39, .39, "rose", "family"),
    "Q": ("Баланс близости и дел", .325, .325, "gold", "balance"),
    "L": ("Диалог в отношениях", .1625, .4875, "rose", "love"),
    "M": ("Обмен и вознаграждение", .4875, .1625, "green", "money"),
}

SUMMARY_SPECS = {
    "sky": "Небо", "earth": "Земля", "personal": "Личное направление",
    "father": "Итог отцовской линии", "mother": "Итог материнской линии",
    "social": "Социальное направление", "spiritual": "Духовное направление",
    "planetary": "Общий вклад", "family_center": "Родовая опора",
    "total_center": "Общая опора",
}

RESOURCE_SPECS = (
    ("Смыслы", "Сахасрара", "A", "B"),
    ("Видение", "Аджна", "A2", "B2"),
    ("Голос", "Вишудха", "A1", "B1"),
    ("Контакт", "Анахата", "A3", "B3"),
    ("Опора", "Манипура", "E", "E"),
    ("Чувства", "Свадхистана", "C1", "D1"),
    ("Устойчивость", "Муладхара", "C", "D"),
)


def reduce22(value: int) -> int:
    """Digit sum ONLY above 22; zero/negative/non-integer values are invalid."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("Для расчёта требуется положительное целое число")
    while value > 22:
        value = sum(map(int, str(value)))
    return value


def parse_birth_date(value: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Укажи дату рождения в формате ГГГГ-ММ-ДД")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Такой даты рождения нет в календаре") from exc


def calc_matrix(date_str: str) -> dict:
    born = parse_birth_date(date_str)
    values = {"A": reduce22(born.day), "B": born.month,
              "C": reduce22(sum(map(int, str(born.year))))}
    formulas = {"A": "r(день)", "B": "месяц", "C": "r(сумма цифр года)"}

    def add(key: str, *parents: str) -> int:
        values[key] = reduce22(sum(values[p] for p in parents))
        formulas[key] = "r(" + " + ".join(parents) + ")"
        return values[key]

    add("D", "A", "B", "C")
    add("E", "A", "B", "C", "D")
    for key, parents in {"F": ("A", "B"), "G": ("B", "C"),
                         "H": ("C", "D"), "K": ("D", "A")}.items():
        add(key, *parents)
    for key in ("A", "B", "C", "D", "F", "G", "H", "K"):
        add(key + "1", key, "E")
        add(key + "2", key, key + "1")
    for key in ("A", "B"):
        add(key + "3", key + "1", "E")
    add("Q", "C1", "D1")
    add("L", "D1", "Q")
    add("M", "C1", "Q")
    for key, parents in {
        "sky": ("B", "D"), "earth": ("A", "C"),
        "personal": ("sky", "earth"), "father": ("F", "H"),
        "mother": ("G", "K"), "social": ("father", "mother"),
        "spiritual": ("personal", "social"), "planetary": ("social", "spiritual"),
        "family_center": ("F", "G", "H", "K"), "total_center": ("E", "family_center"),
    }.items():
        add(key, *parents)

    nodes = {key: {"id": key, "value": values[key], "title": spec[0],
                   "x": spec[1], "y": spec[2], "color": spec[3], "group": spec[4],
                   "formula": formulas[key], "arcana": ARCANA[values[key]]}
             for key, spec in NODE_SPECS.items()}
    summaries = {key: {"id": key, "title": title, "value": values[key],
                        "formula": formulas[key], "arcana": ARCANA[values[key]]}
                 for key, title in SUMMARY_SPECS.items()}
    resources = [{"id": f"resource_{i}", "title": title, "traditional": traditional,
                  "left_id": left, "right_id": right, "left": values[left],
                  "right": values[right], "balance": reduce22(values[left] + values[right])}
                 for i, (title, traditional, left, right) in enumerate(RESOURCE_SPECS)]
    lines = {"male": ["F", "F2", "F1", "E", "H1", "H2", "H"],
             "female": ["G", "G2", "G1", "E", "K1", "K2", "K"],
             "love": ["D1", "L", "Q"], "money": ["Q", "M", "C1"],
             "lessons": ["D1", "D2", "D"], "talents": ["B", "B2", "B1", "B3"],
             "personal": ["A", "A2", "A1", "A3"]}
    ages = [{"age": i * 10, "node": key} for i, key in enumerate("AFBGCHDK")]
    # Preserve old scalar compatibility keys without drawing false edge-centres.
    for key, parents in {"FG": ("F", "G"), "GH": ("G", "H"),
                         "HK": ("H", "K"), "KF": ("K", "F")}.items():
        add(key, *parents)
    return {**values, "method_version": METHOD_VERSION, "birth_date": date_str,
            "nodes": nodes, "summaries": summaries, "resources": resources,
            "formulas": formulas, "lines": lines, "age_markers": ages,
            "male_line": [values[k] for k in ("F", "E", "H")],
            "female_line": [values[k] for k in ("G", "E", "K")]}
