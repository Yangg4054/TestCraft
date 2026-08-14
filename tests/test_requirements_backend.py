"""需求池门面的后端选择测试（不需要真实 PG，CI 可跑）。

真实 PG 的 CRUD / JSONB 往返由 test_requirements_persistence.py 覆盖；
这里只锁定「DB_ENABLED 时走 db.*，否则走本地文件」这条分支逻辑，
以及整表覆盖语义不被退化成纯 upsert。
"""

import os
import tempfile
import unittest
from unittest.mock import patch

import app as testcraft


REQUIREMENT = {
    "id": "req-1",
    "title": "订单提交",
    "content": "登录用户可以提交订单",
    "priority": "P1",
    "status": "已拆分",
    "source": "手动录入",
    "source_url": "",
    "feature_points": [{"id": "FP-001", "name": "库存校验", "case_points": []}],
    "created_at": "2026-08-14 10:00:00",
    "updated_at": "2026-08-14 10:00:00",
}


class RequirementBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_file = testcraft.REQUIREMENTS_FILE
        self.original_db_enabled = testcraft.DB_ENABLED
        testcraft.REQUIREMENTS_FILE = os.path.join(self.temp_dir.name, "requirements.json")
        testcraft.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        self.client = testcraft.app.test_client()

    def tearDown(self):
        testcraft.REQUIREMENTS_FILE = self.original_file
        testcraft.DB_ENABLED = self.original_db_enabled
        self.temp_dir.cleanup()

    # --- 文件回退（未配置数据库）---------------------------------------

    def test_file_backend_round_trip(self):
        testcraft.DB_ENABLED = False
        testcraft._save_requirements([REQUIREMENT])
        self.assertTrue(os.path.exists(testcraft.REQUIREMENTS_FILE))
        loaded = testcraft._load_requirements()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["feature_points"][0]["name"], "库存校验")

    def test_file_backend_does_not_touch_database(self):
        testcraft.DB_ENABLED = False
        with (
            patch.object(testcraft.db, "sync_requirements") as sync,
            patch.object(testcraft.db, "list_requirements") as listing,
        ):
            testcraft._save_requirements([REQUIREMENT])
            testcraft._load_requirements()
        sync.assert_not_called()
        listing.assert_not_called()

    def test_corrupt_file_degrades_to_empty_pool(self):
        testcraft.DB_ENABLED = False
        with open(testcraft.REQUIREMENTS_FILE, "w", encoding="utf-8") as f:
            f.write("{ 这不是合法 JSON")
        self.assertEqual(testcraft._load_requirements(), [])

    # --- 数据库后端 -----------------------------------------------------

    def test_database_backend_reads_and_writes_through_db(self):
        testcraft.DB_ENABLED = True
        with (
            patch.object(testcraft.db, "sync_requirements") as sync,
            patch.object(testcraft.db, "list_requirements", return_value=[REQUIREMENT]) as listing,
        ):
            testcraft._save_requirements([REQUIREMENT])
            loaded = testcraft._load_requirements()
        sync.assert_called_once_with([REQUIREMENT])
        listing.assert_called_once()
        self.assertEqual(loaded[0]["id"], "req-1")
        # 走数据库时不应再往本地磁盘写状态
        self.assertFalse(os.path.exists(testcraft.REQUIREMENTS_FILE))

    def test_database_backend_reverses_listing_for_history_context(self):
        """db 返回 updated_at 倒序；门面翻正序，保证 [-15:] 取到的是最近的需求。"""
        testcraft.DB_ENABLED = True
        newest = {**REQUIREMENT, "id": "new", "updated_at": "2026-08-14 12:00:00"}
        oldest = {**REQUIREMENT, "id": "old", "updated_at": "2026-08-01 09:00:00"}
        with patch.object(testcraft.db, "list_requirements", return_value=[newest, oldest]):
            loaded = testcraft._load_requirements()
        self.assertEqual([item["id"] for item in loaded], ["old", "new"])

    def test_save_keeps_replace_semantics_not_plain_upsert(self):
        """删除需求依赖整表覆盖：写回的列表就是最终状态。"""
        testcraft.DB_ENABLED = True
        with patch.object(testcraft.db, "sync_requirements") as sync:
            testcraft._save_requirements([])
        sync.assert_called_once_with([])

    def test_find_requirement_uses_the_facade(self):
        testcraft.DB_ENABLED = True
        with patch.object(testcraft.db, "list_requirements", return_value=[REQUIREMENT]):
            requirements, found = testcraft._find_requirement("req-1")
            _, missing = testcraft._find_requirement("nope")
        self.assertEqual(len(requirements), 1)
        self.assertEqual(found["title"], "订单提交")
        self.assertIsNone(missing)

    def test_routes_persist_through_database_backend(self):
        """路由层不感知后端：状态更新在 DB 模式下也只透过门面读写。"""
        testcraft.DB_ENABLED = True
        pool = [dict(REQUIREMENT)]
        with (
            patch.object(testcraft.db, "list_requirements", side_effect=lambda *a, **k: [dict(item) for item in pool]),
            patch.object(testcraft.db, "sync_requirements", side_effect=lambda items: pool.__setitem__(slice(None), items)),
        ):
            response = self.client.post("/api/requirements/req-1/status", json={"status": "已完成"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(pool[0]["status"], "已完成")

            self.assertEqual(self.client.get("/requirements").status_code, 200)
            self.assertEqual(self.client.post("/requirements/req-1/delete").status_code, 302)
            self.assertEqual(pool, [])
        self.assertFalse(os.path.exists(testcraft.REQUIREMENTS_FILE))


if __name__ == "__main__":
    unittest.main()
