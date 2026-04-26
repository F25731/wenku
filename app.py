import asyncio
import json
import os
import queue
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser

from flask import Flask, jsonify, render_template, request, send_from_directory

from playwright.async_api import async_playwright

from wenku_to_pdf import browser_process_launch_options, convert, convert_with_browser


app = Flask(__name__)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATA_DIR = os.path.abspath(os.environ.get("WENKU_DATA_DIR", BASE_DIR))
DOWNLOAD_DIR = os.path.join(DATA_DIR, "downloads")
COOKIE_FILE = os.path.join(DATA_DIR, "cookie.txt")
COOKIE_POOL_FILE = os.path.join(DATA_DIR, "cookies.json")
TOKEN_DB_FILE = os.path.join(DATA_DIR, "tokens.db")
ADMIN_TOKEN_FILE = os.path.join(DATA_DIR, "admin_token.txt")
MAX_JOB_LOGS = 600
MAX_COOKIE_POOL_SIZE = 10
MAX_QUEUE_WORKERS = 2
DOWNLOAD_TTL_SECONDS = int(os.environ.get("WENKU_DOWNLOAD_TTL_SECONDS", "3600"))
DOWNLOAD_CLEANUP_INTERVAL_SECONDS = int(os.environ.get("WENKU_DOWNLOAD_CLEANUP_INTERVAL_SECONDS", "300"))
BROWSER_RESTART_AFTER_JOBS = int(os.environ.get("WENKU_BROWSER_RESTART_AFTER_JOBS", "20"))
COOKIE_TEST_URL = os.environ.get("WENKU_COOKIE_TEST_URL", "https://wenku.baidu.com/")
CORS_ORIGINS = [
    item.strip()
    for item in os.environ.get("WENKU_CORS_ORIGINS", "null,http://localhost,http://127.0.0.1").split(",")
    if item.strip()
]
APP_HOST = os.environ.get("WENKU_HOST", "127.0.0.1")
APP_PORT = int(os.environ.get("WENKU_PORT", "5000"))
ENABLE_SERVER_ADMIN = os.environ.get("WENKU_ENABLE_SERVER_ADMIN", "1").strip().lower() not in {"0", "false", "no"}

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs = {}
jobs_lock = threading.Lock()
cookie_pool_lock = threading.Lock()
cookie_pool_cursor = 0
token_db_lock = threading.Lock()
job_queue = queue.Queue()
job_capacity_condition = threading.Condition()
job_workers_started = False
active_job_count = 0
waiting_job_count = 0
download_cleaner_started = False


def format_time(timestamp):
    if not timestamp:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


def parse_cookie_pool(cookie_text):
    cookie_text = (cookie_text or "").strip()
    if not cookie_text:
        return []

    blocks = []
    current = []
    for line in cookie_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped == "---":
            if current:
                blocks.append(" ".join(current).strip())
                current = []
            continue
        current.append(stripped)
    if current:
        blocks.append(" ".join(current).strip())

    if len(blocks) == 1:
        non_empty_lines = [line.strip() for line in cookie_text.splitlines() if line.strip() and line.strip() != "---"]
        if len(non_empty_lines) > 1 and all("=" in line for line in non_empty_lines):
            blocks = non_empty_lines

    return [cookie for cookie in blocks if cookie][:MAX_COOKIE_POOL_SIZE]


def read_cookie_pool():
    return [item["cookie"] for item in read_named_cookie_pool()]


def read_cookie_file():
    pool = read_cookie_pool()
    return pool[0] if pool else ""


def legacy_cookie_entries():
    if not os.path.exists(COOKIE_FILE):
        return []
    with open(COOKIE_FILE, "r", encoding="utf-8") as file:
        cookies = parse_cookie_pool(file.read())
    now = time.time()
    return [
        {
            "id": uuid.uuid4().hex,
            "name": f"Cookie {index + 1}",
            "cookie": cookie,
            "created_at": now,
            "updated_at": now,
        }
        for index, cookie in enumerate(cookies)
    ]


def normalize_cookie_entry(entry, index=0):
    cookie = (entry.get("cookie") or "").strip()
    if not cookie:
        return None
    now = time.time()
    return {
        "id": (entry.get("id") or uuid.uuid4().hex).strip(),
        "name": (entry.get("name") or f"Cookie {index + 1}").strip()[:40],
        "cookie": cookie,
        "created_at": float(entry.get("created_at") or now),
        "updated_at": float(entry.get("updated_at") or now),
    }


def read_named_cookie_pool():
    if os.path.exists(COOKIE_POOL_FILE):
        try:
            with open(COOKIE_POOL_FILE, "r", encoding="utf-8") as file:
                raw_entries = json.load(file)
        except (OSError, json.JSONDecodeError):
            raw_entries = []
        entries = []
        for index, entry in enumerate(raw_entries if isinstance(raw_entries, list) else []):
            normalized = normalize_cookie_entry(entry if isinstance(entry, dict) else {}, index)
            if normalized:
                entries.append(normalized)
        return entries[:MAX_COOKIE_POOL_SIZE]

    entries = legacy_cookie_entries()
    if entries:
        write_named_cookie_pool(entries)
    return entries[:MAX_COOKIE_POOL_SIZE]


def sync_legacy_cookie_file(entries):
    with open(COOKIE_FILE, "w", encoding="utf-8") as file:
        if entries:
            file.write("\n".join(item["cookie"] for item in entries) + "\n")


def write_named_cookie_pool(entries):
    normalized_entries = []
    for index, entry in enumerate(entries):
        normalized = normalize_cookie_entry(entry, index)
        if normalized:
            normalized_entries.append(normalized)
        if len(normalized_entries) >= MAX_COOKIE_POOL_SIZE:
            break

    with open(COOKIE_POOL_FILE, "w", encoding="utf-8") as file:
        json.dump(normalized_entries, file, ensure_ascii=False, indent=2)
    sync_legacy_cookie_file(normalized_entries)
    with job_capacity_condition:
        job_capacity_condition.notify_all()
    return normalized_entries


def add_named_cookie(name, cookie):
    entries = read_named_cookie_pool()
    if len(entries) >= MAX_COOKIE_POOL_SIZE:
        raise ValueError(f"最多只能保存 {MAX_COOKIE_POOL_SIZE} 个 Cookie")
    cookie = (cookie or "").strip()
    if not cookie:
        raise ValueError("Cookie 不能为空")
    now = time.time()
    entries.append({
        "id": uuid.uuid4().hex,
        "name": (name or f"Cookie {len(entries) + 1}").strip()[:40],
        "cookie": cookie,
        "created_at": now,
        "updated_at": now,
    })
    return write_named_cookie_pool(entries)[-1]


def update_named_cookie(cookie_id, name=None, cookie=None):
    entries = read_named_cookie_pool()
    for entry in entries:
        if entry["id"] == cookie_id:
            if name is not None:
                entry["name"] = (name or entry["name"]).strip()[:40]
            if cookie is not None:
                cookie = (cookie or "").strip()
                if not cookie:
                    raise ValueError("Cookie 不能为空")
                entry["cookie"] = cookie
            entry["updated_at"] = time.time()
            write_named_cookie_pool(entries)
            return entry
    return None


def delete_named_cookie(cookie_id):
    entries = read_named_cookie_pool()
    next_entries = [entry for entry in entries if entry["id"] != cookie_id]
    if len(next_entries) == len(entries):
        return False
    write_named_cookie_pool(next_entries)
    return True


def mask_secret(value, left=8, right=6):
    value = value or ""
    if len(value) <= left + right + 3:
        return "*" * len(value)
    return f"{value[:left]}...{value[-right:]}"


def cookie_pool_items():
    return [
        {
            "id": item["id"],
            "index": index + 1,
            "name": item["name"],
            "preview": mask_secret(item["cookie"]),
            "length": len(item["cookie"]),
            "updated_at": item.get("updated_at", ""),
            "updated_at_text": format_time(item.get("updated_at")),
        }
        for index, item in enumerate(read_named_cookie_pool())
    ]


def save_cookie_pool(cookie_text):
    cookies = parse_cookie_pool(cookie_text)
    now = time.time()
    entries = [
        {
            "id": uuid.uuid4().hex,
            "name": f"Cookie {index + 1}",
            "cookie": cookie,
            "created_at": now,
            "updated_at": now,
        }
        for index, cookie in enumerate(cookies)
    ]
    return [item["cookie"] for item in write_named_cookie_pool(entries)]


def read_cookie_file_raw():
    if not os.path.exists(COOKIE_FILE):
        return ""
    with open(COOKIE_FILE, "r", encoding="utf-8") as file:
        return file.read()


def test_cookie_connectivity(cookie):
    cookie = (cookie or "").strip()
    if not cookie:
        return {"ok": False, "status": None, "message": "Cookie 为空"}

    request_obj = urllib.request.Request(
        COOKIE_TEST_URL,
        headers={
            "Cookie": cookie,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(request_obj, timeout=8) as response:
            status = getattr(response, "status", response.getcode())
            body = response.read(4096)
        ok = 200 <= status < 400 and (b"wenku" in body.lower() or b"baidu" in body.lower())
        return {
            "ok": ok,
            "status": status,
            "message": "连通正常" if ok else "已返回，但无法确认登录状态",
        }
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "message": f"请求被拒绝：HTTP {exc.code}"}
    except Exception as exc:
        return {"ok": False, "status": None, "message": f"网络检测失败：{exc}"}


def choose_cookie_from_pool():
    global cookie_pool_cursor
    pool = read_cookie_pool()
    if not pool:
        return "", 0, 0
    with cookie_pool_lock:
        index = cookie_pool_cursor % len(pool)
        cookie_pool_cursor += 1
    return pool[index], index + 1, len(pool)


def job_concurrency_limit():
    cookie_count = len(read_cookie_pool())
    if cookie_count <= 1:
        return 1
    return min(cookie_count, MAX_QUEUE_WORKERS)


def acquire_job_slot():
    global active_job_count
    with job_capacity_condition:
        while active_job_count >= job_concurrency_limit():
            job_capacity_condition.wait(timeout=2)
        active_job_count += 1
        return active_job_count, job_concurrency_limit()


def release_job_slot():
    global active_job_count
    with job_capacity_condition:
        active_job_count = max(0, active_job_count - 1)
        job_capacity_condition.notify_all()


def add_waiting_job():
    global waiting_job_count
    with job_capacity_condition:
        waiting_job_count += 1


def remove_waiting_job():
    global waiting_job_count
    with job_capacity_condition:
        waiting_job_count = max(0, waiting_job_count - 1)


def queued_job_count():
    with job_capacity_condition:
        return job_queue.qsize() + waiting_job_count


def download_file_expired(path, now=None):
    if DOWNLOAD_TTL_SECONDS <= 0:
        return False
    try:
        modified_at = os.path.getmtime(path)
    except OSError:
        return False
    return (now or time.time()) - modified_at >= DOWNLOAD_TTL_SECONDS


def cleanup_expired_downloads():
    now = time.time()
    removed = 0
    if not os.path.isdir(DOWNLOAD_DIR):
        return removed
    for name in os.listdir(DOWNLOAD_DIR):
        path = os.path.join(DOWNLOAD_DIR, name)
        if not os.path.isfile(path):
            continue
        if download_file_expired(path, now):
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
    return removed


def download_cleanup_worker():
    while True:
        cleanup_expired_downloads()
        time.sleep(max(30, DOWNLOAD_CLEANUP_INTERVAL_SECONDS))


def start_download_cleaner():
    global download_cleaner_started
    if download_cleaner_started:
        return
    download_cleaner_started = True
    thread = threading.Thread(target=download_cleanup_worker, daemon=True)
    thread.start()


def connect_token_db():
    connection = sqlite3.connect(TOKEN_DB_FILE, timeout=20)
    connection.row_factory = sqlite3.Row
    return connection


def init_token_db():
    with token_db_lock:
        with connect_token_db() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token TEXT NOT NULL UNIQUE,
                    remark TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    usage_count INTEGER NOT NULL DEFAULT 0,
                    last_used_at REAL,
                    last_ip TEXT
                )
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(tokens)").fetchall()}
            if "allow_web" not in columns:
                connection.execute("ALTER TABLE tokens ADD COLUMN allow_web INTEGER NOT NULL DEFAULT 1")
            if "allow_api" not in columns:
                connection.execute("ALTER TABLE tokens ADD COLUMN allow_api INTEGER NOT NULL DEFAULT 1")
            connection.commit()


def token_row_to_dict(row):
    now = time.time()
    expires_at = float(row["expires_at"])
    last_used_at = row["last_used_at"]
    return {
        "id": row["id"],
        "token": row["token"],
        "remark": row["remark"],
        "created_at": row["created_at"],
        "created_at_text": format_time(row["created_at"]),
        "expires_at": expires_at,
        "expires_at_text": format_time(expires_at),
        "enabled": bool(row["enabled"]),
        "allow_web": bool(row["allow_web"]),
        "allow_api": bool(row["allow_api"]),
        "expired": expires_at <= now,
        "usage_count": row["usage_count"],
        "last_used_at": last_used_at,
        "last_used_at_text": format_time(last_used_at),
        "last_ip": row["last_ip"] or "",
    }


def list_access_tokens():
    init_token_db()
    with connect_token_db() as connection:
        rows = connection.execute("SELECT * FROM tokens ORDER BY created_at DESC, id DESC").fetchall()
    return [token_row_to_dict(row) for row in rows]


def create_access_token(days, remark="", token_value=None, allow_web=True, allow_api=True):
    try:
        days = int(days)
    except (TypeError, ValueError):
        raise ValueError("天数必须是整数")
    if days < 1 or days > 3650:
        raise ValueError("天数需要在 1 到 3650 之间")

    init_token_db()
    now = time.time()
    token = (token_value or secrets.token_urlsafe(24)).strip()
    if not token:
        raise ValueError("Token 不能为空")
    if not allow_web and not allow_api:
        raise ValueError("网站使用和接口调用至少选择一个")
    remark = (remark or "").strip()[:80]

    with token_db_lock:
        with connect_token_db() as connection:
            connection.execute(
                """
                INSERT INTO tokens (token, remark, created_at, expires_at, enabled, allow_web, allow_api)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (token, remark, now, now + days * 86400, 1 if allow_web else 0, 1 if allow_api else 0),
            )
            connection.commit()
            row = connection.execute("SELECT * FROM tokens WHERE token = ?", (token,)).fetchone()
    return token_row_to_dict(row)


def verify_access_token(token, touch=False, ip_address="", scope="web"):
    token = (token or "").strip()
    if not token:
        return False, "请先输入使用 Token", None

    init_token_db()
    now = time.time()
    with token_db_lock:
        with connect_token_db() as connection:
            row = connection.execute("SELECT * FROM tokens WHERE token = ?", (token,)).fetchone()
            if not row:
                return False, "Token 不存在或已失效", None
            data = token_row_to_dict(row)
            if not data["enabled"]:
                return False, "Token 已停用", data
            if data["expired"]:
                return False, "Token 已过期", data
            if scope == "web" and not data["allow_web"]:
                return False, "Token 不允许网站使用", data
            if scope == "api" and not data["allow_api"]:
                return False, "Token 不允许接口调用", data
            if touch:
                connection.execute(
                    """
                    UPDATE tokens
                    SET usage_count = usage_count + 1, last_used_at = ?, last_ip = ?
                    WHERE token = ?
                    """,
                    (now, (ip_address or "")[:64], token),
                )
                connection.commit()
                row = connection.execute("SELECT * FROM tokens WHERE token = ?", (token,)).fetchone()
                data = token_row_to_dict(row)
    return True, "Token 可用", data


def set_access_token_enabled(token_id, enabled):
    init_token_db()
    with token_db_lock:
        with connect_token_db() as connection:
            connection.execute("UPDATE tokens SET enabled = ? WHERE id = ?", (1 if enabled else 0, token_id))
            connection.commit()
            row = connection.execute("SELECT * FROM tokens WHERE id = ?", (token_id,)).fetchone()
    return token_row_to_dict(row) if row else None


def delete_access_token(token_id):
    init_token_db()
    with token_db_lock:
        with connect_token_db() as connection:
            cursor = connection.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
            connection.commit()
    return cursor.rowcount > 0


def get_admin_token():
    env_token = os.environ.get("WENKU_ADMIN_TOKEN", "").strip()
    if env_token:
        return env_token
    if os.path.exists(ADMIN_TOKEN_FILE):
        with open(ADMIN_TOKEN_FILE, "r", encoding="utf-8") as file:
            token = file.read().strip()
        if token:
            return token

    token = secrets.token_urlsafe(24)
    with open(ADMIN_TOKEN_FILE, "w", encoding="utf-8") as file:
        file.write(token + "\n")
    return token


def request_admin_token():
    data = request.get_json(silent=True) or {}
    return (
        request.headers.get("X-Admin-Token")
        or data.get("admin_token")
        or request.args.get("admin_token")
        or ""
    ).strip()


def require_admin():
    expected = get_admin_token()
    supplied = request_admin_token()
    return bool(supplied) and secrets.compare_digest(supplied, expected)


def request_access_scope(data=None):
    data = data or {}
    requested = (
        request.headers.get("X-Access-Mode")
        or data.get("scope")
        or data.get("mode")
        or request.args.get("scope")
        or request.args.get("mode")
        or ""
    ).strip().lower()
    return "api" if requested == "api" else "web"


def request_access_token(data=None):
    data = data or {}
    return (
        request.headers.get("X-Access-Token")
        or data.get("token")
        or data.get("api_token")
        or request.args.get("token")
        or request.args.get("api_token")
        or ""
    ).strip()


def cors_origin_allowed(origin):
    if not origin:
        return False
    if "*" in CORS_ORIGINS:
        return True
    if origin in CORS_ORIGINS:
        return True
    for item in CORS_ORIGINS:
        base = item.rstrip("/")
        if base in {"http://localhost", "http://127.0.0.1", "https://localhost", "https://127.0.0.1"}:
            if origin.startswith(base + ":"):
                return True
    return False


@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin")
    if request.path.startswith("/api/admin/") and cors_origin_allowed(origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Admin-Token"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
    return response


def add_job_log(job_id, message, level="info"):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        job["log_seq"] += 1
        job["logs"].append({
            "id": job["log_seq"],
            "time": time.strftime("%H:%M:%S"),
            "level": level,
            "message": message,
        })
        if len(job["logs"]) > MAX_JOB_LOGS:
            job["logs"] = job["logs"][-MAX_JOB_LOGS:]


def update_job(job_id, **values):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(values)


def get_job_snapshot(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return None
        snapshot = dict(job)
        snapshot["logs"] = list(job["logs"])
        return snapshot


class WorkerBrowserRuntime:
    def __init__(self, worker_id, restart_after_jobs):
        self.worker_id = worker_id
        self.restart_after_jobs = max(1, restart_after_jobs)
        self.loop = asyncio.new_event_loop()
        self.playwright = None
        self.browser = None
        self.completed_jobs = 0

    async def ensure_browser(self):
        if self.browser and self.browser.is_connected():
            return self.browser
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(**browser_process_launch_options())
        self.completed_jobs = 0
        return self.browser

    async def restart_browser(self):
        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
        self.browser = None
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass
        self.playwright = None
        self.completed_jobs = 0

    async def convert(self, **kwargs):
        browser = await self.ensure_browser()
        try:
            result = await convert_with_browser(browser=browser, **kwargs)
            self.completed_jobs += 1
            if self.completed_jobs >= self.restart_after_jobs:
                await self.restart_browser()
            return result
        except Exception:
            await self.restart_browser()
            raise

    def run_convert(self, **kwargs):
        asyncio.set_event_loop(self.loop)
        return self.loop.run_until_complete(self.convert(**kwargs))


def run_convert_job(job_id, url, cookie, cookie_slot=None, cookie_total=None, browser_runtime=None):
    started_at = time.perf_counter()
    update_job(job_id, status="running")
    if cookie_slot and cookie_total:
        add_job_log(job_id, f"已分配 Cookie {cookie_slot}/{cookie_total}", "ok")
    add_job_log(job_id, "任务已进入后台队列", "ok")

    def progress(message):
        add_job_log(job_id, message)

    try:
        convert_kwargs = {
            "url": url,
            "cookie_text": cookie,
            "output_dir": DOWNLOAD_DIR,
            "keep_temp": False,
            "scale": 2.0,
            "progress": progress,
        }
        if browser_runtime:
            result = browser_runtime.run_convert(**convert_kwargs)
        else:
            result = asyncio.run(convert(**convert_kwargs))
        filename = os.path.basename(result["output"])
        elapsed = round(time.perf_counter() - started_at, 1)
        payload = {
            "success": True,
            "filename": filename,
            "mode": result.get("mode", "unknown"),
            "pages": result.get("pages", "未知"),
            "seconds": elapsed,
            "message": f"转换完成，共处理 {result.get('pages', '未知')} 页，用时 {elapsed} 秒。",
            "download_url": f"/download/{filename}",
        }
        add_job_log(job_id, payload["message"], "ok")
        add_job_log(job_id, f"文件已保存：{filename}", "ok")
        update_job(job_id, status="done", result=payload, finished_at=time.time())
    except Exception as exc:
        add_job_log(job_id, f"转换失败：{exc}", "error")
        update_job(job_id, status="error", error=str(exc), finished_at=time.time())


def queued_convert_worker(worker_id):
    browser_runtime = WorkerBrowserRuntime(worker_id, BROWSER_RESTART_AFTER_JOBS)
    while True:
        job_id, url, override_cookie = job_queue.get()
        has_waiting_marker = False
        try:
            update_job(job_id, status="queued")
            waiting_count = queued_job_count()
            if waiting_count:
                add_job_log(job_id, f"排队中，前方约 {waiting_count} 个任务", "info")

            add_waiting_job()
            has_waiting_marker = True
            active_count, limit = acquire_job_slot()
            remove_waiting_job()
            has_waiting_marker = False
            try:
                if override_cookie:
                    cookie = override_cookie
                    cookie_slot = None
                    cookie_total = None
                else:
                    cookie, cookie_slot, cookie_total = choose_cookie_from_pool()

                if not cookie:
                    raise RuntimeError("没有找到 Cookie，请先把 Cookie 写入 cookie.txt")

                add_job_log(job_id, f"进入处理通道 {active_count}/{limit}", "ok")
                run_convert_job(job_id, url, cookie, cookie_slot, cookie_total, browser_runtime=browser_runtime)
            finally:
                release_job_slot()
        except Exception as exc:
            add_job_log(job_id, f"转换失败：{exc}", "error")
            update_job(job_id, status="error", error=str(exc), finished_at=time.time())
        finally:
            if has_waiting_marker:
                remove_waiting_job()
            job_queue.task_done()


def start_job_workers():
    global job_workers_started
    if job_workers_started:
        return
    job_workers_started = True
    for index in range(MAX_QUEUE_WORKERS):
        thread = threading.Thread(target=queued_convert_worker, args=(index + 1,), daemon=True)
        thread.start()


@app.route("/")
def index():
    return render_template("index.html", has_cookie=bool(read_cookie_pool()))


@app.route("/admin")
def admin():
    if not ENABLE_SERVER_ADMIN:
        return jsonify({"error": "server admin page disabled"}), 404
    return render_template("admin.html")


@app.route("/api/status")
def api_status():
    cookie_pool = read_cookie_pool()
    with job_capacity_condition:
        running_jobs = active_job_count
    return jsonify({
        "has_cookie": bool(cookie_pool),
        "cookie_count": len(cookie_pool),
        "max_cookie_count": MAX_COOKIE_POOL_SIZE,
        "concurrency_limit": job_concurrency_limit(),
        "running_jobs": running_jobs,
        "queued_jobs": queued_job_count(),
        "download_ttl_seconds": DOWNLOAD_TTL_SECONDS,
        "browser_restart_after_jobs": BROWSER_RESTART_AFTER_JOBS,
        "download_dir": DOWNLOAD_DIR,
        "token_required": True,
    })


@app.route("/api/admin/cookies", methods=["GET", "POST", "PUT"])
def api_admin_cookies():
    if not require_admin():
        return jsonify({"error": "后台口令不正确"}), 401

    if request.method == "GET":
        return jsonify({
            "success": True,
            "raw_text": read_cookie_file_raw(),
            "cookies": cookie_pool_items(),
            "count": len(read_cookie_pool()),
            "max_count": MAX_COOKIE_POOL_SIZE,
            "concurrency_limit": job_concurrency_limit(),
        })

    data = request.get_json(silent=True) or {}
    if request.method == "POST":
        try:
            add_named_cookie(data.get("name", ""), data.get("cookie", ""))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({
            "success": True,
            "message": "已添加 Cookie",
            "cookies": cookie_pool_items(),
            "count": len(read_cookie_pool()),
            "max_count": MAX_COOKIE_POOL_SIZE,
            "concurrency_limit": job_concurrency_limit(),
        }), 201

    cookies = save_cookie_pool(data.get("cookie_text", ""))
    return jsonify({
        "success": True,
        "message": f"已保存 {len(cookies)} 个 Cookie",
        "cookies": cookie_pool_items(),
        "count": len(cookies),
        "max_count": MAX_COOKIE_POOL_SIZE,
        "concurrency_limit": job_concurrency_limit(),
    })


@app.route("/api/admin/cookies/<cookie_id>", methods=["PATCH", "DELETE"])
def api_admin_cookie_detail(cookie_id):
    if not require_admin():
        return jsonify({"error": "后台口令不正确"}), 401

    if request.method == "DELETE":
        if not delete_named_cookie(cookie_id):
            return jsonify({"error": "Cookie 不存在"}), 404
        return jsonify({"success": True})

    data = request.get_json(silent=True) or {}
    try:
        updated = update_named_cookie(cookie_id, data.get("name"), data.get("cookie") if "cookie" in data else None)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not updated:
        return jsonify({"error": "Cookie 不存在"}), 404
    return jsonify({
        "success": True,
        "message": "已更新 Cookie",
        "cookies": cookie_pool_items(),
        "count": len(read_cookie_pool()),
        "max_count": MAX_COOKIE_POOL_SIZE,
        "concurrency_limit": job_concurrency_limit(),
    })


@app.route("/api/admin/cookies/test", methods=["POST"])
def api_admin_cookies_test():
    if not require_admin():
        return jsonify({"error": "后台口令不正确"}), 401

    data = request.get_json(silent=True) or {}
    entries = read_named_cookie_pool()
    cookie = (data.get("cookie") or "").strip()
    index = data.get("index")
    cookie_id = (data.get("id") or "").strip()

    targets = []
    if cookie:
        targets.append((None, "", cookie))
    elif cookie_id:
        matched = next((entry for entry in entries if entry["id"] == cookie_id), None)
        if not matched:
            return jsonify({"error": "Cookie 不存在"}), 404
        targets.append((entries.index(matched) + 1, matched["id"], matched["cookie"]))
    elif index:
        try:
            slot = int(index)
        except (TypeError, ValueError):
            return jsonify({"error": "Cookie 序号不正确"}), 400
        if slot < 1 or slot > len(entries):
            return jsonify({"error": "Cookie 序号不存在"}), 404
        targets.append((slot, entries[slot - 1]["id"], entries[slot - 1]["cookie"]))
    else:
        targets = [(slot + 1, item["id"], item["cookie"]) for slot, item in enumerate(entries)]

    results = []
    for slot, cookie_id, item in targets:
        result = test_cookie_connectivity(item)
        result.update({
            "index": slot,
            "id": cookie_id,
            "preview": mask_secret(item),
            "length": len(item),
        })
        results.append(result)

    return jsonify({"success": True, "results": results})


@app.route("/api/token/verify", methods=["POST"])
def api_token_verify():
    data = request.get_json(silent=True) or {}
    token = request_access_token(data)
    scope = request_access_scope(data)
    ok, message, token_data = verify_access_token(token, scope=scope)
    status = 200 if ok else 403
    return jsonify({"success": ok, "message": message, "token": token_data}), status


@app.route("/api/convert", methods=["POST"])
def api_convert():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    access_token = request_access_token(data)
    access_scope = request_access_scope(data)
    override_cookie = (data.get("cookie") or "").strip()

    if not url:
        return jsonify({"error": "请先填写百度文库链接"}), 400

    token_ok, token_message, _ = verify_access_token(access_token, scope=access_scope)
    if not token_ok:
        return jsonify({"error": token_message}), 403

    if not override_cookie and not read_cookie_pool():
        return jsonify({"error": "没有找到 Cookie，请先把 Cookie 写入 cookie.txt"}), 400

    verify_access_token(access_token, touch=True, ip_address=request.remote_addr or "", scope=access_scope)

    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {
            "id": job_id,
            "status": "queued",
            "created_at": time.time(),
            "finished_at": None,
            "result": None,
            "error": None,
            "logs": [],
            "log_seq": 0,
        }

    add_job_log(job_id, f"收到链接：{url}", "ok")
    add_job_log(job_id, f"任务已排队，当前通道上限 {job_concurrency_limit()} 个", "ok")
    job_queue.put((job_id, url, override_cookie))
    return jsonify({"success": True, "job_id": job_id}), 202


@app.route("/api/job/<job_id>")
def api_job(job_id):
    token = request_access_token()
    scope = request_access_scope()
    token_ok, token_message, _ = verify_access_token(token, scope=scope)
    if not token_ok:
        return jsonify({"error": token_message}), 403
    snapshot = get_job_snapshot(job_id)
    if not snapshot:
        return jsonify({"error": "任务不存在或已过期"}), 404
    return jsonify(snapshot)


@app.route("/api/admin/tokens", methods=["GET", "POST"])
def api_admin_tokens():
    if not require_admin():
        return jsonify({"error": "后台口令不正确"}), 401

    if request.method == "GET":
        return jsonify({"success": True, "tokens": list_access_tokens()})

    data = request.get_json(silent=True) or {}
    try:
        token_data = create_access_token(
            data.get("days", 30),
            data.get("remark", ""),
            allow_web=bool(data.get("allow_web", True)),
            allow_api=bool(data.get("allow_api", True)),
        )
    except sqlite3.IntegrityError:
        return jsonify({"error": "Token 已存在，请重新生成"}), 400
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"success": True, "token": token_data}), 201


@app.route("/api/admin/tokens/<int:token_id>", methods=["PATCH", "DELETE"])
def api_admin_token_detail(token_id):
    if not require_admin():
        return jsonify({"error": "后台口令不正确"}), 401

    if request.method == "DELETE":
        if not delete_access_token(token_id):
            return jsonify({"error": "Token 不存在"}), 404
        return jsonify({"success": True})

    data = request.get_json(silent=True) or {}
    token_data = set_access_token_enabled(token_id, bool(data.get("enabled")))
    if not token_data:
        return jsonify({"error": "Token 不存在"}), 404
    return jsonify({"success": True, "token": token_data})


@app.route("/download/<path:filename>")
def download_file(filename):
    token = request_access_token()
    scope = request_access_scope()
    token_ok, token_message, _ = verify_access_token(token, scope=scope)
    if not token_ok:
        return jsonify({"error": token_message}), 403
    path = os.path.join(DOWNLOAD_DIR, filename)
    if download_file_expired(path):
        try:
            os.remove(path)
        except OSError:
            pass
        return jsonify({"error": "文件已过期，请重新生成"}), 404
    return send_from_directory(DOWNLOAD_DIR, filename, as_attachment=True)


def open_browser():
    webbrowser.open_new(f"http://{APP_HOST}:{APP_PORT}/")


init_token_db()
get_admin_token()
start_job_workers()
start_download_cleaner()


if __name__ == "__main__":
    threading.Timer(1.5, open_browser).start()
    print("正在启动本地 Web 服务，请稍等...")
    print(f"后台口令文件：{ADMIN_TOKEN_FILE}")
    app.run(host=APP_HOST, port=APP_PORT, debug=False, threaded=True)
