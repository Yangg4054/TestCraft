"""配置持久化测试：数据库优先、环境变量最高、密钥不回显。

回归目标：容器重启后配置丢失（原来只写应用目录下的 config.json）。
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import app as testcraft
import config as tc_config


class ConfigStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_file = tc_config.CONFIG_FILE
        tc_config.CONFIG_FILE = os.path.join(self.temp_dir.name, "config.json")
        tc_config.invalidate_cache()
        testcraft.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        self.client = testcraft.app.test_client()
        self._clear_env()

    def tearDown(self):
        tc_config.CONFIG_FILE = self.original_file
        tc_config.invalidate_cache()
        self._clear_env()
        self.temp_dir.cleanup()

    def _clear_env(self):
        for env_name in tc_config.ENV_OVERRIDES.values():
            os.environ.pop(env_name, None)

    def write_file_config(self, **values):
        merged = {**tc_config.DEFAULT_CONFIG, **values}
        with open(tc_config.CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(merged, f)
        tc_config.invalidate_cache()

    # --- 文件后端（未配置数据库）---------------------------------------

    def test_file_backend_round_trip(self):
        with patch.object(tc_config.db, "is_enabled", return_value=False):
            tc_config.save_config({
                "provider": "anthropic",
                "base_url": "https://api.anthropic.com",
                "api_key": "sk-file-key",
                "model": "claude-sonnet-4-20250514",
            })
            loaded = tc_config.load_config(use_cache=False)
        self.assertEqual(loaded["provider"], "anthropic")
        self.assertEqual(loaded["api_key"], "sk-file-key")
        self.assertTrue(os.path.exists(tc_config.CONFIG_FILE))

    def test_missing_file_falls_back_to_defaults(self):
        with patch.object(tc_config.db, "is_enabled", return_value=False):
            loaded = tc_config.load_config(use_cache=False)
        self.assertEqual(loaded, tc_config.DEFAULT_CONFIG)

    # --- 数据库后端 -----------------------------------------------------

    def test_database_backend_is_preferred_over_file(self):
        self.write_file_config(api_key="sk-stale-file", model="gpt-4o")
        stored = {"api_key": "sk-from-db", "model": "claude-sonnet-4-20250514"}
        with (
            patch.object(tc_config.db, "is_enabled", return_value=True),
            patch.object(tc_config.db, "load_app_config", return_value=stored),
        ):
            loaded = tc_config.load_config(use_cache=False)
        self.assertEqual(loaded["api_key"], "sk-from-db")
        self.assertEqual(loaded["model"], "claude-sonnet-4-20250514")

    def test_save_writes_to_database_when_enabled(self):
        captured = {}
        with (
            patch.object(tc_config.db, "is_enabled", return_value=True),
            patch.object(tc_config.db, "load_app_config", return_value={}),
            patch.object(tc_config.db, "save_app_config", side_effect=lambda values, **kw: captured.update(values)),
        ):
            tc_config.save_config({"api_key": "sk-new", "model": "gpt-4o", "provider": "openai"})
        self.assertEqual(captured["api_key"], "sk-new")
        self.assertEqual(captured["model"], "gpt-4o")
        # 配置进了数据库就不该再写本地文件
        self.assertFalse(os.path.exists(tc_config.CONFIG_FILE))

    def test_database_failure_falls_back_instead_of_crashing(self):
        self.write_file_config(api_key="sk-file-fallback")
        with (
            patch.object(tc_config.db, "is_enabled", return_value=True),
            patch.object(tc_config.db, "load_app_config", side_effect=RuntimeError("connection refused")),
        ):
            loaded = tc_config.load_config(use_cache=False)
        self.assertEqual(loaded["api_key"], "sk-file-fallback")

    def test_empty_database_values_do_not_clobber_defaults(self):
        with (
            patch.object(tc_config.db, "is_enabled", return_value=True),
            patch.object(tc_config.db, "load_app_config", return_value={"model": "", "base_url": ""}),
        ):
            loaded = tc_config.load_config(use_cache=False)
        self.assertEqual(loaded["model"], tc_config.DEFAULT_CONFIG["model"])
        self.assertEqual(loaded["base_url"], tc_config.DEFAULT_CONFIG["base_url"])

    # --- 环境变量优先级 -------------------------------------------------

    def test_environment_variable_wins_and_is_not_persisted(self):
        os.environ["TESTCRAFT_LLM_API_KEY"] = "sk-from-env"
        captured = {}
        with (
            patch.object(tc_config.db, "is_enabled", return_value=True),
            patch.object(tc_config.db, "load_app_config", return_value={"api_key": "sk-from-db"}),
            patch.object(tc_config.db, "save_app_config", side_effect=lambda values, **kw: captured.update(values)),
        ):
            self.assertEqual(tc_config.load_config(use_cache=False)["api_key"], "sk-from-env")
            self.assertEqual(tc_config.env_locked_fields(), ["api_key"])
            tc_config.save_config({"api_key": "试图覆盖", "model": "gpt-4o"})
        self.assertNotIn("api_key", captured, "环境变量注入的密钥不应被页面值写回存储")
        self.assertEqual(captured["model"], "gpt-4o")

    # --- 缓存 -----------------------------------------------------------

    def test_save_invalidates_cache(self):
        with patch.object(tc_config.db, "is_enabled", return_value=False):
            tc_config.save_config({"model": "gpt-4o"})
            self.assertEqual(tc_config.load_config()["model"], "gpt-4o")
            tc_config.save_config({"model": "gpt-4.1"})
            self.assertEqual(tc_config.load_config()["model"], "gpt-4.1")

    # --- 密钥展示 -------------------------------------------------------

    def test_mask_secret(self):
        self.assertEqual(tc_config.mask_secret(""), "")
        self.assertEqual(tc_config.mask_secret("short"), "••••")
        self.assertEqual(tc_config.mask_secret("sk-abcdefghijkl"), "sk-a••••ijkl")

    def test_configure_page_never_echoes_the_secret(self):
        self.write_file_config(api_key="sk-supersecretvalue", feishu_app_secret="feishu-secret-value")
        with patch.object(tc_config.db, "is_enabled", return_value=False):
            html = self.client.get("/configure").data.decode()
        self.assertNotIn("sk-supersecretvalue", html)
        self.assertNotIn("feishu-secret-value", html)
        self.assertIn("sk-s••••alue", html)

    def test_configure_page_reports_storage_backend(self):
        with patch.object(tc_config.db, "is_enabled", return_value=False):
            html = self.client.get("/configure").data.decode()
        self.assertIn("本地文件", html)
        self.assertIn("TESTCRAFT_DATABASE_URL", html)

        with (
            patch.object(tc_config.db, "is_enabled", return_value=True),
            patch.object(tc_config.db, "load_app_config", return_value={}),
        ):
            html = self.client.get("/configure").data.decode()
        self.assertIn("PostgreSQL", html)

    # --- 表单提交语义 ---------------------------------------------------

    def test_blank_secret_field_keeps_existing_value(self):
        self.write_file_config(api_key="sk-keep-me", model="gpt-4o")
        with patch.object(tc_config.db, "is_enabled", return_value=False):
            self.client.post("/configure", data={
                "provider": "openai",
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-4.1",
                "api_key": "",
                "feishu_app_id": "",
                "feishu_app_secret": "",
                "feishu_domain": "https://open.feishu.cn",
            })
            loaded = tc_config.load_config(use_cache=False)
        self.assertEqual(loaded["api_key"], "sk-keep-me", "留空提交不应清掉已有密钥")
        self.assertEqual(loaded["model"], "gpt-4.1")

    def test_submitting_a_new_secret_replaces_it(self):
        self.write_file_config(api_key="sk-old")
        with patch.object(tc_config.db, "is_enabled", return_value=False):
            self.client.post("/configure", data={
                "provider": "openai",
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-4o",
                "api_key": "sk-brand-new",
                "feishu_domain": "https://open.feishu.cn",
            })
            self.assertEqual(tc_config.load_config(use_cache=False)["api_key"], "sk-brand-new")

    def test_save_failure_is_reported_not_swallowed(self):
        with (
            patch.object(tc_config.db, "is_enabled", return_value=True),
            patch.object(tc_config.db, "load_app_config", return_value={}),
            patch.object(tc_config.db, "save_app_config", side_effect=RuntimeError("数据库不可写")),
        ):
            response = self.client.post("/configure", data={"provider": "openai"}, follow_redirects=True)
        self.assertIn("配置保存失败", response.data.decode())


if __name__ == "__main__":
    unittest.main()
