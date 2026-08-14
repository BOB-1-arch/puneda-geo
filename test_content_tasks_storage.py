"""
storage.py 里 content_tasks 相关函数的离线自动化测试。
每个测试用独立的临时db文件，互不干扰，测试完清理。
运行方式：python test_content_tasks_storage.py
"""

import os
import tempfile

import storage


def _tmp_db():
    return tempfile.mktemp(suffix=".db")


def _sample_fields(**overrides):
    fields = {
        "title": "车载冰箱OEM厂家专题", "content_cluster": "OEM_ODM",
        "content_type": "TOPIC_PAGE", "priority": "P1", "status": "planning",
        "target_query": "中国车载冰箱OEM厂家有哪些？",
        "target_queries": ["中国车载冰箱OEM厂家有哪些？"],
        "query_intent": "manufacturer", "commercial_value": "high",
        "geo_gaps": ["BRAND_ABSENCE"], "facts_required": ["OEM能力", "认证"],
        "entity_facts": [{"name": "工厂面积", "content": "", "source": "", "verified": False}],
        "outline": [{"level": "H1", "text": "标题"}],
    }
    fields.update(overrides)
    return fields


def test_create_and_get_content_task_round_trip():
    db = _tmp_db()
    storage.init_db(db)
    task_id = storage.create_content_task(_sample_fields(), db_path=db)
    task = storage.get_content_task(task_id, db_path=db)
    assert task["title"] == "车载冰箱OEM厂家专题"
    assert task["target_queries"] == ["中国车载冰箱OEM厂家有哪些？"]
    assert task["geo_gaps"] == ["BRAND_ABSENCE"]
    assert task["entity_facts"][0]["name"] == "工厂面积"
    assert task["outline"][0]["level"] == "H1"
    assert task["created_at"] and task["updated_at"]
    os.remove(db)


def test_default_status_values_are_correct():
    db = _tmp_db()
    storage.init_db(db)
    task_id = storage.create_content_task(
        {"title": "x", "content_cluster": "ENTITY", "content_type": "ENTITY_PAGE", "priority": "P2"},
        db_path=db,
    )
    task = storage.get_content_task(task_id, db_path=db)
    assert task["status"] == "planning"
    assert task["index_status"] == "unchecked"
    assert task["retest_status"] == "not_scheduled"
    os.remove(db)


def test_update_content_task_only_changes_passed_fields():
    db = _tmp_db()
    storage.init_db(db)
    task_id = storage.create_content_task(_sample_fields(), db_path=db)
    before = storage.get_content_task(task_id, db_path=db)
    ok = storage.update_content_task(task_id, {"status": "writing"}, db_path=db)
    after = storage.get_content_task(task_id, db_path=db)
    assert ok is True
    assert after["status"] == "writing"
    assert after["title"] == before["title"]  # 没传的字段保持不变
    assert after["updated_at"] >= before["updated_at"]
    os.remove(db)


def test_update_nonexistent_task_returns_false():
    db = _tmp_db()
    storage.init_db(db)
    ok = storage.update_content_task(999, {"status": "writing"}, db_path=db)
    assert ok is False
    os.remove(db)


def test_delete_content_task_does_not_touch_diagnosis_tables():
    """删内容任务绝对不能连带删掉它引用的原始诊断记录。"""
    db = _tmp_db()
    storage.init_db(db)
    run_id = storage.create_run("cn", "DeepSeek", "quick_20", 1, db_path=db)
    storage.add_item(run_id, {
        "question": "q1", "query_intent": "manufacturer", "commercial_value": "high",
        "platform": "DeepSeek", "status": "success", "brand_mentioned": False,
    }, db_path=db)
    task_id = storage.create_content_task(
        _sample_fields(source_diagnosis_id=run_id, source_diagnosis_item_ids=[1]), db_path=db
    )

    deleted = storage.delete_content_task(task_id, db_path=db)
    assert deleted is True
    assert storage.get_content_task(task_id, db_path=db) is None

    # 原始诊断记录必须完好无损
    run = storage.get_run(run_id, db_path=db)
    items = storage.get_run_items(run_id, db_path=db)
    assert run is not None
    assert len(items) == 1
    os.remove(db)


def test_delete_nonexistent_task_returns_false():
    db = _tmp_db()
    storage.init_db(db)
    assert storage.delete_content_task(999, db_path=db) is False
    os.remove(db)


def test_list_content_tasks_filters_by_priority_and_status():
    db = _tmp_db()
    storage.init_db(db)
    storage.create_content_task(_sample_fields(priority="P1", status="planning"), db_path=db)
    storage.create_content_task(_sample_fields(priority="P2", status="writing"), db_path=db)
    storage.create_content_task(_sample_fields(priority="P1", status="writing"), db_path=db)

    p1_tasks = storage.list_content_tasks(priority="P1", db_path=db)
    assert len(p1_tasks) == 2

    writing_tasks = storage.list_content_tasks(status="writing", db_path=db)
    assert len(writing_tasks) == 2

    p1_writing = storage.list_content_tasks(priority="P1", status="writing", db_path=db)
    assert len(p1_writing) == 1


def test_list_content_tasks_filters_by_geo_gap():
    db = _tmp_db()
    storage.init_db(db)
    storage.create_content_task(_sample_fields(geo_gaps=["BRAND_ABSENCE"]), db_path=db)
    storage.create_content_task(_sample_fields(geo_gaps=["CITATION_GAP"]), db_path=db)
    storage.create_content_task(_sample_fields(geo_gaps=["BRAND_ABSENCE", "CITATION_GAP"]), db_path=db)

    hits = storage.list_content_tasks(geo_gap="BRAND_ABSENCE", db_path=db)
    assert len(hits) == 2


def test_list_content_tasks_search_matches_title_and_query():
    db = _tmp_db()
    storage.init_db(db)
    storage.create_content_task(_sample_fields(title="OEM厂家专题"), db_path=db)
    storage.create_content_task(_sample_fields(title="经销商采购指南", target_query="怎么找经销商"), db_path=db)

    hits = storage.list_content_tasks(search="OEM", db_path=db)
    assert len(hits) == 1
    hits2 = storage.list_content_tasks(search="经销商", db_path=db)
    assert len(hits2) == 1


def test_append_query_to_task_merges_instead_of_duplicating():
    db = _tmp_db()
    storage.init_db(db)
    task_id = storage.create_content_task(_sample_fields(), db_path=db)

    ok = storage.append_query_to_task(task_id, "车载冰箱OEM代工推荐哪些厂家？", diagnosis_item_id=42, db_path=db)
    assert ok is True
    task = storage.get_content_task(task_id, db_path=db)
    assert len(task["target_queries"]) == 2
    assert "车载冰箱OEM代工推荐哪些厂家？" in task["target_queries"]
    assert 42 in task["source_diagnosis_item_ids"]

    # 重复append同一个query不应该产生重复条目
    storage.append_query_to_task(task_id, "车载冰箱OEM代工推荐哪些厂家？", diagnosis_item_id=42, db_path=db)
    task2 = storage.get_content_task(task_id, db_path=db)
    assert len(task2["target_queries"]) == 2


def test_data_persists_across_fresh_connection_simulating_refresh():
    db = _tmp_db()
    storage.init_db(db)
    task_id = storage.create_content_task(_sample_fields(), db_path=db)

    # 完全独立的读取，模拟浏览器刷新后重新拉数据。
    reloaded = storage.get_content_task(task_id, db_path=db)
    assert reloaded is not None
    assert reloaded["title"] == "车载冰箱OEM厂家专题"
    os.remove(db)


def test_migration_keeps_existing_diagnosis_history_intact():
    """核心安全要求：老版本数据库升级到带content_tasks表的新schema，
    已有的诊断历史一条都不能丢，新表要能正常用。"""
    db = _tmp_db()
    storage.init_db(db)
    run_id = storage.create_run("cn", "DeepSeek", "quick_20", 1, db_path=db)
    storage.add_item(run_id, {
        "question": "旧数据", "query_intent": "manufacturer", "commercial_value": "high",
        "platform": "DeepSeek", "status": "success",
    }, db_path=db)
    storage.complete_run(run_id, db_path=db)

    # 再次调用init_db，模拟"用新代码启动一个已有旧数据的数据库"。
    storage.init_db(db)

    run = storage.get_run(run_id, db_path=db)
    items = storage.get_run_items(run_id, db_path=db)
    assert run["status"] == "completed"
    assert len(items) == 1
    assert items[0]["question"] == "旧数据"

    # 新表也已经可用。
    task_id = storage.create_content_task(_sample_fields(), db_path=db)
    assert storage.get_content_task(task_id, db_path=db) is not None
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
