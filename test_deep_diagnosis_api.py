"""
深度诊断 API 层的离线自动化测试（FastAPI TestClient，不依赖网络/真实DeepSeek Key）。

关键点：必须在 import main / import storage 之前把 GEO_DB_PATH 环境变量设成
一个临时文件路径——storage.DB_PATH 是模块加载时算好的常量，事后改环境变量不生效。
这样测试用的数据库和真实开发用的 geo_diagnosis.db 完全隔离，不会互相污染。

运行方式：venv 里安装了 fastapi/httpx 之后 python test_deep_diagnosis_api.py
"""

import os
import tempfile

_TMP_DB = tempfile.mktemp(suffix=".db")
os.environ["GEO_DB_PATH"] = _TMP_DB
os.environ.pop("DEEPSEEK_API_KEY", None)

from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402

client = TestClient(main.app)


def _success_item_payload(q, brand_mentioned=False, rank=None, citations=None):
    return {
        "question": q["question"], "query_intent": q["query_intent"],
        "commercial_value": q["commercial_value"], "platform": "DeepSeek", "status": "success",
        "raw_answer": "英得尔、冰虎不错。", "model": "deepseek-v4-flash",
        "tested_at": "2026-08-12 00:00:00",
        "brand_mentioned": brand_mentioned, "mention_count": 1 if brand_mentioned else 0,
        "recommended": False, "rank": rank,
        "competitors": [
            {"name": "英得尔", "aliases": ["Indel B"], "confidence": "high", "evidence": "英得尔"},
            {"name": "冰虎", "aliases": ["Alpicool"], "confidence": "high", "evidence": "冰虎"},
        ],
        "citations": citations or [],
        "answer_fit": "partial", "industry_knowledge_quality": "low",
        "gaps": [{"type": "BRAND_ABSENCE", "label": "品牌未进入AI答案", "evidence": "..."}],
        "diagnosis_summary": "...", "observations": ["..."], "inferences": ["..."],
        "actions": [{"priority": "P1", "action_type": "ENTITY_BUILDING", "action": "补充实体信息",
                     "reason": "...", "target_query": q["question"], "gap_type": "BRAND_ABSENCE"}],
    }


def _failed_item_payload(q, error_message="请求超时"):
    return {
        "question": q["question"], "query_intent": q["query_intent"],
        "commercial_value": q["commercial_value"], "platform": "DeepSeek",
        "status": "failed", "error_message": error_message,
    }


def _run_full_batch(fail_indices=()):
    """跑一次完整的20题批量流程（模拟前端逐题调用的顺序），
    fail_indices 指定哪几题模拟成失败。返回 (run_id, questions, complete_response)。
    """
    r = client.post("/api/diagnose/deep/start", json={"market": "cn", "platform": "DeepSeek", "count": 20})
    assert r.status_code == 200
    data = r.json()
    run_id, questions = data["run_id"], data["questions"]

    for i, q in enumerate(questions):
        if i in fail_indices:
            payload = _failed_item_payload(q)
        else:
            payload = _success_item_payload(q, brand_mentioned=(i % 3 == 0), rank=(1 if i == 0 else None))
        r_item = client.post(f"/api/diagnose/deep/{run_id}/item", json=payload)
        assert r_item.status_code == 200, r_item.text

    r_complete = client.post(f"/api/diagnose/deep/{run_id}/complete")
    assert r_complete.status_code == 200
    return run_id, questions, r_complete.json()


# ---------------------------------------------------------------------------
# 11. SQLite保存验证
# ---------------------------------------------------------------------------

def test_start_creates_run_with_20_questions():
    r = client.post("/api/diagnose/deep/start", json={"market": "cn", "platform": "DeepSeek", "count": 20})
    assert r.status_code == 200
    data = r.json()
    assert len(data["questions"]) == 20
    assert all({"question", "query_intent", "commercial_value"} <= set(q.keys()) for q in data["questions"])


def test_item_and_complete_persist_to_sqlite():
    run_id, questions, completed = _run_full_batch()
    assert completed["run"]["status"] == "completed"
    assert completed["run"]["success_count"] == 20
    assert completed["run"]["failed_count"] == 0
    assert len(completed["items"]) == 20
    # 直接查 detail 接口，验证真的落库了，不是只存在内存里
    r_detail = client.get(f"/api/diagnose/deep/runs/{run_id}")
    assert r_detail.status_code == 200
    assert len(r_detail.json()["items"]) == 20


def test_unknown_run_id_returns_404():
    r = client.post("/api/diagnose/deep/99999/item", json=_success_item_payload(
        {"question": "x", "query_intent": "other", "commercial_value": "low"}
    ))
    assert r.status_code == 404
    r2 = client.get("/api/diagnose/deep/runs/99999")
    assert r2.status_code == 404


# ---------------------------------------------------------------------------
# 12. 浏览器刷新后历史仍存在
# ---------------------------------------------------------------------------

def test_data_survives_fresh_requests_simulating_refresh():
    run_id, _, _ = _run_full_batch()
    # 用全新的、互不共享任何Python对象状态的请求去读，模拟浏览器刷新后重新拉数据
    r1 = client.get(f"/api/diagnose/deep/runs/{run_id}")
    r2 = client.get(f"/api/diagnose/deep/runs/{run_id}")
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["items"] == r2.json()["items"]
    assert len(r1.json()["items"]) == 20

    r_list = client.get("/api/diagnose/deep/runs")
    assert any(run["id"] == run_id for run in r_list.json()["runs"])


# ---------------------------------------------------------------------------
# 13. Dashboard读取最新成功诊断
# ---------------------------------------------------------------------------

def test_latest_endpoint_returns_most_recently_completed_run():
    run_id_1, _, _ = _run_full_batch()
    run_id_2, _, _ = _run_full_batch()

    r = client.get("/api/diagnose/deep/latest")
    assert r.status_code == 200
    data = r.json()
    assert data["run"]["id"] == run_id_2  # 最新完成的那一次
    assert data["report"]["stats"]["total_questions"] == 20


def test_latest_endpoint_ignores_in_progress_run():
    # 建一个只 start 不 complete 的 run，latest 不应该把它当成"最新成功诊断"
    r_start = client.post("/api/diagnose/deep/start", json={"market": "cn", "platform": "DeepSeek", "count": 20})
    in_progress_run_id = r_start.json()["run_id"]

    r_latest = client.get("/api/diagnose/deep/latest")
    if r_latest.json()["run"] is not None:
        assert r_latest.json()["run"]["id"] != in_progress_run_id


# ---------------------------------------------------------------------------
# 14. 单题失败不阻断剩余任务
# ---------------------------------------------------------------------------

def test_single_item_failure_does_not_block_the_rest():
    run_id, questions, completed = _run_full_batch(fail_indices={3, 8, 15})
    assert completed["run"]["success_count"] == 17
    assert completed["run"]["failed_count"] == 3
    assert completed["run"]["status"] == "completed"  # 整体仍然跑到完成状态

    items = completed["items"]
    failed_items = [it for it in items if it["status"] == "failed"]
    assert len(failed_items) == 3
    assert all(it["error_message"] for it in failed_items)
    # 失败题不影响其余17题的正常统计
    assert completed["report"]["stats"]["success_count"] == 17


def test_only_deepseek_platform_supported():
    r = client.post("/api/diagnose/deep/start", json={"market": "cn", "platform": "豆包", "count": 20})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# GEO教程接口
# ---------------------------------------------------------------------------

def test_tutorial_endpoint_returns_only_basics_when_no_completed_run():
    # 用一个全新的、不会和其它测试的market/platform组合冲突的过滤条件，
    # 确保这里查到的确实是"没有已完成诊断"的状态。
    r = client.get("/api/tutorial/geo", params={"market": "cn", "platform": "不存在的平台"})
    assert r.status_code == 200
    data = r.json()
    assert data["run"] is None
    assert data["tutorial"]["has_diagnosis_data"] is False
    assert data["tutorial"]["personalized"] == []
    assert len(data["tutorial"]["basics"]) > 0


def test_tutorial_endpoint_returns_personalized_content_after_completed_run():
    _run_full_batch()  # 全部20题成功，且brand_mentioned在(i%3==0)时为True，必然会产生真实Gap
    r = client.get("/api/tutorial/geo", params={"market": "cn", "platform": "DeepSeek"})
    assert r.status_code == 200
    data = r.json()
    assert data["run"] is not None
    assert data["tutorial"]["has_diagnosis_data"] is True
    assert len(data["tutorial"]["personalized"]) > 0
    for p in data["tutorial"]["personalized"]:
        assert p["affected_count"] > 0
        assert len(p["affected_questions"]) > 0
        assert p["how_to"]


ALL_TESTS = [v for k, v in list(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    failures = 0
    for fn in ALL_TESTS:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {fn.__name__}: {e}")
        except Exception as e:
            failures += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(ALL_TESTS) - failures}/{len(ALL_TESTS)} passed")
    if os.path.exists(_TMP_DB):
        os.remove(_TMP_DB)
    if failures:
        raise SystemExit(1)
