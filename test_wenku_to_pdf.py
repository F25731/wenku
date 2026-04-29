import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

import wenku_to_pdf
from structured_json_pdf import choose_pdf_font_for_text, normalize_text_for_pdf
from wenku_to_pdf import (
    PdfDirectImageNotUsable,
    READER_OVERLAY_HIDE_CSS,
    TOP_MASK_FIRST_PAGE,
    browser_context_options,
    browser_launch_options,
    browser_process_launch_options,
    decode_response_text,
    excel_direct_image_looks_complete,
    excel_page_image_items,
    full_page_png_looks_complete,
    is_mostly_blank_image,
    json_callback_body,
    build_readerinfo_url,
    build_public_readerinfo_url,
    can_try_direct_structured_document,
    parse_readerinfo_race_delays,
    merge_structured_html_urls,
    merge_page_image_urls_from_readerinfo,
    normalized_html_urls,
    normalize_acs_token,
    build_docinfo_page_maps,
    docinfo_document_info,
    page_from_docconvert_url,
    page_image_ready,
    page_index_from_font_url,
    page_index_from_resource_url,
    structured_default_font,
    structured_page_needs_image,
    structured_page_needs_font,
    structured_page_resource_needs,
    readerinfo_extra_headers,
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


class ConvertHttpFirstTest(unittest.IsolatedAsyncioTestCase):
    def test_docinfo_type_6_is_treated_as_ppt(self):
        docinfo = {
            "docInfo": {
                "docTitle": "demo",
                "docType": "6",
                "totalPageNum": "32",
            }
        }

        self.assertEqual(wenku_to_pdf.docinfo_document_info(docinfo), ("demo", "ppt", 32))

    def test_docinfo_type_5_is_treated_as_excel(self):
        docinfo = {
            "docInfo": {
                "docTitle": "demo",
                "docType": "5",
                "totalPageNum": "1",
            }
        }

        self.assertEqual(wenku_to_pdf.docinfo_document_info(docinfo), ("demo", "excel", 1))

    async def test_http_pipeline_dispatches_xlsx_to_spreadsheet_handler(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_pdf = Path(temp_dir) / "sheet.pdf"
            document = {
                "file_type": "excel",
                "page_count": 2,
                "title": "sheet",
                "docinfo": {"docInfo": {"docTitle": "sheet"}},
                "data": None,
            }
            calls = {}

            async def fake_spreadsheet_handler(context, document, temp_dir, output_pdf, source_url, progress=None):
                calls["args"] = (document["file_type"], document["page_count"], output_pdf.name, source_url)
                output_pdf.write_bytes(b"%PDF-1.4\n% demo\n")
                return {"mode": "spreadsheet-http", "pages": document["page_count"]}

            original_handler = wenku_to_pdf.process_http_spreadsheet_document
            try:
                wenku_to_pdf.process_http_spreadsheet_document = fake_spreadsheet_handler

                result = await wenku_to_pdf.process_http_document_by_type(
                    object(),
                    document,
                    Path(temp_dir),
                    output_pdf,
                    "https://wenku.baidu.com/view/sheet.html",
                )

                self.assertEqual(result["mode"], "spreadsheet-http")
                self.assertEqual(calls["args"], ("excel", 2, "sheet.pdf", "https://wenku.baidu.com/view/sheet.html"))
            finally:
                wenku_to_pdf.process_http_spreadsheet_document = original_handler

    async def test_http_pipeline_dispatches_ppt_to_page_image_handler(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_pdf = Path(temp_dir) / "demo.pdf"
            document = {
                "file_type": "ppt",
                "page_count": 3,
                "title": "demo",
                "docinfo": {"docInfo": {"docTitle": "demo"}},
                "data": None,
            }
            calls = {}

            async def fake_ppt_handler(context, doc_id, page_count, temp_dir, output_pdf, source_url, progress=None):
                calls["args"] = (doc_id, page_count, output_pdf.name, source_url)
                output_pdf.write_bytes(b"%PDF-1.4\n% demo\n")
                return {"mode": "ppt-page-images-http", "pages": page_count}

            original_handler = wenku_to_pdf.process_http_presentation_document
            try:
                wenku_to_pdf.process_http_presentation_document = fake_ppt_handler

                result = await wenku_to_pdf.process_http_document_by_type(
                    object(),
                    document,
                    Path(temp_dir),
                    output_pdf,
                    "https://wenku.baidu.com/view/demo.html",
                )

                self.assertEqual(result["mode"], "ppt-page-images-http")
                self.assertEqual(calls["args"], ("demo", 3, "demo.pdf", "https://wenku.baidu.com/view/demo.html"))
            finally:
                wenku_to_pdf.process_http_presentation_document = original_handler

    async def test_convert_returns_http_result_without_starting_playwright(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "out"

            async def fake_http_convert(url, cookie_text, temp_dir, output_dir, progress=None):
                output_pdf = Path(output_dir) / "demo.pdf"
                output_pdf.write_bytes(b"%PDF-1.4\n% demo\n")
                return {"output": str(output_pdf), "mode": "http-direct", "pages": 1}

            def fail_if_playwright_starts():
                raise AssertionError("Playwright should not start after HTTP conversion succeeds")

            original_http_convert = wenku_to_pdf.try_convert_http_only
            original_playwright = wenku_to_pdf.async_playwright
            try:
                wenku_to_pdf.try_convert_http_only = fake_http_convert
                wenku_to_pdf.async_playwright = fail_if_playwright_starts

                result = await wenku_to_pdf.convert(
                    "https://wenku.baidu.com/view/demo.html",
                    "BDUSS=demo",
                    output_dir,
                )

                self.assertEqual(result["mode"], "http-direct")
                self.assertEqual(result["pages"], 1)
            finally:
                wenku_to_pdf.try_convert_http_only = original_http_convert
                wenku_to_pdf.async_playwright = original_playwright


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


class ResponseTextDecodeTest(unittest.TestCase):
    def test_decodes_gb18030_resource_when_charset_is_missing(self):
        raw = "第36页结构化资源".encode("gb18030")

        self.assertEqual(decode_response_text(raw, ""), "第36页结构化资源")


class StructuredPageFontRequirementTest(unittest.TestCase):
    def test_pic_only_page_does_not_need_font_resource(self):
        page_data = {"body": [{"t": "pic", "c": {"ix": 0, "iy": 0, "iw": 959, "ih": 1356}}]}

        self.assertFalse(structured_page_needs_font(page_data))
        self.assertTrue(structured_page_needs_image(page_data))

    def test_word_page_needs_font_resource(self):
        page_data = {"body": [{"t": "word", "c": "hello"}]}

        self.assertTrue(structured_page_needs_font(page_data))
        self.assertFalse(structured_page_needs_image(page_data))

    def test_mixed_page_requires_image_and_font(self):
        page_data = {"body": [{"t": "pic", "c": {}}, {"t": "word", "c": "hello"}]}

        self.assertEqual(structured_page_resource_needs(page_data), {"image": True, "font": True})


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

    def test_maps_docinfo_ranges_to_page_numbers(self):
        docinfo = {
            "bcsParam": [
                {"page": "1", "merge": "0-65911", "zoom": "&png=0-31224"},
                {"page": "2", "merge": "65912-144653", "zoom": "&png=31225-70000"},
            ]
        }

        json_ranges, png_ranges = build_docinfo_page_maps(docinfo)

        self.assertEqual(json_ranges[(0, 65911)], 1)
        self.assertEqual(json_ranges[(65912, 144653)], 2)
        self.assertEqual(png_ranges[(31225, 70000)], 2)

    def test_reads_page_index_from_docinfo_range_before_path_number(self):
        json_ranges = {(65912, 144653): 2}
        png_ranges = {(31225, 70000): 2}

        self.assertEqual(
            page_from_docconvert_url(
                "https://wkbjcloudbos.bdimg.com/v1/docconvert475/wk/hash/0.json?x-bce-range=65912-144653",
                json_ranges,
                png_ranges,
            ),
            2,
        )
        self.assertEqual(
            page_from_docconvert_url(
                "https://wkbjcloudbos.bdimg.com/v1/docconvert475/wk/hash/0.png?x-bce-range=31225-70000",
                json_ranges,
                png_ranges,
            ),
            2,
        )

    def test_reads_docinfo_title_type_and_page_count(self):
        title, file_type, page_count = docinfo_document_info(
            {"docInfo": {"docTitle": "投资学", "docType": "1", "totalPageNum": "47"}},
            fallback_title="fallback",
        )

        self.assertEqual(title, "投资学")
        self.assertEqual(file_type, "word")
        self.assertEqual(page_count, 47)

    def test_direct_word_uses_cjk_default_font_to_avoid_bad_embedded_fonts(self):
        self.assertEqual(structured_default_font("word", direct=True), "STSong-Light")
        self.assertIsNone(structured_default_font("word", direct=False))

    def test_unknown_docinfo_type_can_try_direct_reader_endpoint(self):
        document = {"docinfo": {"docInfo": {"docTitle": "x"}}, "file_type": "0", "page_count": 96}

        self.assertTrue(can_try_direct_structured_document(document))

    def test_excel_keeps_dedicated_handling(self):
        document = {"docinfo": {"docInfo": {"docTitle": "x"}}, "file_type": "xlsx", "page_count": 1}

        self.assertFalse(can_try_direct_structured_document(document))

    def test_normalizes_bullet_for_cjk_pdf_font(self):
        self.assertEqual(normalize_text_for_pdf("威廉•F•夏普", default_font="STSong-Light"), "威廉·F·夏普")
        self.assertEqual(normalize_text_for_pdf("威廉•F•夏普", default_font=None), "威廉•F•夏普")

    def test_uses_builtin_font_for_standalone_bullet(self):
        self.assertEqual(choose_pdf_font_for_text("·", default_font="STSong-Light"), "Helvetica")
        self.assertEqual(choose_pdf_font_for_text("正文", default_font="STSong-Light"), "STSong-Light")

    def test_uses_ipa_capable_font_for_phonetic_symbols(self):
        self.assertEqual(choose_pdf_font_for_text("/hə'ləʊ/", default_font="STSong-Light"), "IpaLatin")
        self.assertEqual(choose_pdf_font_for_text("/bi;biː/", default_font="STSong-Light"), "IpaLatin")

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

    def test_merges_public_readerinfo_string_html_urls(self):
        json_urls = {}
        png_urls = {}
        font_urls = {}
        merge_structured_html_urls(
            {
                "status": {"code": 0, "msg": "success"},
                "data": {
                    "oriReaderInfo": {
                        "storeId": "store456",
                        "htmlUrls": (
                            '{"json":[{"pageIndex":1,"pageLoadUrl":"https://example.test/1.json"}],'
                            '"png":[{"pageIndex":1,"pageLoadUrl":"https://example.test/1.png"}],'
                            '"ttf":[{"pageIndex":1,"param":"&md5sum=abc&range=1-2"}]}'
                        ),
                    }
                },
            },
            "doc123",
            json_urls,
            png_urls,
            font_urls,
        )

        self.assertEqual(json_urls[1], "https://example.test/1.json")
        self.assertEqual(png_urls[1], "https://example.test/1.png")
        self.assertIn("/store456?", font_urls[1])

    def test_normalizes_html_urls_from_json_string(self):
        self.assertEqual(normalized_html_urls('{"json": []}'), {"json": []})
        self.assertIsNone(normalized_html_urls("{bad"))

    def test_readerinfo_url_requests_large_page_window(self):
        url = build_readerinfo_url(
            "doc123",
            3,
            200,
            "https://wenku.baidu.com/view/doc123.html?wkQuery=math&chatType=chat",
        )

        self.assertIn("docId=doc123", url)
        self.assertIn("pn=3", url)
        self.assertIn("rn=200", url)
        self.assertIn("powerId=2", url)
        self.assertIn("bizName=mainPc", url)
        self.assertIn("wkQuery=math", url)

    def test_public_readerinfo_url_uses_getdocreader2019(self):
        url = build_public_readerinfo_url(
            "doc123",
            3,
            200,
            "https://wenku.baidu.com/view/doc123.html?wkQuery=math&chatType=chat",
        )

        self.assertIn("/browse/interface/getdocreader2019?", url)
        self.assertIn("doc_id=doc123", url)
        self.assertIn("docId=doc123", url)
        self.assertIn("pn=3", url)
        self.assertIn("rn=200", url)
        self.assertIn("powerId=2", url)
        self.assertIn("bizName=mainPc", url)
        self.assertIn("wkQuery=math", url)

    def test_readerinfo_race_delays_are_staggered_from_zero(self):
        self.assertEqual(parse_readerinfo_race_delays("5,10"), (0.0, 5.0, 10.0))

    def test_readerinfo_race_delays_ignore_bad_and_duplicate_values(self):
        self.assertEqual(parse_readerinfo_race_delays("0,5,bad,5,-1,10"), (0.0, 5.0, 10.0))

    def test_normalizes_acs_token_shapes(self):
        self.assertEqual(normalize_acs_token({"Acs-Token": " abc "}), "abc")
        self.assertEqual(normalize_acs_token({"acs-token": "def"}), "def")
        self.assertEqual(normalize_acs_token("ERR:missing"), "")

    def test_readerinfo_headers_use_frontend_token_name(self):
        headers = readerinfo_extra_headers({"acs_token": "token123"})

        self.assertEqual(headers["Acs-Token"], "token123")
        self.assertIn("application/json", headers["accept"])

    def test_readerinfo_font_urls_prefer_store_id(self):
        json_urls = {}
        png_urls = {}
        font_urls = {}
        merge_structured_html_urls(
            {
                "data": {
                    "storeId": "store123",
                    "htmlUrls": {
                        "ttf": [{"pageIndex": 8, "param": "&md5sum=abc&range=1-2"}],
                    },
                }
            },
            "doc123",
            json_urls,
            png_urls,
            font_urls,
        )

        self.assertIn("/store123?", font_urls[8])

    def test_merges_ppt_readerinfo_image_list_by_url_page_number(self):
        urls_by_page = {1: "https://example.test/1.jpg"}
        merge_page_image_urls_from_readerinfo(
            {
                "data": {
                    "htmlUrls": [
                        "https://wkretype.bdimg.com/retype/zoom/store?pn=21&o=jpg_6",
                        "https://wkretype.bdimg.com/retype/zoom/store?pn=22&o=jpg_6",
                    ]
                }
            },
            urls_by_page,
        )

        self.assertEqual(urls_by_page[21], "https://wkretype.bdimg.com/retype/zoom/store?pn=21&o=jpg_6")
        self.assertEqual(urls_by_page[22], "https://wkretype.bdimg.com/retype/zoom/store?pn=22&o=jpg_6")


class ReaderInfoFetchTest(unittest.IsolatedAsyncioTestCase):
    async def test_public_readerinfo_refetches_prefix_to_cover_missing_tail_pages(self):
        calls = []
        original_fetch_public = wenku_to_pdf.fetch_public_readerinfo_payload
        original_fetch_acs = wenku_to_pdf.fetch_readerinfo_payload

        async def fake_public(context, doc_id, start_page, page_window, source_url):
            calls.append((start_page, page_window))
            return {
                "data": {
                    "storeId": "store123",
                    "htmlUrls": {
                        "json": [
                            {"pageIndex": index, "pageLoadUrl": f"https://example.test/{index}.json"}
                            for index in range(1, page_window + 1)
                        ],
                        "png": [],
                        "ttf": [],
                    },
                }
            }

        async def fake_acs(*args, **kwargs):
            raise AssertionError("ACS fallback should not run when public readerinfo succeeds")

        wenku_to_pdf.fetch_public_readerinfo_payload = fake_public
        wenku_to_pdf.fetch_readerinfo_payload = fake_acs
        try:
            json_urls = {1: "https://example.test/1.json", 2: "https://example.test/2.json"}
            await wenku_to_pdf.fetch_missing_readerinfo_resources(
                context=None,
                doc_id="doc123",
                page_count=12,
                source_url="https://wenku.baidu.com/view/doc123.html",
                json_urls=json_urls,
                png_urls={},
                font_urls={},
                readerinfo_auth={},
            )
        finally:
            wenku_to_pdf.fetch_public_readerinfo_payload = original_fetch_public
            wenku_to_pdf.fetch_readerinfo_payload = original_fetch_acs

        self.assertIn((1, 12), calls)
        self.assertTrue(all(index in json_urls for index in range(1, 13)))


if __name__ == "__main__":
    unittest.main()
