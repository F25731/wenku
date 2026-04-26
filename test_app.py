import tempfile
import unittest
from pathlib import Path

import app
from app import MAX_COOKIE_POOL_SIZE, parse_cookie_pool


class CookiePoolTest(unittest.TestCase):
    def test_single_cookie_still_works(self):
        self.assertEqual(parse_cookie_pool("BAIDUID=a; BDUSS=b"), ["BAIDUID=a; BDUSS=b"])

    def test_reads_one_cookie_per_line(self):
        self.assertEqual(
            parse_cookie_pool("BAIDUID=a; BDUSS=1\nBAIDUID=b; BDUSS=2"),
            ["BAIDUID=a; BDUSS=1", "BAIDUID=b; BDUSS=2"],
        )

    def test_reads_blank_line_separated_cookie_blocks(self):
        self.assertEqual(
            parse_cookie_pool("BAIDUID=a;\nBDUSS=1\n\nBAIDUID=b;\nBDUSS=2"),
            ["BAIDUID=a; BDUSS=1", "BAIDUID=b; BDUSS=2"],
        )

    def test_cookie_pool_is_capped_at_ten(self):
        text = "\n".join(f"BAIDUID={index}; BDUSS={index}" for index in range(12))

        self.assertEqual(len(parse_cookie_pool(text)), MAX_COOKIE_POOL_SIZE)

    def test_choose_cookie_round_robins_pool(self):
        old_cookie_file = app.COOKIE_FILE
        old_cookie_pool_file = app.COOKIE_POOL_FILE
        old_cursor = app.cookie_pool_cursor
        with tempfile.TemporaryDirectory() as temp_dir:
            cookie_file = Path(temp_dir) / "cookie.txt"
            cookie_file.write_text("BAIDUID=a; BDUSS=1\nBAIDUID=b; BDUSS=2", encoding="utf-8")
            app.COOKIE_FILE = str(cookie_file)
            app.COOKIE_POOL_FILE = str(Path(temp_dir) / "cookies.json")
            app.cookie_pool_cursor = 0

            self.assertEqual(app.choose_cookie_from_pool(), ("BAIDUID=a; BDUSS=1", 1, 2))
            self.assertEqual(app.choose_cookie_from_pool(), ("BAIDUID=b; BDUSS=2", 2, 2))
            self.assertEqual(app.choose_cookie_from_pool(), ("BAIDUID=a; BDUSS=1", 1, 2))

        app.COOKIE_FILE = old_cookie_file
        app.COOKIE_POOL_FILE = old_cookie_pool_file
        app.cookie_pool_cursor = old_cursor

    def test_concurrency_limit_follows_cookie_count_but_caps_at_two(self):
        old_cookie_file = app.COOKIE_FILE
        old_cookie_pool_file = app.COOKIE_POOL_FILE
        with tempfile.TemporaryDirectory() as temp_dir:
            cookie_file = Path(temp_dir) / "cookie.txt"
            app.COOKIE_FILE = str(cookie_file)
            app.COOKIE_POOL_FILE = str(Path(temp_dir) / "cookies.json")

            app.save_cookie_pool("BAIDUID=a; BDUSS=1")
            self.assertEqual(app.job_concurrency_limit(), 1)

            app.save_cookie_pool("BAIDUID=a; BDUSS=1\nBAIDUID=b; BDUSS=2")
            self.assertEqual(app.job_concurrency_limit(), 2)

            app.save_cookie_pool("BAIDUID=a; BDUSS=1\nBAIDUID=b; BDUSS=2\nBAIDUID=c; BDUSS=3")
            self.assertEqual(app.job_concurrency_limit(), 2)

        app.COOKIE_FILE = old_cookie_file
        app.COOKIE_POOL_FILE = old_cookie_pool_file

    def test_save_cookie_pool_normalizes_and_caps_cookies(self):
        old_cookie_file = app.COOKIE_FILE
        old_cookie_pool_file = app.COOKIE_POOL_FILE
        with tempfile.TemporaryDirectory() as temp_dir:
            cookie_file = Path(temp_dir) / "cookie.txt"
            app.COOKIE_FILE = str(cookie_file)
            app.COOKIE_POOL_FILE = str(Path(temp_dir) / "cookies.json")
            source = "\n".join(f"BAIDUID={index}; BDUSS={index}" for index in range(12))

            cookies = app.save_cookie_pool(source)

            self.assertEqual(len(cookies), MAX_COOKIE_POOL_SIZE)
            self.assertEqual(len(app.read_cookie_pool()), MAX_COOKIE_POOL_SIZE)

        app.COOKIE_FILE = old_cookie_file
        app.COOKIE_POOL_FILE = old_cookie_pool_file


class TokenBackendTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_token_db_file = app.TOKEN_DB_FILE
        self.old_admin_token_file = app.ADMIN_TOKEN_FILE
        self.old_cookie_file = app.COOKIE_FILE
        self.old_cookie_pool_file = app.COOKIE_POOL_FILE
        self.old_download_dir = app.DOWNLOAD_DIR
        self.old_testing = app.app.config.get("TESTING")

        temp_path = Path(self.temp_dir.name)
        app.TOKEN_DB_FILE = str(temp_path / "tokens.db")
        app.ADMIN_TOKEN_FILE = str(temp_path / "admin_token.txt")
        app.COOKIE_FILE = str(temp_path / "cookie.txt")
        app.COOKIE_POOL_FILE = str(temp_path / "cookies.json")
        app.DOWNLOAD_DIR = str(temp_path / "downloads")
        Path(app.DOWNLOAD_DIR).mkdir()
        Path(app.ADMIN_TOKEN_FILE).write_text("admin-secret", encoding="utf-8")
        Path(app.COOKIE_FILE).write_text("BAIDUID=a; BDUSS=b", encoding="utf-8")
        app.app.config["TESTING"] = True
        app.init_token_db()

    def tearDown(self):
        app.TOKEN_DB_FILE = self.old_token_db_file
        app.ADMIN_TOKEN_FILE = self.old_admin_token_file
        app.COOKIE_FILE = self.old_cookie_file
        app.COOKIE_POOL_FILE = self.old_cookie_pool_file
        app.DOWNLOAD_DIR = self.old_download_dir
        app.app.config["TESTING"] = self.old_testing
        self.temp_dir.cleanup()

    def test_create_and_verify_access_token(self):
        token = app.create_access_token(7, "demo")

        ok, message, token_data = app.verify_access_token(token["token"])

        self.assertTrue(ok)
        self.assertEqual(message, "Token 可用")
        self.assertEqual(token_data["remark"], "demo")
        self.assertTrue(token_data["allow_web"])
        self.assertTrue(token_data["allow_api"])

    def test_access_token_scope_permissions_are_enforced(self):
        web_only = app.create_access_token(7, "web", allow_web=True, allow_api=False)
        api_only = app.create_access_token(7, "api", allow_web=False, allow_api=True)

        web_ok, _, _ = app.verify_access_token(web_only["token"], scope="web")
        api_ok, api_message, _ = app.verify_access_token(web_only["token"], scope="api")
        api_token_ok, _, _ = app.verify_access_token(api_only["token"], scope="api")
        web_token_ok, web_message, _ = app.verify_access_token(api_only["token"], scope="web")

        self.assertTrue(web_ok)
        self.assertFalse(api_ok)
        self.assertEqual(api_message, "Token 不允许接口调用")
        self.assertTrue(api_token_ok)
        self.assertFalse(web_token_ok)
        self.assertEqual(web_message, "Token 不允许网站使用")

    def test_expired_access_token_is_rejected(self):
        token = app.create_access_token(1, "expired")
        with app.connect_token_db() as connection:
            connection.execute("UPDATE tokens SET expires_at = ? WHERE token = ?", (1, token["token"]))
            connection.commit()

        ok, message, _ = app.verify_access_token(token["token"])

        self.assertFalse(ok)
        self.assertEqual(message, "Token 已过期")

    def test_convert_requires_access_token(self):
        client = app.app.test_client()

        response = client.post("/api/convert", json={"url": "https://wenku.baidu.com/view/demo.html"})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], "请先输入使用 Token")

    def test_admin_api_creates_access_token(self):
        client = app.app.test_client()

        response = client.post(
            "/api/admin/tokens",
            json={"days": 3, "remark": "class"},
            headers={"X-Admin-Token": "admin-secret"},
        )

        self.assertEqual(response.status_code, 201)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["token"]["remark"], "class")
        self.assertTrue(payload["token"]["allow_web"])
        self.assertTrue(payload["token"]["allow_api"])

        list_response = client.get("/api/admin/tokens", headers={"X-Admin-Token": "admin-secret"})
        self.assertEqual(len(list_response.get_json()["tokens"]), 1)

    def test_admin_api_creates_scoped_access_token(self):
        client = app.app.test_client()

        response = client.post(
            "/api/admin/tokens",
            json={"days": 3, "remark": "api", "allow_web": False, "allow_api": True},
            headers={"X-Admin-Token": "admin-secret"},
        )

        self.assertEqual(response.status_code, 201)
        payload = response.get_json()["token"]
        self.assertFalse(payload["allow_web"])
        self.assertTrue(payload["allow_api"])

    def test_scoped_download_permission(self):
        Path(app.DOWNLOAD_DIR, "scoped.pdf").write_bytes(b"pdf")
        token = app.create_access_token(1, "api", allow_web=False, allow_api=True)
        client = app.app.test_client()

        web_denied = client.get(f"/download/scoped.pdf?token={token['token']}")
        api_allowed = client.get(f"/download/scoped.pdf?token={token['token']}&scope=api")

        self.assertEqual(web_denied.status_code, 403)
        self.assertEqual(api_allowed.status_code, 200)
        api_allowed.close()

    def test_admin_cookie_api_reads_and_saves_pool(self):
        client = app.app.test_client()

        save_response = client.put(
            "/api/admin/cookies",
            json={"cookie_text": "BAIDUID=x; BDUSS=1\nBAIDUID=y; BDUSS=2"},
            headers={"X-Admin-Token": "admin-secret"},
        )
        list_response = client.get("/api/admin/cookies", headers={"X-Admin-Token": "admin-secret"})

        self.assertEqual(save_response.status_code, 200)
        self.assertEqual(save_response.get_json()["count"], 2)
        self.assertEqual(list_response.get_json()["count"], 2)
        self.assertEqual(list_response.get_json()["concurrency_limit"], 2)

    def test_admin_api_allows_local_html_cors_origin(self):
        client = app.app.test_client()

        response = client.options(
            "/api/admin/cookies",
            headers={
                "Origin": "null",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "X-Admin-Token, Content-Type",
            },
        )

        self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), "null")
        self.assertIn("X-Admin-Token", response.headers.get("Access-Control-Allow-Headers", ""))

    def test_admin_cookie_api_adds_renames_and_deletes_card(self):
        client = app.app.test_client()

        add_response = client.post(
            "/api/admin/cookies",
            json={"name": "账号A", "cookie": "BAIDUID=x; BDUSS=1"},
            headers={"X-Admin-Token": "admin-secret"},
        )
        added = next(item for item in add_response.get_json()["cookies"] if item["name"] == "账号A")

        rename_response = client.patch(
            f"/api/admin/cookies/{added['id']}",
            json={"name": "账号B"},
            headers={"X-Admin-Token": "admin-secret"},
        )
        delete_response = client.delete(
            f"/api/admin/cookies/{added['id']}",
            headers={"X-Admin-Token": "admin-secret"},
        )
        list_response = client.get("/api/admin/cookies", headers={"X-Admin-Token": "admin-secret"})

        self.assertEqual(add_response.status_code, 201)
        self.assertTrue(any(item["name"] == "账号B" for item in rename_response.get_json()["cookies"]))
        self.assertEqual(delete_response.status_code, 200)
        self.assertFalse(any(item["name"] == "账号B" for item in list_response.get_json()["cookies"]))

    def test_admin_cookie_test_api_checks_all_cookies(self):
        old_checker = app.test_cookie_connectivity
        app.test_cookie_connectivity = lambda cookie: {"ok": "BDUSS=1" in cookie, "status": 200, "message": "checked"}
        client = app.app.test_client()
        client.put(
            "/api/admin/cookies",
            json={"cookie_text": "BAIDUID=x; BDUSS=1\nBAIDUID=y; BDUSS=2"},
            headers={"X-Admin-Token": "admin-secret"},
        )

        response = client.post(
            "/api/admin/cookies/test",
            json={},
            headers={"X-Admin-Token": "admin-secret"},
        )

        app.test_cookie_connectivity = old_checker
        self.assertEqual(response.status_code, 200)
        results = response.get_json()["results"]
        self.assertEqual(len(results), 2)
        self.assertTrue(results[0]["ok"])
        self.assertFalse(results[1]["ok"])

    def test_download_requires_access_token(self):
        Path(app.DOWNLOAD_DIR, "demo.pdf").write_bytes(b"pdf")
        token = app.create_access_token(1, "download")
        client = app.app.test_client()

        denied = client.get("/download/demo.pdf")
        allowed = client.get(f"/download/demo.pdf?token={token['token']}")

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(allowed.status_code, 200)
        allowed.close()


if __name__ == "__main__":
    unittest.main()
