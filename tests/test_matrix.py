import io
import math
from pathlib import Path
import tempfile
import unittest
from xml.etree import ElementTree

from PIL import Image

from matrix_model import ARCANA, NODE_SPECS, calc_matrix, reduce22
from matrix_image import IMG_H, IMG_W, R, build_matrix_image, generate_matrix_image
from matrix_report import INTERPRETATIONS, POSITION_FOCUS, build_matrix_report, generate_matrix_report_html, report_sections


class MatrixCalculationTests(unittest.TestCase):
    def test_published_1984_reference_preserves_twenty_two(self):
        # Independent hand-worked table at ladinimatrix.ru/calculate-matrix-yourself/.
        expected = dict(A=4, B=10, C=22, D=9, E=9, F=14, G=5, H=4, K=13,
                        family_center=9, total_center=18, sky=19, earth=8,
                        personal=9, father=18, mother=18, social=9,
                        spiritual=18, planetary=9)
        actual = calc_matrix("1984-10-31")
        self.assertEqual({key: actual[key] for key in expected}, expected)

    def test_second_independent_reference_and_internal_channels(self):
        # Public primary calculator example: 14.03.1991; additional sums by hand.
        expected = dict(A=14, B=3, C=20, D=10, E=11, F=17, G=5, H=3, K=6,
                        C1=4, D1=21, Q=7, L=10, M=11, D2=4,
                        personal=20, father=20, mother=11, social=4, spiritual=6)
        actual = calc_matrix("1991-03-14")
        self.assertEqual({key: actual[key] for key in expected}, expected)

    def test_reduction_does_not_use_modulo_or_single_digit_root(self):
        for value, expected in ((22, 22), (23, 5), (29, 11), (31, 4), (44, 8), (99, 18), (99999999999, 18)):
            with self.subTest(value=value):
                self.assertEqual(reduce22(value), expected)
        for invalid in (0, -4, 1.5, True, "22", None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                reduce22(invalid)

    def test_invalid_dates_fail_instead_of_fabricating_a_matrix(self):
        for invalid in (None, "", "31.10.1984", "1984-1-2", "1900-02-29", "2001-02-29", "2024-04-31", "2024-13-01"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                calc_matrix(invalid)
        self.assertEqual(calc_matrix("2000-02-29")["A"], 11)

    def test_range_and_identity_for_distinct_birth_dates(self):
        for year in range(1900, 2027, 7):
            for month in range(1, 13):
                for day in (1, 22, 28):
                    date = f"{year:04d}-{month:02d}-{day:02d}"
                    matrix = calc_matrix(date)
                    self.assertEqual(len(matrix["nodes"]), 30)
                    self.assertEqual(len(matrix["summaries"]), 10)
                    for key, value in matrix.items():
                        if isinstance(value, int):
                            self.assertTrue(1 <= value <= 22, (date, key, value))
                    self.assertEqual(matrix["nodes"]["E"]["value"], matrix["E"])

    def test_ancestry_diagonals_and_age_orientation(self):
        matrix = calc_matrix("1991-03-14")
        self.assertEqual(matrix["male_line"], [17, 11, 3])
        self.assertEqual(matrix["female_line"], [5, 11, 6])
        age_map = {item["age"]: item["node"] for item in matrix["age_markers"]}
        self.assertEqual(age_map, {0: "A", 10: "F", 20: "B", 30: "G", 40: "C", 50: "H", 60: "D", 70: "K"})
        self.assertEqual(matrix["lines"]["love"], ["D1", "L", "Q"])
        self.assertEqual(matrix["lines"]["money"], ["Q", "M", "C1"])

    def test_all_circle_positions_have_clearance(self):
        nodes = calc_matrix("1984-10-31")["nodes"]
        def radius(key):
            return 51 if key == "E" else 43 if len(key) == 1 and key not in "QLM" else 31
        for i, (left, first) in enumerate(nodes.items()):
            for right, second in list(nodes.items())[i+1:]:
                distance = math.hypot(first["x"]-second["x"], first["y"]-second["y"])*R
                self.assertGreater(distance, radius(left)+radius(right)+3, (left, right, distance))
        # The shared channel node must lie exactly on the paternal diagonal;
        # love/money intermediate nodes must lie on their channel segments.
        for key in ("Q", "H1", "H2"):
            self.assertEqual(nodes[key]["x"], nodes[key]["y"])
        for key in ("D1", "L", "Q", "M", "C1"):
            self.assertAlmostEqual(nodes[key]["x"]+nodes[key]["y"], .65)


class MatrixReportTests(unittest.TestCase):
    USER = {"birth_date": "1984-10-31", "name": "Александра", "birth_city": "Москва"}

    def test_report_explains_every_plotted_position_exactly_once(self):
        matrix, sections = report_sections(self.USER)
        report = "\n".join("\n".join(section["blocks"]) for section in sections)
        for key, node in matrix["nodes"].items():
            heading = f"<b>{key} · {node['title']} — {node['value']}, {node['arcana']}</b>"
            self.assertEqual(report.count(heading), 1, key)
        for item in matrix["summaries"].values():
            self.assertIn(f"<b>{item['title']} — {item['value']}, {item['arcana']}</b>", report)
        for row in matrix["resources"]:
            self.assertIn(f"<b>{row['title']} · {row['traditional']}</b>", report)
            self.assertIn(f"Действие {row['left_id']} = {row['left']},", report)
            self.assertIn(f"Восприятие {row['right_id']} = {row['right']},", report)
            self.assertIn(f"Баланс {row['balance']},", report)
        self.assertEqual(set(INTERPRETATIONS), set(range(1, 23)))
        self.assertEqual(set(POSITION_FOCUS), set(NODE_SPECS))

    def test_chunks_are_valid_independent_html_below_telegram_limit(self):
        for date in ("1984-10-31", "1991-03-14", "2000-02-29", "1922-12-22", "2026-01-01"):
            chunks = build_matrix_report({**self.USER, "birth_date": date})
            self.assertGreater(len(chunks), 3)
            for chunk in chunks:
                self.assertLess(len(chunk), 3900)
                self.assertLess(len(chunk.encode("utf-16-le"))//2, 3900)
                root = ElementTree.fromstring("<root>"+chunk+"</root>")
                self.assertTrue(all(element.tag in ("root", "b", "i") for element in root.iter()))

    def test_date_only_report_is_deterministic_and_does_not_infer_place(self):
        report = build_matrix_report(self.USER)
        self.assertEqual(report, build_matrix_report({**self.USER, "name": "Другой человек", "birth_city": "Казань"}))

    def test_download_is_self_contained_and_escapes_user_text(self):
        html = generate_matrix_report_html({**self.USER, "name": '<script>alert("x")</script> & Анна'})
        self.assertIn("data:image/png;base64,", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn('<script>alert("x")</script>', html)
        self.assertNotIn('src="http', html)
        self.assertIn('@media print', html)


class MatrixRenderTests(unittest.TestCase):
    def test_png_has_correct_size_and_is_deterministic(self):
        content = generate_matrix_image("1984-10-31", "Имя "*40)
        self.assertEqual(content, generate_matrix_image("1984-10-31", "Имя "*40))
        with Image.open(io.BytesIO(content)) as image:
            self.assertEqual(image.size, (IMG_W, IMG_H))
            self.assertEqual(image.format, "PNG")
        self.assertLess(len(content), 5_000_000)

    def test_simultaneous_requests_get_independent_files(self):
        user = {"birth_date": "1984-10-31", "telegram_id": 123}
        paths = [Path(build_matrix_image(user)), Path(build_matrix_image(user))]
        try:
            self.assertNotEqual(paths[0], paths[1])
            self.assertEqual(paths[0].read_bytes(), paths[1].read_bytes())
        finally:
            for path in paths:
                path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
