"""本地冒烟测试：验证需求池的 PG 持久化 + 不依赖 LLM 的路由。

需要 TESTCRAFT_DATABASE_URL 指向一个可用 PG。用法：

    TESTCRAFT_DATABASE_URL=postgresql://postgres:x@localhost:55432/testcraft \
        python test_requirements_persistence.py
"""
import os
import sys

assert os.environ.get("TESTCRAFT_DATABASE_URL"), "请先设置 TESTCRAFT_DATABASE_URL"

import db

FAIL = []


def check(name, cond):
    print(("  ✅ " if cond else "  ❌ ") + name)
    if not cond:
        FAIL.append(name)


FEATURE_POINTS = [{
    "id": "FP-001",
    "name": "库存校验",
    "description": "提交订单前校验库存",
    "priority": "P0",
    "actors": ["登录用户"],
    "main_flow": ["读取库存", "比较下单数量"],
    "exception_flows": ["库存不足时拒绝提交"],
    "business_rules": ["库存必须大于零"],
    "acceptance_criteria": ["库存不足返回明确错误码"],
    "requirement_dependencies": [
        {"requirement_id": "req-old", "requirement_title": "商品中心",
         "dependency_type": "数据依赖", "reason": "需要读取商品库存"},
    ],
    "case_points": [
        {"id": "CP-001", "name": "库存充足可提交", "category": "正常流程",
         "priority": "P0", "method": "场景法", "conditions": ["库存 >= 数量"],
         "verify_points": ["订单创建成功"], "source": "主流程步骤2", "notes": ""},
    ],
    "test_case_run_id": "run_req_001",
    "test_case_count": 12,
    "test_cases_updated_at": "2026-08-14 12:00:00",
}]

SAMPLE = {
    "id": "req_test_001",
    "title": "订单提交",
    "content": "登录用户可以提交订单，库存不足时拒绝。",
    "priority": "P0",
    "status": "已拆分",
    "source": "手动录入",
    "source_url": "",
    "feature_points": FEATURE_POINTS,
    "created_at": "2026-08-14 10:00:00",
    "updated_at": "2026-08-14 10:30:00",
}

print("== db 层：requirements 表 ==")
db.init_db()
db.init_db()
check("init_db 幂等（含 requirements 表）", True)

db.sync_requirements([])  # 从干净状态开始
db.save_requirement(SAMPLE)
r = db.get_requirement("req_test_001")
check("save_requirement + get_requirement 往返", r is not None)
check("结构化字段保真（标题/优先级/状态）",
      r and r["title"] == "订单提交" and r["priority"] == "P0" and r["status"] == "已拆分")
check("content 中文保真", r and r["content"].startswith("登录用户"))
check("created_at / updated_at 原样返回字符串",
      r and r["created_at"] == "2026-08-14 10:00:00" and isinstance(r["updated_at"], str))

fp = r["feature_points"] if r else []
check("feature_points 反序列化为 list", isinstance(fp, list) and len(fp) == 1)
check("功能点嵌套字段保真（名称/主流程）",
      fp and fp[0]["name"] == "库存校验" and fp[0]["main_flow"][1] == "比较下单数量")
check("功能点里的跨需求依赖保真",
      fp and fp[0]["requirement_dependencies"][0]["dependency_type"] == "数据依赖")
check("功能点里的 Case 点保真",
      fp and fp[0]["case_points"][0]["id"] == "CP-001")
check("功能点关联的 test_run 字段保真",
      fp and fp[0]["test_case_run_id"] == "run_req_001" and fp[0]["test_case_count"] == 12)

# 列以外的顶层键（例如上传入池时回写的 run 信息）必须原样带回
db.save_requirement({**SAMPLE, "id": "req_test_extra", "test_case_run_id": "run_x", "test_case_count": 7})
extra = db.get_requirement("req_test_extra")
check("列以外的顶层键不会在往返中丢失",
      extra and extra["test_case_run_id"] == "run_x" and extra["test_case_count"] == 7)

updated = {**SAMPLE, "title": "订单提交（改）", "status": "已完成", "feature_points": []}
db.save_requirement(updated)
r2 = db.get_requirement("req_test_001")
check("同 id upsert 覆盖（标题/状态）", r2["title"] == "订单提交（改）" and r2["status"] == "已完成")
check("upsert 覆盖 feature_points（1→0）", r2["feature_points"] == [])
check("get_requirement 不存在返回 None", db.get_requirement("no_such_req") is None)

db.save_requirement({**SAMPLE, "id": "req_test_002", "title": "退款", "updated_at": "2026-08-14 09:00:00"})
lst = db.list_requirements()
ids = [x["id"] for x in lst]
check("list_requirements 含全部需求", set(ids) >= {"req_test_001", "req_test_002"})
check("list_requirements 按 updated_at 倒序",
      ids.index("req_test_001") < ids.index("req_test_002"))
check("list_requirements 返回完整结构（含 feature_points 键）",
      all("feature_points" in x for x in lst))

check("delete_requirement 生效", db.delete_requirement("req_test_002") is True)
check("delete_requirement 删不存在的返回 False", db.delete_requirement("req_test_002") is False)

# 整表覆盖语义：不在列表里的行必须被删掉，否则 UI 上的"删除需求"会失效
db.sync_requirements([{**SAMPLE, "id": "keep_1"}, {**SAMPLE, "id": "keep_2"}])
db.sync_requirements([{**SAMPLE, "id": "keep_1"}])
remaining = {x["id"] for x in db.list_requirements()}
check("sync_requirements 删除不在列表中的需求", remaining == {"keep_1"})
db.sync_requirements([])
check("sync_requirements 传空列表清空需求池", db.list_requirements() == [])

print("== Flask 路由（无 LLM）==")
os.environ.setdefault("FLASK_SECRET_KEY", "test")
import app as flaskapp

check("app 选中 PG 后端", flaskapp.DB_ENABLED is True)
client = flaskapp.app.test_client()
flaskapp.app.config.update(TESTING=True)

resp = client.post("/requirements", data={
    "source_type": "manual", "title": "优惠券核销", "priority": "P0",
    "content": "下单时可以使用优惠券，过期券不可用。", "source": "手动录入",
})
check("POST /requirements 创建成功并跳转", resp.status_code == 302)
created = [x for x in flaskapp._load_requirements() if x["title"] == "优惠券核销"]
check("新建的需求已写进 PG", len(created) == 1)
new_id = created[0]["id"] if created else ""
check("新建需求默认状态为待分析", created and created[0]["status"] == "待分析")

resp = client.get("/requirements")
check("GET /requirements 200 且渲染出需求", resp.status_code == 200 and "优惠券核销".encode() in resp.data)

resp = client.get(f"/requirements?status=待分析")
check("按状态筛选命中", "优惠券核销".encode() in resp.data)
resp = client.get(f"/requirements?status=已完成")
check("按状态筛选可排除", "优惠券核销".encode() not in resp.data)
resp = client.get(f"/requirements?q=优惠券")
check("按关键词搜索命中", "优惠券核销".encode() in resp.data)
resp = client.get(f"/requirements?q=不存在的关键词")
check("按关键词搜索可排除", "优惠券核销".encode() not in resp.data)

resp = client.get(f"/requirements/{new_id}")
check("GET /requirements/<id> 详情 200", resp.status_code == 200)

resp = client.post(f"/api/requirements/{new_id}/status", json={"status": "分析中"})
check("POST 状态更新 200", resp.status_code == 200)
check("状态已落库", flaskapp._find_requirement(new_id)[1]["status"] == "分析中")

resp = client.post(f"/api/requirements/{new_id}/status", json={"status": "乱填的状态"})
check("非法状态被拒绝（400）", resp.status_code == 400)

# 拆分依赖 LLM，这里直接写入拆分结果，验证嵌套功能点在 PG 里回读一致
requirements, requirement = flaskapp._find_requirement(new_id)
requirement["feature_points"] = FEATURE_POINTS
requirement["status"] = "已拆分"
flaskapp._save_requirements(requirements)
reread = flaskapp._find_requirement(new_id)[1]
check("拆分后的功能点回读一致", len(reread["feature_points"]) == 1)
check("回读的功能点保留 Case 点", reread["feature_points"][0]["case_points"][0]["name"] == "库存充足可提交")

resp = client.get("/case-points")
check("GET /case-points 200 且含功能点", resp.status_code == 200 and "库存校验".encode() in resp.data)

resp = client.post(f"/requirements/{new_id}/update", data={
    "title": "优惠券核销 V2", "content": "补充：叠加规则", "priority": "P1", "status": "已完成",
})
check("POST 编辑需求 302", resp.status_code == 302)
edited = flaskapp._find_requirement(new_id)[1]
check("编辑已落库", edited["title"] == "优惠券核销 V2" and edited["priority"] == "P1")
check("编辑保留已拆分的功能点", len(edited["feature_points"]) == 1)

resp = client.post(f"/requirements/{new_id}/delete")
check("POST 删除需求 302", resp.status_code == 302)
check("删除后 PG 中确实没有了", flaskapp._find_requirement(new_id)[1] is None)

print()
if FAIL:
    print(f"❌ {len(FAIL)} 项失败: {FAIL}")
    sys.exit(1)
print("✅ 全部通过")
