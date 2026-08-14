"""本地冒烟测试：验证 PG 持久化 + 不依赖 LLM 的路由。

需要 TESTCRAFT_DATABASE_URL 指向一个可用 PG。
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


# 字段名与 ai_generator 产出 / results.html 渲染保持一致（全小写）
SAMPLE = [
    {"id": "TC001", "module": "登录", "name": "正常登录", "priority": "P0",
     "preconditions": "已注册", "steps": "输入账号密码", "expected_result": "登录成功",
     "type": "功能", "method": "手动"},
    {"id": "TC002", "module": "登录", "name": "密码错误", "priority": "P1",
     "preconditions": "已注册", "steps": "输入错误密码", "expected_result": "提示密码错误",
     "type": "功能", "method": "手动"},
]

print("== db 层 ==")
db.init_db()
check("init_db 幂等（重复调用不报错）", True)
db.init_db()

db.save_run("run_test_001", SAMPLE,
            requirement_text="需求：用户登录功能，支持账号密码登录……",
            requirement_source="login_spec.docx",
            code_structure_text="app/auth.py::login()",
            feishu_doc_url="")
r = db.get_run("run_test_001")
check("save_run + get_run 往返", r is not None)
check("case_count 正确", r and r["count"] == 2)
check("test_cases 反序列化为 list", r and isinstance(r["test_cases"], list))
check("JSONB 内容保真（中文/字段）", r and r["test_cases"][0]["id"] == "TC001" and r["test_cases"][0]["module"] == "登录")
check("需求文本已存", r and r["requirement_text"].startswith("需求"))
check("需求来源已存", r and r["requirement_source"] == "login_spec.docx")
check("代码结构已存", r and r["code_structure_text"] == "app/auth.py::login()")

lst = db.list_runs()
check("list_runs 含刚存的 run", any(x["run_id"] == "run_test_001" for x in lst))
check("list_runs 字段形状 run_id/count/time", lst and set(lst[0]) == {"run_id", "count", "time"})

db.update_feishu_url("run_test_001", "https://feishu.cn/docx/XYZ")
check("update_feishu_url 生效", db.get_run("run_test_001")["feishu_doc_url"] == "https://feishu.cn/docx/XYZ")

db.save_run("run_test_001", SAMPLE[:1], requirement_text="覆盖写")
r2 = db.get_run("run_test_001")
check("同 run_id upsert 覆盖（count 2→1）", r2["count"] == 1)
check("upsert 更新 requirement_text", r2["requirement_text"] == "覆盖写")
check("get_run 不存在返回 None", db.get_run("no_such_run") is None)

print("== Flask 路由（无 LLM）==")
os.environ.setdefault("FLASK_SECRET_KEY", "test")
import app as flaskapp

check("app 选中 PG 后端", flaskapp.DB_ENABLED is True)
client = flaskapp.app.test_client()

flaskapp._store_run("run_ui_001", SAMPLE, requirement_text="req", requirement_source="s.md")

resp = client.get("/history")
check("/history 200 且含 run", resp.status_code == 200 and b"run_ui_001" in resp.data)

resp = client.get("/history/run_ui_001")
check("/history/<id> 200", resp.status_code == 200)
check("/history/<id> 渲染用例内容", "正常登录".encode() in resp.data)

with client.session_transaction() as s:
    s["run_id"] = "run_ui_001"

resp = client.get("/download/excel")
check("/download/excel 200", resp.status_code == 200)
check("/download/excel 是 xlsx 且非空", resp.data[:2] == b"PK" and len(resp.data) > 500)

resp = client.get("/download/markdown")
check("/download/markdown 200 非空", resp.status_code == 200 and len(resp.data) > 10)

resp = client.get("/results")
check("/results 200", resp.status_code == 200)

resp = client.get("/")
check("/ 首页 200", resp.status_code == 200)

print()
if FAIL:
    print(f"❌ {len(FAIL)} 项失败: {FAIL}")
    sys.exit(1)
print("✅ 全部通过")
