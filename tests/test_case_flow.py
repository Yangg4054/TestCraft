"""需求池 CRUD、Case 点、用例集的端到端行为测试。

覆盖三步流程：需求拆分 -> 生成 Case 点 -> 生成测试用例，以及用例集资产管理。
"""

import json
import os
import tempfile
import unittest
from io import BytesIO
from unittest.mock import patch

import app as testcraft
import case_sets
from services.ai_generator import _parse_case_points


def make_case(index=1, module="订单", name=None):
    return {
        "id": f"TC-{index:03d}",
        "module": module,
        "name": name or f"用例 {index}",
        "priority": "P1",
        "preconditions": "已登录",
        "steps": "执行操作",
        "expected_result": "结果正确",
        "type": "Functional",
        "method": "场景法",
    }


class BaseFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self._originals = {
            "OUTPUT_DIR": testcraft.OUTPUT_DIR,
            "REQUIREMENTS_FILE": testcraft.REQUIREMENTS_FILE,
            "FILE_STORE_DIR": testcraft.FILE_STORE_DIR,
            "UPLOAD_DIR": testcraft.UPLOAD_DIR,
            "STORE_FILE": case_sets.STORE_FILE,
        }
        testcraft.OUTPUT_DIR = self.temp_dir.name
        testcraft.FILE_STORE_DIR = self.temp_dir.name
        testcraft.UPLOAD_DIR = os.path.join(self.temp_dir.name, "uploads")
        os.makedirs(testcraft.UPLOAD_DIR, exist_ok=True)
        testcraft.REQUIREMENTS_FILE = os.path.join(self.temp_dir.name, "requirements.json")
        case_sets.STORE_FILE = os.path.join(self.temp_dir.name, "case_sets.json")
        testcraft.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        self.client = testcraft.app.test_client()

    def tearDown(self):
        testcraft.OUTPUT_DIR = self._originals["OUTPUT_DIR"]
        testcraft.REQUIREMENTS_FILE = self._originals["REQUIREMENTS_FILE"]
        testcraft.FILE_STORE_DIR = self._originals["FILE_STORE_DIR"]
        testcraft.UPLOAD_DIR = self._originals["UPLOAD_DIR"]
        case_sets.STORE_FILE = self._originals["STORE_FILE"]
        self.temp_dir.cleanup()

    def seed_requirement(self, **overrides):
        requirement = {
            "id": "req-1",
            "title": "订单提交",
            "content": "登录用户可以提交订单",
            "priority": "P1",
            "status": "已拆分",
            "source": "手动录入",
            "source_url": "",
            "created_at": "2026-08-14 10:00:00",
            "updated_at": "2026-08-14 10:00:00",
            "feature_points": [{
                "id": "FP-001",
                "name": "库存校验",
                "description": "提交前校验库存",
                "priority": "P0",
                "main_flow": ["读取库存", "判断数量"],
                "acceptance_criteria": ["库存不足时拒绝"],
                "requirement_dependencies": [],
            }],
        }
        requirement.update(overrides)
        testcraft._save_requirements([requirement])
        return requirement


class RequirementCrudTests(BaseFlowTest):
    def test_create_requirement_persists_to_pool(self):
        response = self.client.post("/requirements", data={
            "source_type": "manual",
            "title": "优惠券核销",
            "content": "用户下单时可以使用优惠券",
            "priority": "P0",
            "source": "手动录入",
        }, follow_redirects=False)
        self.assertEqual(response.status_code, 302)

        pool = testcraft._load_requirements()
        self.assertEqual(len(pool), 1)
        self.assertEqual(pool[0]["title"], "优惠券核销")
        self.assertEqual(pool[0]["priority"], "P0")
        self.assertEqual(pool[0]["status"], "待分析")
        self.assertEqual(pool[0]["feature_points"], [])

    def test_update_requirement_keeps_feature_points(self):
        self.seed_requirement()
        response = self.client.post("/requirements/req-1/update", data={
            "title": "订单提交（修订）",
            "content": "登录用户可以提交订单，并支持取消",
            "priority": "P0",
            "status": "已完成",
            "source": "PRD v2",
        })
        self.assertEqual(response.status_code, 302)

        stored = testcraft._load_requirements()[0]
        self.assertEqual(stored["title"], "订单提交（修订）")
        self.assertEqual(stored["priority"], "P0")
        self.assertEqual(stored["status"], "已完成")
        self.assertEqual(stored["source"], "PRD v2")
        # 编辑不能把已经拆好的功能点冲掉
        self.assertEqual(len(stored["feature_points"]), 1)

    def test_update_requirement_rejects_empty_content(self):
        self.seed_requirement()
        self.client.post("/requirements/req-1/update", data={"title": "标题", "content": "  "})
        self.assertEqual(testcraft._load_requirements()[0]["title"], "订单提交")

    def test_update_requirement_ignores_invalid_status(self):
        self.seed_requirement()
        self.client.post("/requirements/req-1/update", data={
            "title": "订单提交", "content": "内容", "priority": "PX", "status": "乱填",
        })
        stored = testcraft._load_requirements()[0]
        self.assertEqual(stored["priority"], "P1")
        self.assertEqual(stored["status"], "已拆分")

    def test_delete_requirement(self):
        self.seed_requirement()
        response = self.client.post("/requirements/req-1/delete")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(testcraft._load_requirements(), [])

    def test_delete_missing_requirement_redirects(self):
        response = self.client.post("/requirements/nope/delete")
        self.assertEqual(response.status_code, 302)

    def test_uploaded_document_is_archived_into_pool(self):
        with (
            patch.object(testcraft, "parse_document", return_value="# 结算需求\n用户可以结算购物车"),
            patch.object(testcraft, "generate_test_cases", return_value=[make_case()]),
            patch.object(testcraft, "create_test_case_doc", side_effect=RuntimeError("skip")),
        ):
            response = self.client.post("/generate", data={
                "doc_file": (BytesIO("# 结算需求\n用户可以结算购物车".encode()), "结算.md"),
            }, content_type="multipart/form-data")

        self.assertEqual(response.status_code, 302)
        pool = testcraft._load_requirements()
        self.assertEqual(len(pool), 1, "上传的需求应当自动入池")
        self.assertEqual(pool[0]["title"], "结算需求")
        self.assertEqual(pool[0]["source"], "结算.md")
        self.assertEqual(pool[0]["test_case_count"], 1)

    def test_uploading_same_document_twice_reuses_one_record(self):
        text = "# 结算需求\n用户可以结算购物车"
        for _ in range(2):
            with (
                patch.object(testcraft, "parse_document", return_value=text),
                patch.object(testcraft, "generate_test_cases", return_value=[make_case()]),
                patch.object(testcraft, "create_test_case_doc", side_effect=RuntimeError("skip")),
            ):
                self.client.post("/generate", data={
                    "doc_file": (BytesIO(text.encode()), "结算.md"),
                }, content_type="multipart/form-data")
        self.assertEqual(len(testcraft._load_requirements()), 1)


class CasePointTests(BaseFlowTest):
    generated = [{
        "id": "CP-001",
        "name": "库存充足时允许提交",
        "category": "正常流程",
        "priority": "P0",
        "intent": "验证库存足够时下单成功",
        "method": "场景法",
        "conditions": ["库存 >= 下单数量"],
        "verify_points": ["订单创建成功"],
        "source": "主流程步骤2",
        "notes": "",
    }]

    def test_generate_case_points_persists_on_feature_point(self):
        self.seed_requirement()
        with patch.object(testcraft, "generate_case_points", return_value=self.generated) as mocked:
            response = self.client.post("/api/requirements/req-1/features/FP-001/case-points")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["count"], 1)
        mocked.assert_called_once()

        stored = testcraft._load_requirements()[0]["feature_points"][0]
        self.assertEqual(len(stored["case_points"]), 1)
        self.assertEqual(stored["case_points"][0]["id"], "CP-001")
        self.assertTrue(stored["case_points_updated_at"])

    def test_generate_case_points_requires_existing_feature(self):
        self.seed_requirement()
        response = self.client.post("/api/requirements/req-1/features/FP-404/case-points")
        self.assertEqual(response.status_code, 404)

    def test_delete_case_point(self):
        self.seed_requirement()
        with patch.object(testcraft, "generate_case_points", return_value=self.generated):
            self.client.post("/api/requirements/req-1/features/FP-001/case-points")

        response = self.client.delete("/api/requirements/req-1/features/FP-001/case-points/CP-001")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["count"], 0)
        self.assertEqual(testcraft._load_requirements()[0]["feature_points"][0]["case_points"], [])

    def test_delete_unknown_case_point_returns_404(self):
        self.seed_requirement()
        response = self.client.delete("/api/requirements/req-1/features/FP-001/case-points/CP-999")
        self.assertEqual(response.status_code, 404)

    def test_case_points_feed_test_case_generation(self):
        """有 Case 点时，用例生成必须带上测试点并按数量放大规模。"""
        self.seed_requirement()
        many = [dict(self.generated[0], id=f"CP-{i:03d}", name=f"测试点 {i}") for i in range(1, 11)]
        with patch.object(testcraft, "generate_case_points", return_value=many):
            self.client.post("/api/requirements/req-1/features/FP-001/case-points")

        cases = [make_case(i) for i in range(1, 13)]
        with patch.object(testcraft, "generate_test_cases", return_value=cases) as mocked:
            response = self.client.post("/api/requirements/req-1/features/FP-001/generate-cases")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["case_point_count"], 10)
        source_text = mocked.call_args.args[0]
        self.assertIn("case_points", source_text)
        self.assertIn("测试点 7", source_text)
        self.assertEqual(mocked.call_args.kwargs, {"target_count": 20, "min_count": 10})

    def test_generation_without_case_points_uses_default_size(self):
        self.seed_requirement()
        with patch.object(testcraft, "generate_test_cases", return_value=[make_case()]) as mocked:
            self.client.post("/api/requirements/req-1/features/FP-001/generate-cases")
        self.assertEqual(mocked.call_args.kwargs, {"target_count": 12, "min_count": 6})

    def test_case_points_page_lists_split_requirements(self):
        self.seed_requirement()
        response = self.client.get("/case-points")
        self.assertEqual(response.status_code, 200)
        self.assertIn("库存校验".encode(), response.data)

    def test_parse_case_points_normalizes_unknown_values(self):
        parsed = _parse_case_points(json.dumps({"case_points": [{
            "name": "越权访问被拒绝",
            "category": "瞎写的分类",
            "priority": "P9",
            "method": "拍脑袋",
        }]}))
        self.assertEqual(parsed[0]["id"], "CP-001")
        self.assertEqual(parsed[0]["category"], "正常流程")
        self.assertEqual(parsed[0]["priority"], "P1")
        self.assertEqual(parsed[0]["method"], "场景法")


class CaseSetTests(BaseFlowTest):
    def test_create_and_list_case_set(self):
        response = self.client.post("/case-sets", data={"name": "回归集", "description": "上线前跑"})
        self.assertEqual(response.status_code, 302)
        sets = case_sets.list_sets()
        self.assertEqual(len(sets), 1)
        self.assertEqual(sets[0]["name"], "回归集")
        self.assertEqual(sets[0]["case_count"], 0)

        page = self.client.get("/case-sets")
        self.assertIn("回归集".encode(), page.data)

    def test_create_case_set_rejects_empty_name(self):
        self.client.post("/case-sets", data={"name": "   "})
        self.assertEqual(case_sets.list_sets(), [])

    def test_save_selected_cases_from_results(self):
        record = case_sets.create_set("回归集")
        cases = [make_case(1, name="库存充足"), make_case(2, name="库存不足"), make_case(3, name="并发下单")]
        testcraft._store_run("aaaaaaaaaaaa", cases)
        with self.client.session_transaction() as session:
            session["run_id"] = "aaaaaaaaaaaa"
            session["run_origin"] = {"requirement_id": "req-1", "requirement_title": "订单提交", "feature_id": "FP-001"}

        response = self.client.post(
            f"/api/case-sets/{record['id']}/cases",
            json={"case_ids": ["TC-001", "TC-003"]},
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["added"], 2)
        self.assertEqual(body["total"], 2)

        stored = case_sets.get_set(record["id"])
        self.assertEqual([case["name"] for case in stored["cases"]], ["库存充足", "并发下单"])
        self.assertEqual(stored["cases"][0]["source_requirement_title"], "订单提交")
        self.assertEqual(stored["cases"][0]["source_run_id"], "aaaaaaaaaaaa")
        # 存入后重新编号，编号必须连续
        self.assertEqual([case["id"] for case in stored["cases"]], ["TC-001", "TC-002"])

    def test_empty_selection_saves_every_case(self):
        record = case_sets.create_set("全量集")
        testcraft._store_run("bbbbbbbbbbbb", [make_case(1), make_case(2)])
        with self.client.session_transaction() as session:
            session["run_id"] = "bbbbbbbbbbbb"

        response = self.client.post(f"/api/case-sets/{record['id']}/cases", json={"case_ids": []})
        self.assertEqual(response.get_json()["added"], 2)

    def test_duplicate_cases_are_skipped(self):
        record = case_sets.create_set("回归集")
        testcraft._store_run("cccccccccccc", [make_case(1, name="库存充足")])
        with self.client.session_transaction() as session:
            session["run_id"] = "cccccccccccc"

        first = self.client.post(f"/api/case-sets/{record['id']}/cases", json={})
        second = self.client.post(f"/api/case-sets/{record['id']}/cases", json={})
        self.assertEqual(first.get_json()["added"], 1)
        self.assertEqual(second.get_json()["added"], 0)
        self.assertEqual(second.get_json()["skipped"], 1)
        self.assertEqual(len(case_sets.get_set(record["id"])["cases"]), 1)

    def test_save_without_run_returns_error(self):
        record = case_sets.create_set("回归集")
        response = self.client.post(f"/api/case-sets/{record['id']}/cases", json={})
        self.assertEqual(response.status_code, 400)

    def test_save_to_missing_set_returns_404(self):
        testcraft._store_run("dddddddddddd", [make_case()])
        with self.client.session_transaction() as session:
            session["run_id"] = "dddddddddddd"
        response = self.client.post("/api/case-sets/does-not-exist/cases", json={})
        self.assertEqual(response.status_code, 404)

    def test_remove_cases_from_set(self):
        record = case_sets.create_set("回归集", cases=[make_case(1), make_case(2), make_case(3)])
        response = self.client.post(
            f"/api/case-sets/{record['id']}/cases/remove", json={"case_ids": ["TC-002"]}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["removed"], 1)
        remaining = case_sets.get_set(record["id"])["cases"]
        self.assertEqual([case["id"] for case in remaining], ["TC-001", "TC-002"])
        self.assertEqual(len(remaining), 2)

    def test_update_and_delete_case_set(self):
        record = case_sets.create_set("回归集")
        self.client.post(f"/case-sets/{record['id']}/update", data={"name": "冒烟集", "description": "每日"})
        self.assertEqual(case_sets.get_set(record["id"])["name"], "冒烟集")

        self.client.post(f"/case-sets/{record['id']}/delete")
        self.assertIsNone(case_sets.get_set(record["id"]))

    def test_case_set_detail_and_export(self):
        record = case_sets.create_set("回归集", cases=[make_case(1, name="库存充足")])
        detail = self.client.get(f"/case-sets/{record['id']}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("库存充足".encode(), detail.data)

        excel = self.client.get(f"/case-sets/{record['id']}/download/excel")
        self.assertEqual(excel.status_code, 200)
        self.assertGreater(len(excel.data), 0)

        markdown = self.client.get(f"/case-sets/{record['id']}/download/markdown")
        self.assertEqual(markdown.status_code, 200)

    def test_export_rejects_unknown_format(self):
        record = case_sets.create_set("回归集", cases=[make_case()])
        response = self.client.get(f"/case-sets/{record['id']}/download/pdf")
        self.assertEqual(response.status_code, 302)

    def test_missing_set_detail_redirects(self):
        self.assertEqual(self.client.get("/case-sets/nope").status_code, 302)

    def test_normalize_case_drops_unknown_fields(self):
        normalized = case_sets.normalize_case(
            {"id": "TC-9", "name": "用例", "priority": "bad", "secret": "不该被存"},
            source_run_id="run-1",
        )
        self.assertNotIn("secret", normalized)
        self.assertEqual(normalized["priority"], "P2")
        self.assertEqual(normalized["module"], "General")
        self.assertEqual(normalized["source_run_id"], "run-1")


class NavigationTests(BaseFlowTest):
    def test_sidebar_orders_stages_by_workflow(self):
        """菜单必须按 需求拆分 -> 生成 Case 点 -> 生成测试用例 排列。"""
        html = self.client.get("/").data.decode()
        # 只看侧边栏的「测试设计流程」分组，避开页面标题里的同名文案
        nav = html[html.index("测试设计流程"):html.index("测试资产")]
        positions = [nav.index(label) for label in ("需求拆分", "生成 Case 点", "生成测试用例")]
        self.assertEqual(positions, sorted(positions))
        self.assertLess(nav.index("stage-num"), nav.index("需求拆分"))

    def test_nav_counts_reflect_pool_state(self):
        self.seed_requirement()
        case_sets.create_set("回归集")
        with testcraft.app.test_request_context("/"):
            counts = testcraft.inject_nav_state()["nav_counts"]
        self.assertEqual(counts["requirements"], 1)
        self.assertEqual(counts["features"], 1)
        self.assertEqual(counts["case_points"], 0)
        self.assertEqual(counts["case_sets"], 1)
        self.assertIn("1 条需求", self.client.get("/").data.decode())

    def test_every_stage_page_renders(self):
        self.seed_requirement()
        for path in ("/", "/requirements", "/case-points", "/case-sets", "/history", "/requirements/req-1"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)


if __name__ == "__main__":
    unittest.main()
