import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from wenku_to_pdf import (
    PdfDirectImageNotUsable,
    READER_OVERLAY_HIDE_CSS,
    TOP_MASK_FIRST_PAGE,
    browser_context_options,
    browser_launch_options,
    browser_process_launch_options,
    excel_direct_image_looks_complete,
    excel_page_image_items,
    full_page_png_looks_complete,
    is_mostly_blank_image,
    json_callback_body,
    merge_structured_html_urls,
    page_image_ready,
    page_index_from_font_url,
    page_index_from_resource_url,
    url_with_query_params,
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


class BrowserLaunchOptionsTest(unittest.TestCase):
    def test_default_launch_options_do_not_force_system_chrome(self):
        options = browser_launch_options("profile", 2.0)

        self.assertNotIn("channel", options)
        self.assertEqual(options["user_data_dir"], "profile")
        self.assertEqual(options["device_scale_factor"], 2.0)

    def test_browser_context_options_keep_task_state_isolated(self):
        options = browser_context_options(2.0)

        self.assertEqual(options["viewport"], {"width": 1440, "height": 1800})
        self.assertEqual(options["device_scale_factor"], 2.0)
        self.assertNotIn("user_data_dir", options)

    def test_browser_process_launch_options_do_not_include_context_fields(self):
        options = browser_process_launch_options()

        self.assertTrue(options["headless"])
        self.assertIn("--no-first-run", options["args"])
        self.assertNotIn("viewport", options)


class PdfFallbackTest(unittest.TestCase):
    def test_pdf_direct_image_exception_marks_fallback_case(self):
        self.assertTrue(issubclass(PdfDirectImageNotUsable, RuntimeError))

    def test_tiny_placeholder_pdf_png_is_not_treated_as_complete_page(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "placeholder.png"
            image = Image.new("RGB", (893, 1276), "white")
            draw = ImageDraw.Draw(image)
            draw.line((0, 1270, 893, 1270), fill="black", width=4)
            image.save(path)

            self.assertFalse(full_page_png_looks_complete(path))

    def test_substantial_pdf_png_can_use_direct_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "page.png"
            image = Image.new("RGB", (893, 1276), "white")
            draw = ImageDraw.Draw(image)
            for y in range(80, 1180, 36):
                draw.line((90, y, 780, y), fill="black", width=2)
                draw.text((100, y + 4), "legal term explanation and notes", fill="black")
            for x in range(0, 893, 9):
                draw.point((x, (x * 17) % 1276), fill=(120, 120, 120))
            image.save(path)

            self.assertTrue(full_page_png_looks_complete(path))


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


class StructuredResourceTest(unittest.TestCase):
    def test_adds_edt_mode_without_dropping_existing_query(self):
        url = url_with_query_params("https://wenku.baidu.com/view/doc?aggId=abc&chatType=chat", edtMode=2)

        self.assertIn("aggId=abc", url)
        self.assertIn("chatType=chat", url)
        self.assertIn("edtMode=2", url)

    def test_extracts_callback_json_body(self):
        self.assertEqual(json_callback_body('wenku_1({"body":[]});'), '{"body":[]}')

    def test_reads_page_index_from_resource_and_font_urls(self):
        self.assertEqual(page_index_from_resource_url("https://x/docconvert/hash/0.json?x=1"), 1)
        self.assertEqual(page_index_from_resource_url("https://x/docconvert/hash/11.png?x=1"), 12)
        self.assertEqual(page_index_from_font_url("https://wkretype.bdimg.com/retype/pipe/id?pn=8&t=ttf"), 8)

    def test_merges_readerinfo_html_urls_by_page_index(self):
        json_urls = {}
        png_urls = {}
        font_urls = {}
        merge_structured_html_urls(
            {
                "data": {
                    "htmlUrls": {
                        "json": [{"pageIndex": 3, "pageLoadUrl": "https://example.test/2.json"}],
                        "png": [{"pageIndex": 3, "pageLoadUrl": "https://example.test/2.png"}],
                        "ttf": [{"pageIndex": 3, "param": "&md5sum=abc&range=1-2"}],
                    }
                }
            },
            "docid",
            json_urls,
            png_urls,
            font_urls,
        )

        self.assertEqual(json_urls[3], "https://example.test/2.json")
        self.assertEqual(png_urls[3], "https://example.test/2.png")
        self.assertIn("pn=3", font_urls[3])


if __name__ == "__main__":
    unittest.main()
