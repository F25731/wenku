import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from wenku_to_pdf import (
    READER_OVERLAY_HIDE_CSS,
    TOP_MASK_FIRST_PAGE,
    excel_direct_image_looks_complete,
    excel_page_image_items,
    is_mostly_blank_image,
    page_image_ready,
)


class ImageBlankDetectionTest(unittest.TestCase):
    def test_white_page_is_blank(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "blank.png"
            Image.new("RGB", (900, 1200), "white").save(path)

            self.assertTrue(is_mostly_blank_image(path))

    def test_table_page_is_not_blank(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "table.png"
            image = Image.new("RGB", (900, 1200), "white")
            draw = ImageDraw.Draw(image)
            for x in range(80, 821, 180):
                draw.line((x, 160, x, 980), fill="black", width=2)
            for y in range(160, 981, 55):
                draw.line((80, y, 820, y), fill="black", width=2)
            draw.text((140, 210), "14050141", fill="black")
            image.save(path)

            self.assertFalse(is_mostly_blank_image(path))

    def test_sparse_landscape_formula_page_is_not_blank(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "formula.png"
            image = Image.new("RGB", (1260, 890), "white")
            draw = ImageDraw.Draw(image)
            for x in (90, 250, 610, 1160):
                draw.line((x, 110, x, 760), fill="black", width=3)
            for y in (110, 235, 360, 485, 610, 760):
                draw.line((90, y, 1160, y), fill="black", width=3)
            for row, text in enumerate(("sin^2 a + cos^2 a = 1", "tan^2 a + 1 = sec^2 a", "cot^2 a + 1 = csc^2 a")):
                draw.text((300, 150 + row * 145), text, fill="black")
            image.save(path)

            self.assertFalse(is_mostly_blank_image(path))

    def test_missing_page_image_is_not_ready_for_required_nonblank_page(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "missing.png"

            self.assertFalse(page_image_ready(path, require_nonblank_pages=True))


class ReaderOverlayHideCssTest(unittest.TestCase):
    def test_hides_known_reader_overlays_before_screenshot(self):
        for selector in (".tool-bar-wrap", ".toolbar-core-btn", "#app-reader-editor-below", ".doc-hints-wrap"):
            self.assertIn(selector, READER_OVERLAY_HIDE_CSS)


class ScreenshotMaskRatioTest(unittest.TestCase):
    def test_uses_unified_first_page_top_mask_ratio(self):
        self.assertEqual(TOP_MASK_FIRST_PAGE, 0.08)


class ExcelDirectImageTest(unittest.TestCase):
    def test_sorts_docconvert_png_urls_by_range_start(self):
        urls = [
            "https://wkstatic.bdimg.com/static.png",
            "https://wkbjcloudbos.bdimg.com/v1/docconvert3396/wk/hash/0.png?x-bce-range=50773-99742",
            "https://wkbjcloudbos.bdimg.com/v1/docconvert3396/wk/hash/0.json?x-bce-range=0-508",
            "https://wkbjcloudbos.bdimg.com/v1/docconvert3396/wk/hash/0.png?x-bce-range=0-50772",
        ]

        self.assertEqual([start for start, _ in excel_page_image_items(urls)], [0, 50773])

    def test_tiny_excel_png_is_not_treated_as_complete_page(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "tiny.png"
            Image.new("RGB", (700, 1500), "white").save(path)

            self.assertFalse(excel_direct_image_looks_complete(path, min_file_bytes=10000))

    def test_substantial_excel_png_can_use_direct_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "table.png"
            image = Image.new("RGB", (960, 1356), "white")
            draw = ImageDraw.Draw(image)
            for x in range(20, 940, 40):
                draw.line((x, 20, x, 1330), fill="black", width=2)
            for y in range(20, 1330, 32):
                draw.line((20, y, 940, y), fill="black", width=2)
            for offset in range(0, 9000, 3):
                draw.point((20 + offset % 900, 30 + (offset * 7) % 1280), fill=(offset % 255, 0, 0))
            image.save(path)

            self.assertTrue(excel_direct_image_looks_complete(path, min_file_bytes=10000))


if __name__ == "__main__":
    unittest.main()
