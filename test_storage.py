"""
storage.py（SQLite持久化层）的离线自动化测试。
每个测试用独立的临时db文件，互不干扰，测试完清理。
运行方式：python test_storage.py
"""

import os
import tempfile

import storage


def _tmp_db():
    return tempfile.mktemp(suffix=".db")


def _sample_item(question="q1", status="success", **overrides):
    item = {
        "question": question, "query_intent": "manufacturer", "commercial_value": "high",
        "platform": "DeepSeek", "status": status,
        "raw_answer": "英得尔、冰虎不错。", "model": "deepseek-v4-flash",
        "tested_at": "2026-08-12 00:00:00",
        "brand_mentioned": False, "mention_count": 0, "recommended": False, "rank": None,
        "competitors": [
            {"name": "英得尔", "aliases": ["Indel B"], "confidence": "high", "evidence": "英得尔"},
            {"name": "冰虎", "aliases": ["Alpicool"], "confidence": "high", "evidence": "冰虎"},
        ],
        "citations": [],
        "answer_fit": "partial", "industry_knowledge_quality": "low",
        "gaps": [{"type": "BRAND_ABSENCE", "label": "品牌未进入AI答案", "evidence": "..."}],
        "diagnosis_summary": "...", "observations": ["..."], "inferences": ["..."],
        "actions": [{"priority": "P1", "action_type": "ENTITY_BUILDING", "action": "...",
                     "reason": "...", "target_query": question, "gap_type": "BRAND_ABSENCE"}],
    }
    item.update(overrides)
    return item


def test_create_run_and_get_run_round_trip():
    db = _tmp_db()
    storage.init_db(db)
    run_id = storage.create_run("cn", "DeepSeek", "quick_20", 20, db_path=db)
    run = storage.get_run(run_id, db_path=db)
    assert run["market"] == "cn"
    assert run["platform"] == "DeepSeek"
    assert run["status"] == "running"
    assert run["total_questions"] == 20
    os.remove(db)


def test_add_item_updates_success_and_failed_counts():
    db = _tmp_db()
    storage.init_db(db)
    run_id = storage.create_run("cn", "DeepSeek", "quick_20", 2, db_path=db)
    storage.add_item(run_id, _sample_item("q1", status="success"), db_path=db)
    storage.add_item(run_id, _sample_item("q2", status="failed", error_message="超时"), db_path=db)
    run = storage.get_run(run_id, db_path=db)
    assert run["success_count"] == 1
    assert run["failed_count"] == 1
    os.remove(db)


def test_get_run_items_deserializes_json_fields():
    db = _tmp_db()
    storage.init_db(db)
    run_id = storage.create_run("cn", "DeepSeek", "quick_20", 1, db_path=db)
    storage.add_item(run_id, _sample_item("q1"), db_path=db)
    items = storage.get_run_items(run_id, db_path=db)
    assert len(items) == 1
    it = items[0]
    assert [c["name"] for c in it["competitors"]] == ["英得尔", "冰虎"]
    assert it["competitors"][0]["confidence"] == "high"
    assert isinstance(it["gaps"], list) and it["gaps"][0]["type"] == "BRAND_ABSENCE"
    assert it["brand_mentioned"] is False
    assert isinstance(it["actions"], list) and it["actions"][0]["priority"] == "P1"
    os.remove(db)


def test_failed_item_with_missing_fields_does_not_crash():
    db = _tmp_db()
    storage.init_db(db)
    run_id = storage.create_run("cn", "DeepSeek", "quick_20", 1, db_path=db)
    storage.add_item(run_id, {
        "question": "失败题", "query_intent": "other", "commercial_value": "low",
        "platform": "DeepSeek", "status": "failed", "error_message": "网络错误",
    }, db_path=db)
    items = storage.get_run_items(run_id, db_path=db)
    assert items[0]["status"] == "failed"
    assert items[0]["error_message"] == "网络错误"
    assert items[0]["competitors"] == []  # 空JSON字段应反序列化成空列表而不是None/报错
    os.remove(db)


def test_complete_run_sets_status_and_completed_at():
    db = _tmp_db()
    storage.init_db(db)
    run_id = storage.create_run("cn", "DeepSeek", "quick_20", 1, db_path=db)
    storage.add_item(run_id, _sample_item("q1"), db_path=db)
    storage.complete_run(run_id, db_path=db)
    run = storage.get_run(run_id, db_path=db)
    assert run["status"] == "completed"
    assert run["completed_at"]
    os.remove(db)


def test_data_persists_across_new_connection():
    """模拟"浏览器刷新后历史仍存在"：数据必须能被一次全新的数据库连接读到，
    而不是只存在于内存里。"""
    db = _tmp_db()
    storage.init_db(db)
    run_id = storage.create_run("cn", "DeepSeek", "quick_20", 1, db_path=db)
    storage.add_item(run_id, _sample_item("q1"), db_path=db)
    storage.complete_run(run_id, db_path=db)

    # 完全独立的一次读取（模拟新的HTTP请求/新的浏览器会话），
    # 不复用上面任何连接对象或Python变量状态。
    reloaded_run = storage.get_run(run_id, db_path=db)
    reloaded_items = storage.get_run_items(run_id, db_path=db)
    assert reloaded_run is not None
    assert reloaded_run["status"] == "completed"
    assert len(reloaded_items) == 1
    assert reloaded_items[0]["question"] == "q1"
    os.remove(db)


def test_list_runs_ordered_by_created_at_desc():
    db = _tmp_db()
    storage.init_db(db)
    run1 = storage.create_run("cn", "DeepSeek", "quick_20", 1, db_path=db)
    run2 = storage.create_run("cn", "DeepSeek", "quick_20", 1, db_path=db)
    runs = storage.list_runs(db_path=db)
    assert runs[0]["id"] == run2  # 最新创建的排在最前
    assert runs[1]["id"] == run1
    os.remove(db)


def test_get_latest_successful_run_ignores_running_runs():
    db = _tmp_db()
    storage.init_db(db)
    run1 = storage.create_run("cn", "DeepSeek", "quick_20", 1, db_path=db)
    storage.complete_run(run1, db_path=db)
    storage.create_run("cn", "DeepSeek", "quick_20", 1, db_path=db)  # 未完成的run2

    latest = storage.get_latest_successful_run(db_path=db)
    assert latest["id"] == run1  # 只看completed状态的run，未完成的不算


def test_get_latest_successful_run_returns_none_when_empty():
    db = _tmp_db()
    storage.init_db(db)
    assert storage.get_latest_successful_run(db_path=db) is None
    os.remove(db)


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
    if failures:
        raise SystemExit(1)
