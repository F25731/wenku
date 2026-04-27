import argparse
import asyncio
import base64
import getpass
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

import img2pdf
from PIL import Image, ImageDraw
from playwright.async_api import async_playwright

try:
    from PyPDF2 import PdfMerger, PdfReader
except Exception:
    from pypdf import PdfMerger, PdfReader

from structured_json_pdf import save_structured_page_pdf


TOP_MASK_FIRST_PAGE = 0.08
GRAY_WATERMARK_WHITE_THRESHOLD = 185
NEUTRAL_COLOR_TOLERANCE = 35
WATERMARK_REGION_LEFT = 0.45
WATERMARK_REGION_TOP = 0.62
DEFAULT_BROWSER_CHANNEL = os.environ.get("WENKU_BROWSER_CHANNEL", "").strip()
RESOURCE_REQUEST_TIMEOUT_MS = int(os.environ.get("WENKU_RESOURCE_REQUEST_TIMEOUT_MS", "30000"))
RESOURCE_REQUEST_RETRIES = int(os.environ.get("WENKU_RESOURCE_REQUEST_RETRIES", "2"))
STRUCTURED_CAPTURE_ROUNDS = int(os.environ.get("WENKU_STRUCTURED_CAPTURE_ROUNDS", "12"))
STRUCTURED_CAPTURE_DEADLINE_SECONDS = float(os.environ.get("WENKU_STRUCTURED_CAPTURE_DEADLINE_SECONDS", "150"))
DIRECT_STRUCTURED_TYPES = {"word", "doc", "docx", "pdf"}
DOCINFO_TYPE_MAP = {
    "1": "word",
    "2": "excel",
    "3": "ppt",
    "4": "pdf",
}
READER_OVERLAY_HIDE_CSS = """
.tool-bar-wrap,
.toolbar-core-btn,
.share-btn-wrap,
.btns-wrap,
.comp-database-wrap,
#app-reader-editor-below,
.doc-hints-wrap,
.ai-sidebar,
.ai-side,
#app-right,
.resize-stripe {
    display: none !important;
    visibility: hidden !important;
    pointer-events: none !important;
}
"""


try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def emit_progress(progress, message):
    print(message)
    if progress:
        progress(message)


def browser_launch_options(profile_dir, scale):
    options = {
        "user_data_dir": str(profile_dir),
        **browser_context_options(scale),
        **browser_process_launch_options(),
    }
    return options


def browser_process_launch_options():
    options = {
        "headless": True,
        "args": [
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
        ],
    }
    if DEFAULT_BROWSER_CHANNEL:
        options["channel"] = DEFAULT_BROWSER_CHANNEL
    return options


def browser_context_options(scale):
    return {
        "viewport": {"width": 1440, "height": 1800},
        "device_scale_factor": scale,
    }


def parse_cookie_header(cookie_header):
    cookies = []
    for part in cookie_header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if not name:
            continue
        cookies.append(
            {
                "name": name,
                "value": value.strip(),
                "domain": ".baidu.com",
                "path": "/",
                "secure": True,
                "sameSite": "Lax",
            }
        )
    return cookies


def sanitize_filename(name):
    name = re.sub(r"\s+", " ", name or "").strip()
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = name.rstrip(". ")
    return name or "百度文库文档"


def url_with_query_params(url, **params):
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key, value in params.items():
        query[key] = str(value)
    return urlunparse(parsed._replace(query=urlencode(query)))


def find_json_object_after_marker(html, marker):
    pos = html.find(marker)
    if pos < 0:
        return None
    start = html.find("{", pos)
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    quote = ""
    for index in range(start, len(html)):
        ch = html[index]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                in_str = False
            continue
        if ch in ("'", '"'):
            in_str = True
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return html[start : index + 1]
    return None


def extract_page_data(html):
    patterns = [
        r"var\s+pageData\s*=\s*(\{.*?\});",
        r"window\.pageData\s*=\s*(\{.*?\});",
    ]
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.S)
        if match:
            try:
                return json.loads(match.group(1))
            except Exception:
                pass
    raw = find_json_object_after_marker(html, "pageData")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            return None
    return None


def title_from_page_data(data, fallback):
    title = data.get("title") or fallback or "百度文库文档"
    for suffix in (" - 百度文库", "-百度文库"):
        if title.endswith(suffix):
            title = title[: -len(suffix)]
    return sanitize_filename(title)


def reader_info(data):
    reader = data.get("readerInfo") or {}
    doc_info = ((data.get("viewBiz") or {}).get("docInfo") or {})
    page_count = reader.get("page") or doc_info.get("page") or doc_info.get("totalPageNum")
    try:
        page_count = int(page_count)
    except Exception:
        page_count = 0
    file_type = (doc_info.get("fileType") or reader.get("fileType") or "").lower()
    tpl_key = (reader.get("tplKey") or "").lower()
    return reader, doc_info, file_type, tpl_key, page_count


async def safe_wait(page, timeout_ms=15000):
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        await page.wait_for_timeout(5000)


async def click_read_more(page, max_clicks=8):
    clicked = 0
    for _ in range(max_clicks):
        did_click = False
        for label in ("查看剩余全文", "继续阅读"):
            locator = page.get_by_text(label, exact=False)
            try:
                if await locator.count():
                    await locator.first.scroll_into_view_if_needed(timeout=10000)
                    await page.wait_for_timeout(500)
                    await locator.first.click(timeout=10000)
                    clicked += 1
                    did_click = True
                    await page.wait_for_timeout(2500)
                    break
            except Exception:
                pass
        if not did_click:
            break
    return clicked


async def rendered_page_candidate_count(page):
    selectors = [
        '[id^="original-pageNo-"]',
        '[id^="pageNo-"]',
        '[class~="pageNo"]',
        '[class*="reader-page"]',
        '[class*="doc-page"]',
        '[class*="ql-editor-page"]',
        '[class*="canvas-page"]',
        "canvas",
    ]
    counts = []
    for selector in selectors:
        try:
            count = await page.locator(selector).count()
            if count:
                counts.append(count)
        except Exception:
            pass
    return max(counts) if counts else 0


async def exit_editor_mode_if_needed(page, progress=None):
    locator = page.locator("#app-top-right-tool .edit-btn")
    try:
        if not await locator.count():
            return False
        text = (await locator.first.inner_text(timeout=3000)).strip()
    except Exception:
        return False

    if "退出" not in text:
        return False

    emit_progress(progress, "正在切换到标准阅读模式")
    try:
        await locator.first.click(timeout=10000)
    except Exception:
        emit_progress(progress, "阅读模式切换暂未完成，继续优化加载")
        return False

    for _ in range(18):
        await page.wait_for_timeout(1000)
        if await rendered_page_candidate_count(page):
            emit_progress(progress, "标准阅读模式已就绪")
            return True

    emit_progress(progress, "阅读内容仍在加载，继续等待文档就绪")
    return True


async def scroll_to_load(page, rounds=10, pixels=1400, delay_ms=350):
    for _ in range(rounds):
        await page.mouse.wheel(0, pixels)
        await page.wait_for_timeout(delay_ms)


def image_size(path):
    with Image.open(path) as image:
        return image.size


def is_mostly_blank_image(path, white_threshold=245, min_dark_ratio=0.001):
    with Image.open(path).convert("RGB") as image:
        image.thumbnail((320, 320))
        pixels = list(image.getdata())
    if not pixels:
        return True
    dark_pixels = sum(1 for r, g, b in pixels if min(r, g, b) < white_threshold)
    return dark_pixels / len(pixels) < min_dark_ratio


def page_image_ready(path, require_nonblank_pages=False):
    if not Path(path).exists():
        return False
    if not require_nonblank_pages:
        return True
    return not is_mostly_blank_image(path)


def full_page_png_looks_complete(path, min_width=700, min_height=900, min_file_bytes=20000):
    path = Path(path)
    if not path.exists() or path.stat().st_size < min_file_bytes:
        return False
    try:
        width, height = image_size(path)
    except Exception:
        return False
    if width < min_width or height < min_height:
        return False
    return not is_mostly_blank_image(path)


def image_difference_ratio(first_path, second_path):
    with Image.open(first_path).convert("RGB") as first, Image.open(second_path).convert("RGB") as second:
        first.thumbnail((160, 160))
        second.thumbnail((160, 160))
        if first.size != second.size:
            second = second.resize(first.size)
        first_pixels = list(first.getdata())
        second_pixels = list(second.getdata())
    if not first_pixels:
        return 1
    total = 0
    for (r1, g1, b1), (r2, g2, b2) in zip(first_pixels, second_pixels):
        total += abs(r1 - r2) + abs(g1 - g2) + abs(b1 - b2)
    return total / (len(first_pixels) * 3 * 255)


def pdf_range_start(url):
    value = (parse_qs(urlparse(url).query).get("x-bce-range") or [""])[0]
    match = re.match(r"^(\d+)-", value)
    return int(match.group(1)) if match else None


def parse_range_value(value):
    if not value:
        return None
    value = str(value).strip()
    if "-" not in value:
        return None
    start, _, end = value.partition("-")
    try:
        return int(start), int(end) if end else None
    except ValueError:
        return None


def query_range(url):
    value = (parse_qs(urlparse(url).query).get("x-bce-range") or [""])[0]
    return parse_range_value(value)


def zoom_png_range(zoom):
    query = parse_qs(str(zoom or "").lstrip("&"))
    return parse_range_value((query.get("png") or [""])[0])


def build_docinfo_page_maps(docinfo):
    json_range_to_page = {}
    png_range_to_page = {}
    for item in (docinfo or {}).get("bcsParam") or []:
        try:
            page = int(item.get("page") or 0)
        except Exception:
            page = 0
        if not page:
            continue
        merge_range = parse_range_value(item.get("merge"))
        if merge_range:
            json_range_to_page[merge_range] = page
        png_range = zoom_png_range(item.get("zoom"))
        if png_range:
            png_range_to_page[png_range] = page
    return json_range_to_page, png_range_to_page


def page_from_docconvert_url(url, json_range_to_page, png_range_to_page):
    parsed = urlparse(url)
    range_value = query_range(url)
    if parsed.path.lower().endswith(".json"):
        return json_range_to_page.get(range_value)
    if parsed.path.lower().endswith(".png"):
        return png_range_to_page.get(range_value)
    return None


def is_docconvert_png_url(url):
    parsed = urlparse(url)
    return (
        parsed.netloc.endswith("bdimg.com")
        and "docconvert" in parsed.path
        and parsed.path.lower().endswith(".png")
        and pdf_range_start(url) is not None
    )


def excel_page_image_items(urls):
    items_by_range = {}
    for url in urls:
        if not is_docconvert_png_url(url):
            continue
        range_start = pdf_range_start(url)
        if range_start not in items_by_range:
            items_by_range[range_start] = url
    return sorted(items_by_range.items(), key=lambda item: item[0])


def excel_direct_image_looks_complete(path, min_file_bytes=10000):
    path = Path(path)
    if not path.exists() or path.stat().st_size < min_file_bytes:
        return False
    try:
        with Image.open(path) as image:
            width, height = image.size
    except Exception:
        return False
    return width >= 500 and height >= 650


def flatten_image_on_white(path):
    with Image.open(path).convert("RGBA") as image:
        background = Image.new("RGBA", image.size, (255, 255, 255, 255))
        background.alpha_composite(image)
        background.convert("RGB").save(path)


def initial_page_urls_from_data(data):
    reader = data.get("readerInfo") or {}
    html_urls = reader.get("htmlUrls")
    urls_by_page = {}
    if isinstance(html_urls, list):
        for index, value in enumerate(html_urls, start=1):
            if isinstance(value, str):
                urls_by_page[index] = value
    elif isinstance(html_urls, dict):
        for key in ("png", "jpg", "jpeg", "image"):
            for item in html_urls.get(key, []) or []:
                if isinstance(item, dict) and item.get("pageLoadUrl"):
                    try:
                        page_index = int(item.get("pageIndex"))
                    except Exception:
                        continue
                    urls_by_page[page_index] = item["pageLoadUrl"]
    return urls_by_page


async def download_binary(context, url, output_path, referer):
    response = await request_get_with_retry(context, url, referer)
    output_path.write_bytes(await response.body())
    return output_path


def write_pdf_from_images(image_paths, output_pdf):
    with output_pdf.open("wb") as pdf_file:
        pdf_file.write(img2pdf.convert([str(path) for path in image_paths]))


class StructuredResourceNotUsable(RuntimeError):
    pass


def json_callback_body(text):
    match = re.search(r"^[^(]+\((.*)\)\s*;?\s*$", text, flags=re.S)
    return match.group(1) if match else text


def structured_page_needs_font(page_data):
    for item in page_data.get("body") or []:
        if item.get("t") == "word" and str(item.get("c") or ""):
            return True
    return False


def structured_page_needs_image(page_data):
    for item in page_data.get("body") or []:
        if item.get("t") == "pic":
            return True
    return False


def structured_page_resource_needs(page_data):
    return {
        "image": structured_page_needs_image(page_data),
        "font": structured_page_needs_font(page_data),
    }


def decode_response_text(raw, content_type=""):
    charset_match = re.search(r"charset=([^\s;]+)", content_type or "", flags=re.I)
    encodings = []
    if charset_match:
        encodings.append(charset_match.group(1).strip("\"'"))
    encodings.extend(["utf-8-sig", "utf-8", "gb18030", "gbk"])

    tried = set()
    for encoding in encodings:
        normalized = encoding.lower()
        if normalized in tried:
            continue
        tried.add(normalized)
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def page_index_from_resource_url(url):
    match = re.search(r"/(\d+)\.(?:json|png)$", urlparse(url).path, flags=re.I)
    return int(match.group(1)) + 1 if match else None


def page_index_from_font_url(url):
    value = (parse_qs(urlparse(url).query).get("pn") or [""])[0]
    return int(value) if str(value).isdigit() else None


def doc_id_from_data_or_url(data, url):
    reader = data.get("readerInfo") or {}
    doc_id = reader.get("docId") or reader.get("doc_id")
    if doc_id:
        return doc_id
    match = re.search(r"/view/([^/?#]+?)(?:\.html)?(?:[?#]|$)", url)
    return match.group(1) if match else ""


def docinfo_document_info(docinfo, fallback_title="百度文库文档"):
    info = (docinfo or {}).get("docInfo") or {}
    title = sanitize_filename(info.get("docTitle") or (docinfo or {}).get("seoTitle") or fallback_title)
    doc_type = str(info.get("docType") or info.get("showDocType") or "").lower()
    file_type = (info.get("fileType") or DOCINFO_TYPE_MAP.get(doc_type) or doc_type or "").lower()
    try:
        page_count = int(info.get("totalPageNum") or info.get("page") or (docinfo or {}).get("freepagenum") or 0)
    except Exception:
        page_count = 0
    return title, file_type, page_count


def structured_default_font(file_type, direct=False):
    normalized = (file_type or "").lower()
    if normalized in {"excel", "xls", "xlsx"}:
        return "STSong-Light"
    if direct and normalized in {"word", "doc", "docx"}:
        return "STSong-Light"
    return None


def merge_structured_html_urls(payload, doc_id, json_urls, png_urls, font_urls):
    html_urls = None
    if isinstance(payload, dict):
        if isinstance(payload.get("htmlUrls"), dict):
            html_urls = payload["htmlUrls"]
        elif isinstance(payload.get("data"), dict) and isinstance(payload["data"].get("htmlUrls"), dict):
            html_urls = payload["data"]["htmlUrls"]
    if not html_urls:
        return

    for item in html_urls.get("json") or []:
        if item.get("pageLoadUrl") and item.get("pageIndex"):
            json_urls[int(item["pageIndex"])] = item["pageLoadUrl"]
    for item in html_urls.get("png") or []:
        if item.get("pageLoadUrl") and item.get("pageIndex"):
            png_urls[int(item["pageIndex"])] = item["pageLoadUrl"]
    for item in html_urls.get("ttf") or []:
        page_index = int(item.get("pageIndex") or 0)
        if page_index and doc_id:
            font_urls[page_index] = (
                f"https://wkretype.bdimg.com/retype/pipe/{doc_id}"
                f"?pn={page_index}&t=ttf&rn=1&v=6{item.get('param', '')}"
            )


def initial_structured_resource_urls(data, doc_id):
    json_urls = {}
    png_urls = {}
    font_urls = {}
    merge_structured_html_urls(data.get("readerInfo") or {}, doc_id, json_urls, png_urls, font_urls)
    return json_urls, png_urls, font_urls


async def download_text(context, url, referer):
    response = await request_get_with_retry(context, url, referer)
    return decode_response_text(await response.body(), response.headers.get("content-type", ""))


async def download_bytes(context, url, referer):
    response = await request_get_with_retry(context, url, referer)
    return await response.body()


async def request_get_with_retry(context, url, referer, timeout_ms=RESOURCE_REQUEST_TIMEOUT_MS, retries=RESOURCE_REQUEST_RETRIES):
    last_error = None
    for attempt in range(retries + 1):
        try:
            response = await context.request.get(url, headers={"Referer": referer}, timeout=timeout_ms)
            if response.status < 400:
                return response
            last_error = RuntimeError(f"HTTP {response.status}: {url[:120]}")
        except Exception as exc:
            last_error = exc
        if attempt < retries:
            await asyncio.sleep(0.5 * (attempt + 1))
    raise last_error


def clean_docinfo_text(text):
    return re.sub(r"^/\*\*/", "", (text or "").strip())


async def fetch_docinfo(context, doc_id, referer):
    if not doc_id:
        return None
    url = f"https://wenku.baidu.com/api/doc/getdocinfo?callback=cb&doc_id={doc_id}"
    response = await request_get_with_retry(context, url, referer)
    text = decode_response_text(await response.body(), response.headers.get("content-type", ""))
    return json.loads(json_callback_body(clean_docinfo_text(text)))


async def wait_for_pending_response_tasks(pending_response_tasks):
    if pending_response_tasks:
        await asyncio.gather(*list(pending_response_tasks), return_exceptions=True)


async def collect_structured_resources(
    page,
    page_count,
    json_urls,
    png_urls,
    font_urls,
    pending_response_tasks,
    progress=None,
    required_png_pages=None,
    required_font_pages=None,
):
    required_png_pages = set(required_png_pages or [])
    required_font_pages = set(required_font_pages or [])
    started_at = time.monotonic()
    await click_read_more(page, max_clicks=8)
    for round_index in range(1, STRUCTURED_CAPTURE_ROUNDS + 1):
        await wait_for_pending_response_tasks(pending_response_tasks)
        has_json = all(index in json_urls for index in range(1, page_count + 1))
        has_png = all(index in png_urls for index in required_png_pages)
        has_font = all(index in font_urls for index in required_font_pages)
        if has_json and has_png and has_font:
            break
        if time.monotonic() - started_at > STRUCTURED_CAPTURE_DEADLINE_SECONDS:
            raise StructuredResourceNotUsable("文档资源准备超时")
        await scroll_to_load(page, rounds=10, pixels=2000, delay_ms=240)
        await page.wait_for_timeout(900)
        emit_progress(progress, f"资源准备 {round_index}: {len(json_urls)}/{page_count}")


def merge_page_pdfs(page_pdfs, output_pdf):
    merger = PdfMerger()
    try:
        for page_pdf in page_pdfs:
            with Path(page_pdf).open("rb") as handle:
                merger.append(PdfReader(handle))
        with output_pdf.open("wb") as handle:
            merger.write(handle)
    finally:
        merger.close()


def bind_structured_resource_collector(page, doc_id, json_urls, png_urls, font_urls, docinfo=None):
    json_range_to_page, png_range_to_page = build_docinfo_page_maps(docinfo)
    pending_response_tasks = set()

    async def handle_response(response):
        url = response.url
        try:
            if "ndocview/readerinfo" in url:
                merge_structured_html_urls(await response.json(), doc_id, json_urls, png_urls, font_urls)
            elif "docconvert" in url and ".json" in url:
                page_index = page_from_docconvert_url(url, json_range_to_page, png_range_to_page) or page_index_from_resource_url(url)
                if page_index:
                    json_urls.setdefault(page_index, url)
            elif "docconvert" in url and ".png" in url:
                page_index = page_from_docconvert_url(url, json_range_to_page, png_range_to_page) or page_index_from_resource_url(url)
                if page_index:
                    png_urls.setdefault(page_index, url)
            elif "wkretype.bdimg.com/retype/pipe/" in url and "t=ttf" in url:
                page_index = page_index_from_font_url(url)
                if page_index:
                    font_urls.setdefault(page_index, url)
        except Exception:
            pass

    def on_response(response):
        task = asyncio.create_task(handle_response(response))
        pending_response_tasks.add(task)
        task.add_done_callback(pending_response_tasks.discard)

    page.on("response", on_response)
    return pending_response_tasks


async def refresh_structured_page_data(page, source_url, data, file_type, page_count, doc_id, json_urls, png_urls, font_urls):
    if not source_url:
        return data, file_type, page_count, doc_id

    await page.goto(url_with_query_params(source_url, edtMode=2), wait_until="domcontentloaded", timeout=60000)
    await safe_wait(page)
    loaded_data = extract_page_data(await page.content())
    if not loaded_data:
        return data, file_type, page_count, doc_id

    _, _, loaded_file_type, _, loaded_page_count = reader_info(loaded_data)
    loaded_doc_id = doc_id_from_data_or_url(loaded_data, source_url) or doc_id
    initial_json, initial_png, initial_font = initial_structured_resource_urls(loaded_data, loaded_doc_id)
    json_urls.update(initial_json)
    png_urls.update(initial_png)
    font_urls.update(initial_font)
    return loaded_data, loaded_file_type or file_type, loaded_page_count or page_count, loaded_doc_id


async def prepare_structured_json_pages(context, page, page_count, temp_dir, json_urls, default_font):
    required_png_pages = set()
    required_font_pages = set()

    for index in range(1, page_count + 1):
        raw_json = await download_text(context, json_urls[index], page.url)
        page_json = json_callback_body(raw_json)
        (temp_dir / f"{index}.json").write_text(page_json, encoding="utf-8")
        page_data = json.loads(page_json)
        needs = structured_page_resource_needs(page_data)
        if needs["image"]:
            required_png_pages.add(index)
        if needs["font"] and not default_font:
            required_font_pages.add(index)

    return required_png_pages, required_font_pages


async def ensure_structured_assets(
    page,
    page_count,
    json_urls,
    png_urls,
    font_urls,
    pending_response_tasks,
    required_png_pages,
    required_font_pages,
    progress=None,
):
    missing_png = [index for index in sorted(required_png_pages) if index not in png_urls]
    missing_font = [index for index in sorted(required_font_pages) if index not in font_urls]
    if missing_png or missing_font:
        await collect_structured_resources(
            page,
            page_count,
            json_urls,
            png_urls,
            font_urls,
            pending_response_tasks,
            progress=progress,
            required_png_pages=required_png_pages,
            required_font_pages=required_font_pages,
        )
        await wait_for_pending_response_tasks(pending_response_tasks)
        missing_png = [index for index in sorted(required_png_pages) if index not in png_urls]
        missing_font = [index for index in sorted(required_font_pages) if index not in font_urls]

    if missing_png:
        raise StructuredResourceNotUsable(f"结构化图片资源不完整，缺少页面：{missing_png[:10]}")
    if missing_font:
        raise StructuredResourceNotUsable(f"结构化字体资源不完整，缺少页面：{missing_font[:10]}")


async def download_structured_assets(context, page, page_count, temp_dir, png_urls, font_urls, required_png_pages, required_font_pages, progress=None):
    for index in range(1, page_count + 1):
        if index in required_png_pages:
            (temp_dir / f"{index}.png").write_bytes(await download_bytes(context, png_urls[index], page.url))
        if index in required_font_pages:
            font_css = await download_text(context, font_urls[index], page.url)
            (temp_dir / f"{index}.font.css").write_text(font_css, encoding="utf-8")
            for encoded, family in re.findall(
                r"@font-face \{src: url\(data:font/opentype;base64,(.*?)\)format\('truetype'\);font-family: '(.*?)';",
                font_css,
            ):
                (temp_dir / f"{family}.ttf").write_bytes(base64.b64decode(encoded))
        emit_progress(progress, f"处理第 {index}/{page_count} 页 ✅")


def render_structured_pdf(temp_dir, page_count, output_pdf, default_font=None):
    page_pdfs = []
    font_replace = {}
    for index in range(1, page_count + 1):
        font_replace, page_pdf = save_structured_page_pdf(temp_dir, index, font_replace=font_replace, default_font=default_font)
        page_pdfs.append(page_pdf)
    merge_page_pdfs(page_pdfs, output_pdf)


async def process_structured_document(
    context,
    page,
    page_count,
    temp_dir,
    output_pdf,
    data,
    file_type,
    progress=None,
    source_url=None,
    docinfo=None,
    direct=False,
):
    doc_id = doc_id_from_data_or_url(data, source_url or page.url)
    if not doc_id:
        raise StructuredResourceNotUsable("缺少文档 ID，无法进行结构化处理")

    json_urls, png_urls, font_urls = initial_structured_resource_urls(data, doc_id)
    pending_response_tasks = bind_structured_resource_collector(page, doc_id, json_urls, png_urls, font_urls, docinfo=docinfo)
    data, file_type, page_count, doc_id = await refresh_structured_page_data(
        page,
        source_url,
        data,
        file_type,
        page_count,
        doc_id,
        json_urls,
        png_urls,
        font_urls,
    )

    if not page_count:
        raise StructuredResourceNotUsable("缺少页数，无法进行结构化处理")

    emit_progress(progress, "正在准备文档资源")
    await collect_structured_resources(page, page_count, json_urls, png_urls, font_urls, pending_response_tasks, progress=progress)
    await wait_for_pending_response_tasks(pending_response_tasks)

    missing_json = [index for index in range(1, page_count + 1) if index not in json_urls]
    if missing_json:
        raise StructuredResourceNotUsable(f"结构化资源不完整，缺少页面：{missing_json[:10]}")

    default_font = structured_default_font(file_type, direct=direct)
    emit_progress(progress, f"页面资源准备完成，共 {page_count} 页")

    required_png_pages, required_font_pages = await prepare_structured_json_pages(
        context, page, page_count, temp_dir, json_urls, default_font
    )
    await ensure_structured_assets(
        page,
        page_count,
        json_urls,
        png_urls,
        font_urls,
        pending_response_tasks,
        required_png_pages,
        required_font_pages,
        progress=progress,
    )
    await download_structured_assets(
        context,
        page,
        page_count,
        temp_dir,
        png_urls,
        font_urls,
        required_png_pages,
        required_font_pages,
        progress=progress,
    )

    emit_progress(progress, "页面处理完成，正在生成最终文件")
    render_structured_pdf(temp_dir, page_count, output_pdf, default_font=default_font)
    return {"mode": "structured-json-pdf", "pages": page_count}


async def load_structured_page_data(page, url, fallback_data):
    structured_url = url_with_query_params(url, edtMode=2)
    if page.url != structured_url:
        await page.goto(structured_url, wait_until="domcontentloaded", timeout=60000)
        await safe_wait(page)
    return extract_page_data(await page.content()) or fallback_data


async def process_ppt(context, page, page_count, temp_dir, output_pdf, data, progress=None):
    urls_by_page = dict(initial_page_urls_from_data(data))
    emit_progress(progress, "正在准备演示文档页面")

    def collect_from_response(response):
        url = response.url
        if "wkretype.bdimg.com/retype/zoom/" not in url:
            return
        match = re.search(r"[?&]pn=(\d+)", url)
        if match:
            urls_by_page[int(match.group(1))] = url

    page.on("response", collect_from_response)
    await scroll_to_load(page, rounds=8)
    await click_read_more(page, max_clicks=10)
    for _ in range(10):
        before = len(urls_by_page)
        await scroll_to_load(page, rounds=35, pixels=1800)
        await click_read_more(page, max_clicks=1)
        if page_count and len(urls_by_page) >= page_count:
            break
        if len(urls_by_page) <= before:
            break

    if not page_count:
        page_count = max(urls_by_page) if urls_by_page else 0
    missing = [index for index in range(1, page_count + 1) if index not in urls_by_page]
    if missing:
        raise RuntimeError(f"PPT page images incomplete. Missing pages: {missing[:20]}")

    emit_progress(progress, f"页面准备完成，共 {page_count} 页")
    image_paths = []
    for index in range(1, page_count + 1):
        path = temp_dir / f"{index:04d}.jpg"
        await download_binary(context, urls_by_page[index], path, page.url)
        image_paths.append(path)
        emit_progress(progress, f"处理第 {index}/{page_count} 页 ✅")

    emit_progress(progress, "页面处理完成，正在生成最终文件")
    write_pdf_from_images(image_paths, output_pdf)
    return {"mode": "ppt-page-images", "pages": page_count}


class PdfDirectImageNotUsable(RuntimeError):
    pass


async def process_pdf_page_images(context, page, page_count, temp_dir, output_pdf, data, progress=None):
    urls = []
    for _, url in sorted(initial_page_urls_from_data(data).items()):
        if url not in urls:
            urls.append(url)

    def collect_from_response(response):
        url = response.url
        if "wkbjcloudbos.bdimg.com" in url and "docconvert" in url:
            if url not in urls:
                urls.append(url)

    page.on("response", collect_from_response)
    emit_progress(progress, "正在准备文档页面")
    await scroll_to_load(page, rounds=8)
    await click_read_more(page, max_clicks=10)
    for _ in range(8):
        before = len(urls)
        await scroll_to_load(page, rounds=20, pixels=1600)
        await click_read_more(page, max_clicks=1)
        if page_count and len(urls) >= page_count:
            break
        if len(urls) <= before:
            break

    # Keep only real full-page PNG images. Small slices, JSON files, and decorative resources are dropped.
    image_items = []
    emit_progress(progress, "正在校验页面完整性")
    for url_index, url in enumerate(urls, start=1):
        if not urlparse(url).path.lower().endswith(".png"):
            continue
        range_start = pdf_range_start(url)
        if range_start is None:
            continue
        path = temp_dir / f"probe_{url_index:04d}.png"
        try:
            await download_binary(context, url, path, page.url)
        except Exception:
            path.unlink(missing_ok=True)
            continue
        if full_page_png_looks_complete(path):
            image_items.append((range_start, path))
        else:
            path.unlink(missing_ok=True)

    if page_count and len(image_items) != page_count:
        raise PdfDirectImageNotUsable(f"PDF full-page PNG count is {len(image_items)}, expected {page_count}")
    if not image_items:
        raise PdfDirectImageNotUsable("No full-page PDF PNG images found")

    starts = [item[0] for item in image_items]
    if len(starts) != len(set(starts)):
        raise PdfDirectImageNotUsable("PDF full-page PNG ranges contain duplicates")

    image_items.sort(key=lambda item: item[0])
    emit_progress(progress, f"页面校验完成，共 {len(image_items)} 页")

    final_paths = []
    for index, (_, path) in enumerate(image_items, start=1):
        final_path = temp_dir / f"{index:04d}.png"
        if path != final_path:
            path.replace(final_path)
        final_paths.append(final_path)
        emit_progress(progress, f"处理第 {index}/{len(image_items)} 页 ✅")

    emit_progress(progress, "页面处理完成，正在生成最终文件")
    write_pdf_from_images(final_paths, output_pdf)
    return {"mode": "pdf-full-page-png", "pages": len(final_paths)}


class ExcelDirectImageNotUsable(RuntimeError):
    pass


async def process_excel_page_images(context, page, page_count, temp_dir, output_pdf, data, progress=None):
    urls = []
    for _, url in sorted(initial_page_urls_from_data(data).items()):
        if url not in urls:
            urls.append(url)

    def collect_from_response(response):
        url = response.url
        if is_docconvert_png_url(url) and url not in urls:
            urls.append(url)

    page.on("response", collect_from_response)
    emit_progress(progress, "正在准备表格文档页面")
    await page.mouse.move(720, 900)
    await scroll_to_load(page, rounds=8)
    await click_read_more(page, max_clicks=10)
    for _ in range(12):
        before = len(excel_page_image_items(urls))
        await page.mouse.move(720, 900)
        await scroll_to_load(page, rounds=25, pixels=1800)
        await click_read_more(page, max_clicks=1)
        current = len(excel_page_image_items(urls))
        if page_count and current >= page_count:
            break
        if current <= before:
            await page.wait_for_timeout(1000)

    image_items = excel_page_image_items(urls)
    if page_count and len(image_items) != page_count:
        raise ExcelDirectImageNotUsable(f"表格页面资源数量为 {len(image_items)}，预期 {page_count}")
    if not image_items:
        raise ExcelDirectImageNotUsable("未找到表格页面资源")

    first_probe = temp_dir / "excel_direct_probe.png"
    await download_binary(context, image_items[0][1], first_probe, page.url)
    if not excel_direct_image_looks_complete(first_probe):
        first_probe.unlink(missing_ok=True)
        raise ExcelDirectImageNotUsable("表格页面资源不完整")
    flatten_image_on_white(first_probe)

    emit_progress(progress, f"页面校验完成，共 {len(image_items)} 页")
    image_paths = []
    for index, (_, url) in enumerate(image_items, start=1):
        path = temp_dir / f"{index:04d}.png"
        if index == 1:
            first_probe.replace(path)
        else:
            await download_binary(context, url, path, page.url)
            flatten_image_on_white(path)
        image_paths.append(path)
        emit_progress(progress, f"处理第 {index}/{len(image_items)} 页 ✅")

    emit_progress(progress, "页面处理完成，正在生成最终文件")
    write_pdf_from_images(image_paths, output_pdf)
    return {"mode": "excel-page-images", "pages": len(image_paths)}


def mask_html_image(path, index):
    with Image.open(path).convert("RGB") as image:
        width, height = image.size
        top_blank = round(height * TOP_MASK_FIRST_PAGE) if index == 1 else 0
        draw = ImageDraw.Draw(image)
        if top_blank:
            draw.rectangle((0, 0, width, top_blank), fill="white")
        image.save(path)


def clean_gray_watermark(path):
    with Image.open(path).convert("RGB") as image:
        pixels = image.load()
        width, height = image.size
        start_x = round(width * WATERMARK_REGION_LEFT)
        start_y = round(height * WATERMARK_REGION_TOP)
        for y in range(start_y, height):
            for x in range(start_x, width):
                r, g, b = pixels[x, y]
                if max(r, g, b) - min(r, g, b) > NEUTRAL_COLOR_TOLERANCE:
                    continue
                gray = (r + g + b) // 3
                if gray >= GRAY_WATERMARK_WHITE_THRESHOLD:
                    pixels[x, y] = (255, 255, 255)
        image.save(path)


async def usable_page_locator(page, index, page_count):
    direct_selectors = [
        f"#original-pageNo-{index}",
        f"#pageNo-{index}",
        f"#reader-page-{index}",
        f"[data-page-no='{index}']",
        f"[data-page='{index}']",
        f"[data-page-num='{index}']",
    ]
    for selector in direct_selectors:
        locator = page.locator(selector)
        if await locator.count() > 0:
            return locator.first

    grouped_selectors = [
        '[id^="original-pageNo-"]',
        '[id^="pageNo-"]',
        '[class~="pageNo"]',
        '[class*="pageNo"]',
        '[class*="reader-page"]',
        '[class*="doc-page"]',
        '[class*="ql-editor-page"]',
        '[class*="canvas-page"]',
        '[class*="paper"]',
        "canvas",
    ]
    for selector in grouped_selectors:
        group = page.locator(selector)
        count = await group.count()
        if count < index:
            continue
        if page_count and count not in {page_count, page_count + 1} and count > page_count * 3:
            continue
        candidate = group.nth(index - 1)
        try:
            if not await candidate.is_visible(timeout=1000):
                continue
            box = await candidate.bounding_box(timeout=3000)
        except Exception:
            continue
        if not box:
            continue
        if box["width"] >= 500 and box["height"] >= 650:
            return candidate
    return None


async def infer_rendered_page_count(page, fallback):
    if fallback:
        return fallback
    selectors = [
        '[id^="original-pageNo-"]',
        '[id^="pageNo-"]',
        '[class~="pageNo"]',
        '[class*="reader-page"]',
        '[class*="doc-page"]',
        '[class*="ql-editor-page"]',
        '[class*="canvas-page"]',
        "canvas",
    ]
    counts = []
    for selector in selectors:
        try:
            count = await page.locator(selector).count()
            if count:
                counts.append(count)
        except Exception:
            pass
    return max(counts) if counts else 0


async def debug_candidate_counts(page):
    selectors = [
        '[id^="original-pageNo-"]',
        '[id^="pageNo-"]',
        '[class~="pageNo"]',
        '[class*="pageNo"]',
        '[class*="reader-page"]',
        '[class*="doc-page"]',
        '[class*="ql-editor-page"]',
        '[class*="canvas-page"]',
        '[class*="paper"]',
        '[class*="page"]',
        '[class*="reader"]',
        "canvas",
        "iframe",
    ]
    counts = {}
    for selector in selectors:
        try:
            counts[selector] = await page.locator(selector).count()
        except Exception as exc:
            counts[selector] = f"error: {exc}"
    return counts


async def hide_reader_overlays(page):
    try:
        await page.add_style_tag(content=READER_OVERLAY_HIDE_CSS)
    except Exception:
        pass
    try:
        await page.evaluate(
            """() => {
                const shouldHideText = /分享|批量下载|单篇下载|下载客户端|90%人选择|AI帮你创作|版权说明/;
                for (const element of document.querySelectorAll('*')) {
                    const rect = element.getBoundingClientRect();
                    if (rect.width < 80 || rect.height < 20) continue;
                    const text = (element.innerText || element.textContent || '').replace(/\\s+/g, ' ').trim();
                    const className = String(element.className || '');
                    const isKnownToolbar = /tool-bar|toolbar|share-btn|database-btn|doc-hints|pcStepView|ai-side|ai-sidebar/.test(className);
                    const isBottomAction = rect.bottom > window.innerHeight - 4 && rect.height <= 140 && shouldHideText.test(text);
                    if (isKnownToolbar || isBottomAction) {
                        element.style.setProperty('display', 'none', 'important');
                        element.style.setProperty('visibility', 'hidden', 'important');
                        element.style.setProperty('pointer-events', 'none', 'important');
                    }
                }
            }"""
        )
    except Exception:
        pass


async def save_processed_locator(locator, path, index, clean_watermark):
    await locator.screenshot(path=str(path), timeout=30000)
    mask_html_image(path, index)
    if clean_watermark:
        clean_gray_watermark(path)


async def ensure_rendered_pages_loaded(page, page_count):
    if not page_count:
        return
    for _ in range(10):
        if await rendered_page_candidate_count(page) >= page_count:
            return
        await page.mouse.move(720, 900)
        await scroll_to_load(page, rounds=12, pixels=1800)
        await click_read_more(page, max_clicks=1)
        await page.wait_for_timeout(800)


async def process_html_screenshots(
    page,
    page_count,
    temp_dir,
    output_pdf,
    clean_watermark=True,
    progress=None,
    require_nonblank_pages=False,
    hide_overlays=False,
):
    emit_progress(progress, "正在加载完整文档内容")
    if require_nonblank_pages:
        await page.mouse.move(720, 900)
    await scroll_to_load(page, rounds=8)
    await click_read_more(page, max_clicks=10)
    if require_nonblank_pages:
        await page.mouse.move(720, 900)
    await scroll_to_load(page, rounds=12)
    if require_nonblank_pages:
        await ensure_rendered_pages_loaded(page, page_count)
    if hide_overlays:
        await hide_reader_overlays(page)

    page_count = await infer_rendered_page_count(page, page_count)
    if not page_count:
        raise RuntimeError("Cannot infer rendered page count")

    emit_progress(progress, f"页面识别完成，共 {page_count} 页")
    image_paths = []
    for index in range(1, page_count + 1):
        locator = await usable_page_locator(page, index, page_count)
        if locator is None and require_nonblank_pages:
            for _ in range(6):
                await page.mouse.move(720, 900)
                await scroll_to_load(page, rounds=8, pixels=1800)
                locator = await usable_page_locator(page, index, page_count)
                if locator is not None:
                    if hide_overlays:
                        await hide_reader_overlays(page)
                    break
        if locator is None:
            (temp_dir / "debug_page.html").write_text(await page.content(), encoding="utf-8")
            (temp_dir / "debug_counts.json").write_text(
                json.dumps(await debug_candidate_counts(page), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            try:
                await page.screenshot(path=str(temp_dir / "debug_full_page.png"), full_page=True, timeout=30000)
            except Exception:
                pass
            raise RuntimeError(f"Cannot find rendered page container {index}")
        await locator.scroll_into_view_if_needed(timeout=10000)
        if hide_overlays:
            await hide_reader_overlays(page)
        await page.wait_for_timeout(1200 if require_nonblank_pages else 500)
        path = temp_dir / f"{index:04d}.png"
        page_ready = False
        for attempt in range(1, 6):
            if hide_overlays:
                await hide_reader_overlays(page)
            await save_processed_locator(locator, path, index, clean_watermark)
            if page_image_ready(path, require_nonblank_pages):
                if require_nonblank_pages:
                    stable_path = temp_dir / f"{index:04d}_stable.png"
                    await page.wait_for_timeout(900)
                    if hide_overlays:
                        await hide_reader_overlays(page)
                    await save_processed_locator(locator, stable_path, index, clean_watermark)
                    stable_ready = page_image_ready(stable_path, require_nonblank_pages)
                    stable_enough = stable_ready and image_difference_ratio(path, stable_path) <= 0.006
                    stable_path.replace(path)
                    if stable_enough:
                        page_ready = True
                        break
                else:
                    page_ready = True
                    break
            elif require_nonblank_pages and attempt >= 3:
                stable_path = temp_dir / f"{index:04d}_stable.png"
                await page.wait_for_timeout(900)
                if hide_overlays:
                    await hide_reader_overlays(page)
                await save_processed_locator(locator, stable_path, index, clean_watermark)
                stable_enough = image_difference_ratio(path, stable_path) <= 0.002
                stable_path.replace(path)
                if stable_enough:
                    page_ready = True
                    break
            if attempt < 5:
                path.unlink(missing_ok=True)
                emit_progress(progress, f"第 {index}/{page_count} 页仍在加载，正在重新处理")
            await locator.scroll_into_view_if_needed(timeout=10000)
            await page.wait_for_timeout(1200 * attempt)
        if not page_ready:
            raise RuntimeError(f"第 {index} 页内容未稳定生成，请重试")
        image_paths.append(path)
        emit_progress(progress, f"处理第 {index}/{page_count} 页 ✅")

    emit_progress(progress, "页面处理完成，正在生成最终文件")
    write_pdf_from_images(image_paths, output_pdf)
    return {"mode": "html-render-screenshot-masked", "pages": page_count}


async def load_page_data(page, url):
    await page.goto(url_with_query_params(url, edtMode=2), wait_until="domcontentloaded", timeout=60000)
    await safe_wait(page)
    data = extract_page_data(await page.content())
    if not data:
        raise RuntimeError("Cannot find pageData in document page")
    return data


async def read_document_metadata(browser_context, page, url):
    data = None
    direct_docinfo = None
    tpl_key = ""
    doc_id = doc_id_from_data_or_url({}, url)

    if doc_id:
        try:
            direct_docinfo = await fetch_docinfo(browser_context, doc_id, url)
        except Exception:
            direct_docinfo = None

    if direct_docinfo:
        title, file_type, page_count = docinfo_document_info(direct_docinfo)
    else:
        data = await load_page_data(page, url)
        title = title_from_page_data(data, await page.title())
        _, _, file_type, tpl_key, page_count = reader_info(data)

    return {
        "data": data,
        "docinfo": direct_docinfo,
        "title": title,
        "file_type": file_type,
        "tpl_key": tpl_key,
        "page_count": page_count,
    }


async def ensure_rendered_document_data(page, url, document):
    if document["data"] is None:
        data = await load_page_data(page, url)
        _, _, file_type, tpl_key, page_count = reader_info(data)
        document.update(
            {
                "data": data,
                "file_type": file_type or document["file_type"],
                "tpl_key": tpl_key,
                "page_count": page_count or document["page_count"],
            }
        )
    return document


async def try_direct_structured_document(browser_context, page, temp_dir, output_pdf, document, url, progress=None):
    if not document["docinfo"] or document["file_type"] not in DIRECT_STRUCTURED_TYPES:
        return None

    try:
        return await process_structured_document(
            browser_context,
            page,
            document["page_count"],
            temp_dir,
            output_pdf,
            {},
            document["file_type"],
            progress=progress,
            source_url=url,
            docinfo=document["docinfo"],
            direct=True,
        )
    except StructuredResourceNotUsable:
        emit_progress(progress, "正在切换备用处理方案")
        return None


async def fallback_html_screenshots(
    page,
    url,
    page_count,
    temp_dir,
    output_pdf,
    progress=None,
    require_nonblank_pages=False,
    hide_overlays=False,
):
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await safe_wait(page)
    await exit_editor_mode_if_needed(page, progress=progress)
    return await process_html_screenshots(
        page,
        page_count,
        temp_dir,
        output_pdf,
        clean_watermark=True,
        progress=progress,
        require_nonblank_pages=require_nonblank_pages,
        hide_overlays=hide_overlays,
    )


async def process_structured_from_document(browser_context, page, temp_dir, output_pdf, document, url, progress=None):
    return await process_structured_document(
        browser_context,
        page,
        document["page_count"],
        temp_dir,
        output_pdf,
        document["data"],
        document["file_type"],
        progress=progress,
        source_url=url,
        docinfo=document["docinfo"],
    )


async def process_document_by_type(browser_context, page, temp_dir, output_pdf, document, url, progress=None):
    file_type = document["file_type"]
    tpl_key = document["tpl_key"]
    page_count = document["page_count"]
    data = document["data"]

    if file_type in {"ppt", "pptx"} or tpl_key == "new_view":
        return await process_ppt(browser_context, page, page_count, temp_dir, output_pdf, data, progress=progress)

    if file_type == "pdf":
        try:
            return await process_structured_from_document(browser_context, page, temp_dir, output_pdf, document, url, progress=progress)
        except StructuredResourceNotUsable:
            emit_progress(progress, "正在切换备用处理方案")
            try:
                return await process_pdf_page_images(browser_context, page, page_count, temp_dir, output_pdf, data, progress=progress)
            except PdfDirectImageNotUsable:
                return await fallback_html_screenshots(
                    page, url, page_count, temp_dir, output_pdf, progress=progress, require_nonblank_pages=True, hide_overlays=True
                )

    if file_type in {"excel", "xls", "xlsx"}:
        try:
            return await process_structured_from_document(browser_context, page, temp_dir, output_pdf, document, url, progress=progress)
        except StructuredResourceNotUsable:
            emit_progress(progress, "正在切换备用处理方案")
            try:
                return await process_excel_page_images(browser_context, page, page_count, temp_dir, output_pdf, data, progress=progress)
            except ExcelDirectImageNotUsable:
                return await fallback_html_screenshots(
                    page, url, page_count, temp_dir, output_pdf, progress=progress, require_nonblank_pages=True, hide_overlays=True
                )

    if file_type in {"word", "doc", "docx"}:
        try:
            return await process_structured_from_document(browser_context, page, temp_dir, output_pdf, document, url, progress=progress)
        except StructuredResourceNotUsable:
            emit_progress(progress, "正在切换备用处理方案")
            return await fallback_html_screenshots(page, url, page_count, temp_dir, output_pdf, progress=progress)

    await exit_editor_mode_if_needed(page, progress=progress)
    return await process_html_screenshots(page, page_count, temp_dir, output_pdf, clean_watermark=True, progress=progress)


async def convert_in_context(browser_context, url, cookie_text, temp_dir, output_dir, progress=None):
    page = browser_context.pages[0] if browser_context.pages else await browser_context.new_page()
    await browser_context.add_cookies(parse_cookie_header(cookie_text))

    emit_progress(progress, "正在进入文档空间")
    emit_progress(progress, "正在读取文档信息")
    document = await read_document_metadata(browser_context, page, url)
    output_pdf = output_dir / f"{document['title']}.pdf"

    emit_progress(progress, f"文档名称：{document['title']}")
    emit_progress(progress, f"文档页数：{document['page_count'] or 'unknown'}")
    emit_progress(progress, "已选择最佳处理方案")

    result = await try_direct_structured_document(browser_context, page, temp_dir, output_pdf, document, url, progress=progress)
    if result is None:
        document = await ensure_rendered_document_data(page, url, document)
        result = await process_document_by_type(browser_context, page, temp_dir, output_pdf, document, url, progress=progress)

    emit_progress(progress, f"最终文件已生成：{output_pdf.name}")
    return {"output": str(output_pdf), **result}


async def convert_with_browser(browser, url, cookie_text, output_dir, temp_root=None, keep_temp=False, scale=2.0, progress=None):
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    temp_parent = Path(temp_root).resolve() if temp_root else None
    temp_dir = Path(tempfile.mkdtemp(prefix="wenku_to_pdf_", dir=str(temp_parent) if temp_parent else None))
    browser_context = None
    try:
        browser_context = await browser.new_context(**browser_context_options(scale))
        return await convert_in_context(browser_context, url, cookie_text, temp_dir, output_dir, progress=progress)
    finally:
        if browser_context:
            try:
                await browser_context.close()
            except Exception:
                pass
        if keep_temp:
            emit_progress(progress, f"保留诊断目录：{temp_dir}")
        else:
            shutil.rmtree(temp_dir, ignore_errors=True)
            emit_progress(progress, "运行环境已整理完成")


async def convert(url, cookie_text, output_dir, temp_root=None, keep_temp=False, scale=2.0, progress=None):
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    temp_parent = Path(temp_root).resolve() if temp_root else None
    temp_dir = Path(tempfile.mkdtemp(prefix="wenku_to_pdf_", dir=str(temp_parent) if temp_parent else None))
    profile_dir = Path(tempfile.mkdtemp(prefix="wenku_chrome_profile_", dir=str(temp_parent) if temp_parent else None))

    try:
        async with async_playwright() as p:
            browser_context = await p.chromium.launch_persistent_context(**browser_launch_options(profile_dir, scale))
            try:
                return await convert_in_context(browser_context, url, cookie_text, temp_dir, output_dir, progress=progress)
            finally:
                await browser_context.close()
    finally:
        if keep_temp:
            emit_progress(progress, f"保留诊断目录：{temp_dir}")
            emit_progress(progress, f"保留运行目录：{profile_dir}")
        else:
            shutil.rmtree(temp_dir, ignore_errors=True)
            shutil.rmtree(profile_dir, ignore_errors=True)
            emit_progress(progress, "运行环境已整理完成")


def read_cookie(args):
    if args.cookie:
        return args.cookie.strip()
    if args.cookie_file:
        return Path(args.cookie_file).read_text(encoding="utf-8").strip()
    return getpass.getpass("Paste Baidu Cookie: ").strip()


def main():
    parser = argparse.ArgumentParser(description="Convert Baidu Wenku preview pages to a PDF.")
    parser.add_argument("url", help="Baidu Wenku document URL")
    parser.add_argument("-c", "--cookie", help="Baidu Cookie string")
    parser.add_argument("-C", "--cookie-file", help="Text file containing Baidu Cookie")
    parser.add_argument("-o", "--output-dir", default=".", help="Output directory, default: current directory")
    parser.add_argument("--keep-temp", action="store_true", help="Keep temporary images/profile for debugging")
    parser.add_argument("--scale", type=float, default=2.0, help="Screenshot scale for Word/Excel pages, default: 2")
    args = parser.parse_args()

    if "wenku.baidu.com" not in urlparse(args.url).netloc:
        raise SystemExit("Only wenku.baidu.com URLs are supported.")

    cookie_text = read_cookie(args)
    if not cookie_text:
        raise SystemExit("Cookie is empty.")

    started = time.time()
    result = asyncio.run(convert(args.url, cookie_text, args.output_dir, keep_temp=args.keep_temp, scale=args.scale))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"done in {time.time() - started:.1f}s")


if __name__ == "__main__":
    main()
