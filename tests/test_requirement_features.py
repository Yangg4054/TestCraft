import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as testcraft
from services.ai_generator import _parse_feature_points


class RequirementFeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_output_dir = testcraft.OUTPUT_DIR
        self.original_requirements_file = testcraft.REQUIREMENTS_FILE
        self.original_file_store_dir = testcraft.FILE_STORE_DIR
        testcraft.OUTPUT_DIR = self.temp_dir.name
        testcraft.FILE_STORE_DIR = self.temp_dir.name
        testcraft.REQUIREMENTS_FILE = os.path.join(
            self.temp_dir.name, "requirements.json"
        )
        testcraft.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        self.client = testcraft.app.test_client()

    def tearDown(self):
        testcraft.OUTPUT_DIR = self.original_output_dir
        testcraft.REQUIREMENTS_FILE = self.original_requirements_file
        testcraft.FILE_STORE_DIR = self.original_file_store_dir
        self.temp_dir.cleanup()

    def save_requirements(self, requirements):
        testcraft._save_requirements(requirements)

    def test_split_passes_previous_requirements_and_persists_details(self):
        requirements = [
            {
                "id": "old-1",
                "title": "账号体系",
                "content": "提供登录账号",
                "status": "已完成",
                "feature_points": [],
            },
            {
                "id": "current-1",
                "title": "订单提交",
                "content": "登录用户提交订单",
                "status": "待分析",
                "feature_points": [],
            },
        ]
        self.save_requirements(requirements)
        feature = {
            "id": "FP-001",
            "name": "提交订单",
            "description": "校验并提交订单",
            "actors": ["登录用户"],
            "trigger": "点击提交",
            "preconditions": ["用户已登录"],
            "main_flow": ["校验库存", "创建订单"],
            "exception_flows": ["库存不足时拒绝提交"],
            "business_rules": ["库存必须大于零"],
            "data_changes": ["新增订单记录"],
            "acceptance_criteria": ["订单创建成功"],
            "priority": "P0",
            "dependencies": [],
            "requirement_dependencies": [
                {
                    "requirement_id": "old-1",
                    "requirement_title": "账号体系",
                    "dependency_type": "前置能力",
                    "reason": "提交订单要求登录态",
                }
            ],
            "risks": [],
        }

        with patch.object(testcraft, "split_requirement", return_value=[feature]) as mocked:
            response = self.client.post("/api/requirements/current-1/split")

        self.assertEqual(response.status_code, 200)
        previous = mocked.call_args.args[2]
        self.assertEqual([item["id"] for item in previous], ["old-1"])
        stored = testcraft._load_requirements()[1]
        self.assertEqual(stored["feature_points"][0]["main_flow"][1], "创建订单")
        self.assertEqual(
            stored["feature_points"][0]["requirement_dependencies"][0]["requirement_id"],
            "old-1",
        )

    def test_parser_normalizes_detailed_feature_fields(self):
        payload = json.dumps({
            "feature_points": [{
                "name": "权限校验",
                "description": "校验操作权限",
                "roles": "管理员",
                "trigger": "提交操作",
                "preconditions": "用户已登录",
                "mainFlow": ["读取权限", "执行操作"],
                "exceptionFlows": ["无权限返回 403"],
                "businessRules": ["只有管理员可执行"],
                "dataChanges": ["写入操作日志"],
                "acceptanceCriteria": ["管理员操作成功"],
                "requirementDependencies": [{
                    "requirementId": "REQ-001",
                    "requirementTitle": "统一登录",
                    "dependencyType": "接口依赖",
                    "reason": "需要登录态接口",
                }],
            }]
        }, ensure_ascii=False)

        point = _parse_feature_points(payload)[0]
        self.assertEqual(point["actors"], ["管理员"])
        self.assertEqual(point["main_flow"], ["读取权限", "执行操作"])
        self.assertEqual(point["business_rules"], ["只有管理员可执行"])
        self.assertEqual(
            point["requirement_dependencies"][0]["dependency_type"], "接口依赖"
        )

    def test_feature_case_endpoint_returns_404_for_missing_records(self):
        response = self.client.post(
            "/api/requirements/missing/features/FP-001/generate-cases"
        )
        self.assertEqual(response.status_code, 404)

        self.save_requirements([{
            "id": "req-1",
            "title": "需求",
            "content": "内容",
            "feature_points": [],
        }])
        response = self.client.post(
            "/api/requirements/req-1/features/FP-001/generate-cases"
        )
        self.assertEqual(response.status_code, 404)

    def test_feature_case_endpoint_persists_run_and_metadata(self):
        self.save_requirements([{
            "id": "req-1",
            "title": "订单提交",
            "content": "用户提交订单",
            "feature_points": [{
                "id": "FP-001",
                "name": "库存校验",
                "description": "提交前校验库存",
                "main_flow": ["读取库存", "判断数量"],
                "requirement_dependencies": [],
            }],
        }])
        cases = [{
            "id": "TC-001",
            "module": "订单",
            "name": "库存充足",
            "priority": "P0",
            "preconditions": "库存为 10",
            "steps": "提交数量 1",
            "expected_result": "订单创建成功",
            "type": "Functional",
            "method": "场景法",
        }]

        def touch_export(_cases, path):
            Path(path).touch()
            return path

        with (
            patch.object(testcraft, "generate_test_cases", return_value=cases) as mocked,
            patch.object(testcraft, "export_excel", side_effect=touch_export),
            patch.object(testcraft, "export_markdown", side_effect=touch_export),
        ):
            response = self.client.post(
                "/api/requirements/req-1/features/FP-001/generate-cases"
            )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["redirect"], "/results")
        mocked.assert_called_once()
        self.assertEqual(mocked.call_args.kwargs, {"target_count": 12, "min_count": 6})
        path = Path(self.temp_dir.name, f"testcases_{data['run_id']}.json")
        self.assertTrue(path.exists())

        stored_feature = testcraft._load_requirements()[0]["feature_points"][0]
        self.assertEqual(stored_feature["test_case_run_id"], data["run_id"])
        self.assertEqual(stored_feature["test_case_count"], 1)
        with self.client.session_transaction() as session:
            self.assertEqual(session["run_id"], data["run_id"])

    def test_core_pages_and_feature_button_render(self):
        self.save_requirements([{
            "id": "req-1",
            "title": "需求",
            "content": "内容",
            "priority": "P1",
            "status": "已拆分",
            "source": "手动录入",
            "source_url": "",
            "created_at": "2026-08-13 10:00:00",
            "updated_at": "2026-08-13 10:00:00",
            "feature_points": [{
                "id": "FP-001",
                "name": "功能点",
                "description": "描述",
                "priority": "P1",
                "acceptance_criteria": [],
                "dependencies": [],
                "risks": [],
            }],
        }])
        for path in ("/", "/requirements", "/history"):
            self.assertEqual(self.client.get(path).status_code, 200)
        detail = self.client.get("/requirements/req-1")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("生成用例".encode(), detail.data)

    def test_regenerate_still_uses_shared_result_storage(self):
        old_cases = [{
            "id": "TC-001",
            "module": "旧模块",
            "name": "旧用例",
            "priority": "P1",
            "preconditions": "无",
            "steps": "执行旧步骤",
            "expected_result": "旧结果",
            "type": "Functional",
            "method": "场景法",
        }]
        old_run_id = "0" * 12
        old_path = Path(self.temp_dir.name, f"testcases_{old_run_id}.json")
        old_path.write_text(json.dumps(old_cases, ensure_ascii=False), encoding="utf-8")
        new_cases = [dict(old_cases[0], name=f"新用例 {index}") for index in range(12)]

        def touch_export(_cases, path):
            Path(path).touch()
            return path

        with self.client.session_transaction() as session:
            session["run_id"] = old_run_id
        with (
            patch("services.ai_generator.call_llm", return_value=json.dumps({"test_cases": new_cases}, ensure_ascii=False)),
            patch.object(testcraft, "export_excel", side_effect=touch_export),
            patch.object(testcraft, "export_markdown", side_effect=touch_export),
        ):
            response = self.client.post(
                "/api/regenerate", json={"feedback": "补充异常场景"}
            )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["count"], 12)
        self.assertTrue(
            Path(self.temp_dir.name, f"testcases_{data['run_id']}.json").exists()
        )


if __name__ == "__main__":
    unittest.main()
