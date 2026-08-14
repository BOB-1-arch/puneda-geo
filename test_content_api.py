"""
GEO内容矩阵 API 层的离线自动化测试（FastAPI TestClient，不依赖网络/真实DeepSeek Key）。

同样在 import main 之前把 GEO_DB_PATH 设成临时文件，和 test_deep_diagnosis_api.py
保持一致的隔离方式，不污染真实开发数据库。

运行方式：venv 里装了 fastapi/httpx 之后 python test_content_api.py
"""

import os
import tempfile

_TMP_DB = tempfile.mktemp(suffix=".db")
os.environ["GEO_DB_PATH"] = _TMP_DB
os.environ["DEEPSEEK_API_KEY"] = "sk-test-fake-key-not-real"

from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402

client = TestClient(main.app)


def _make_deep_item(question, query_intent, commercial_value, brand_mentioned=False, item_status="success"):
    gaps = (
        [{"type": "BRAND_ABSENCE", "label": "品牌未进入AI答案", "evidence": "..."}]
        if not brand_mentioned else
        [{"type": "NO_CLEAR_GAP", "label": "暂无明确Gap", "evidence": "..."}]
    )
    actions = (
        [{"priority": "P1", "action_type": "ENTITY_BUILDING", "action": "补充实体信息",
          "reason": "...", "target_query": question, "gap_type": "BRAND_ABSENCE"}]
        if not brand_mentioned else []
    )
    return {
        "question": question, "query_intent": query_intent, "commercial_value": commercial_value,
        "platform": "DeepSeek", "status": item_status,
        "raw_answer": "英得尔、冰虎不错。", "model": "deepseek-v4-flash",
        "tested_at": "2026-08-14 00:00:00",
        "brand_mentioned": brand_mentioned, "mention_count": 1 if brand_mentioned else 0,
        "recommended": False, "rank": None,
        "competitors": [{"name": "英得尔", "aliases": ["Indel B"], "confidence": "high", "evidence": "英得尔"}],
        "citations": [], "answer_fit": "partial", "industry_knowledge_quality": "low",
        "gaps": gaps, "diagnosis_summary": "...", "observations": ["..."], "inferences": ["..."],
        "actions": actions,
    }


def _run_diagnosis_with_items(items_spec):
    """items_spec: list[(question, query_intent, commercial_value, brand_mentioned)]"""
    r = client.post("/api/diagnose/deep/start", json={"market": "cn", "platform": "DeepSeek", "count": len(items_spec)})
    run_id = r.json()["run_id"]
    questions = r.json()["questions"]
    # 忽略题库真实抽到的题目内容，直接用测试自定义的问题覆盖（题目数量对齐即可）。
    for i, (question, intent, value, mentioned) in enumerate(items_spec):
        payload = _make_deep_item(question, intent, value, mentioned)
        r_item = client.post(f"/api/diagnose/deep/{run_id}/item", json=payload)
        assert r_item.status_code == 200, r_item.text
    client.post(f"/api/diagnose/deep/{run_id}/complete")
    items = client.get(f"/api/diagnose/deep/runs/{run_id}").json()["items"]
    return run_id, items


# ---------------------------------------------------------------------------
# 1. 从快速诊断（单条diagnose/geo结果）创建内容任务 —— 直接用 POST /api/content/tasks
# ---------------------------------------------------------------------------

def test_create_content_task_from_quick_diagnosis_result():
    r = client.post("/api/diagnose/geo", json={
        "question": "车载冰箱哪个牌子好？",
        "raw_answer": "推荐英得尔、冰虎，性价比都不错。", "model": "deepseek-v4-flash",
    })
    diag = r.json()
    r2 = client.post("/api/content/tasks", json={
        "title": "品牌实体页", "content_cluster": "ENTITY", "content_type": "ENTITY_PAGE",
        "priority": "P2", "target_query": "车载冰箱哪个牌子好？",
        "query_intent": diag["diagnosis"]["query_intent"],
        "baseline_brand_mentioned": diag["parsed"]["brand_mentioned"],
        "geo_gaps": [g["type"] for g in diag["diagnosis"]["gaps"]],
    })
    assert r2.status_code == 200
    assert r2.json()["task"]["baseline_brand_mentioned"] is False


# ---------------------------------------------------------------------------
# 2. 从深度诊断明细创建内容任务（预览 -> 确认创建）
# ---------------------------------------------------------------------------

def test_suggest_then_create_content_task_from_deep_diagnosis_item():
    run_id, items = _run_diagnosis_with_items([
        ("中国车载冰箱OEM厂家有哪些？", "manufacturer", "high", False),
    ])
    item_id = items[0]["id"]

    r = client.post("/api/content/tasks/suggest-from-diagnosis", json={
        "diagnosis_id": run_id, "diagnosis_item_id": item_id,
    })
    assert r.status_code == 200
    suggestion = r.json()["suggestion"]
    assert suggestion["content_cluster"] == "OEM_ODM"
    assert suggestion["priority"] == "P1"
    assert suggestion["source_diagnosis_id"] == run_id
    assert suggestion["source_diagnosis_item_ids"] == [item_id]

    r2 = client.post("/api/content/tasks", json=suggestion)
    assert r2.status_code == 200
    task = r2.json()["task"]
    assert task["source_diagnosis_id"] == run_id
    assert task["title"] == suggestion["title"]


# ---------------------------------------------------------------------------
# 3. 相似Query聚合（批量建议接口）
# ---------------------------------------------------------------------------

def test_batch_suggest_aggregates_similar_queries():
    run_id, items = _run_diagnosis_with_items([
        ("中国车载冰箱OEM厂家有哪些？", "manufacturer", "high", False),
        ("车载冰箱OEM代工推荐哪些厂家？", "OEM", "high", False),
        ("哪些车载冰箱厂家支持OEM？", "ODM", "high", False),
    ])
    r = client.post("/api/content/tasks/batch-suggest-from-diagnosis", json={"diagnosis_id": run_id})
    assert r.status_code == 200
    suggestions = r.json()["suggestions"]
    assert len(suggestions) == 1
    assert suggestions[0]["covered_query_count"] == 3


# ---------------------------------------------------------------------------
# 4. 重复任务提示（发现相似内容任务）
# ---------------------------------------------------------------------------

def test_duplicate_task_detection_via_suggest_endpoint():
    run_id, items = _run_diagnosis_with_items([
        ("车载冰箱厂家有哪些", "manufacturer", "high", False),
    ])
    # 先创建一个已有任务（Query措辞和上面这条明显重合，应该被判定为相似）
    client.post("/api/content/tasks", json={
        "title": "OEM厂家专题", "content_cluster": "OEM_ODM", "content_type": "TOPIC_PAGE",
        "priority": "P1", "target_query": "车载冰箱厂家推荐一下",
        "target_queries": ["车载冰箱厂家推荐一下"], "query_intent": "manufacturer",
    })
    r = client.post("/api/content/tasks/suggest-from-diagnosis", json={
        "diagnosis_id": run_id, "diagnosis_item_id": items[0]["id"],
    })
    similar = r.json()["similar_tasks"]
    assert len(similar) >= 1


def test_merge_into_existing_task_instead_of_creating_duplicate():
    r = client.post("/api/content/tasks", json={
        "title": "合并测试任务", "content_cluster": "AFTER_SALES", "content_type": "FAQ",
        "priority": "P3", "target_query": "车载冰箱质保多久",
        "target_queries": ["车载冰箱质保多久"],
    })
    task_id = r.json()["task"]["id"]

    r2 = client.post("/api/content/tasks", json={
        "title": "合并测试任务", "content_cluster": "AFTER_SALES", "content_type": "FAQ",
        "priority": "P3", "target_query": "车载冰箱保修期限是多久",
        "target_queries": ["车载冰箱保修期限是多久"],
        "merge_into_task_id": task_id,
    })
    assert r2.status_code == 200
    assert r2.json()["merged"] is True
    merged_task = r2.json()["task"]
    assert set(merged_task["target_queries"]) == {"车载冰箱质保多久", "车载冰箱保修期限是多久"}

    # 确认没有创建出第二条独立任务
    all_tasks = client.get("/api/content/tasks", params={"search": "合并测试任务"}).json()["tasks"]
    assert len(all_tasks) == 1


# ---------------------------------------------------------------------------
# 5. P1/P2/P3保存 & 6. 状态更新
# ---------------------------------------------------------------------------

def test_priority_and_status_save_and_update():
    r = client.post("/api/content/tasks", json={
        "title": "状态测试", "content_cluster": "ENTITY", "content_type": "ENTITY_PAGE", "priority": "P1",
    })
    task_id = r.json()["task"]["id"]
    assert r.json()["task"]["priority"] == "P1"
    assert r.json()["task"]["status"] == "planning"

    r2 = client.put(f"/api/content/tasks/{task_id}", json={"status": "writing", "priority": "P2"})
    assert r2.json()["task"]["status"] == "writing"
    assert r2.json()["task"]["priority"] == "P2"


def test_invalid_status_rejected():
    r = client.post("/api/content/tasks", json={
        "title": "非法状态", "content_cluster": "ENTITY", "content_type": "ENTITY_PAGE",
        "priority": "P1", "status": "not_a_real_status",
    })
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# 7. 内容大纲生成
# ---------------------------------------------------------------------------

def test_outline_generated_in_suggestion():
    run_id, items = _run_diagnosis_with_items([
        ("汽车主机厂配套车载冰箱供应商有哪些？", "automotive_supplier", "high", False),
    ])
    r = client.post("/api/content/tasks/suggest-from-diagnosis", json={
        "diagnosis_id": run_id, "diagnosis_item_id": items[0]["id"],
    })
    outline = r.json()["suggestion"]["outline"]
    assert outline[0]["level"] == "H1"
    assert any("EMC" in s["text"] for s in outline)


# ---------------------------------------------------------------------------
# 8. 未核实企业事实不能自动编
# ---------------------------------------------------------------------------

def test_facts_required_are_placeholders_not_fabricated_data():
    run_id, items = _run_diagnosis_with_items([
        ("中国车载冰箱OEM厂家有哪些？", "manufacturer", "high", False),
    ])
    r = client.post("/api/content/tasks/suggest-from-diagnosis", json={
        "diagnosis_id": run_id, "diagnosis_item_id": items[0]["id"],
    })
    suggestion = r.json()["suggestion"]
    assert suggestion["entity_facts"] == []  # 新任务的具体事实内容必须是空，等人工填
    assert "OEM能力" in suggestion["facts_required"]


# ---------------------------------------------------------------------------
# 9. 发布URL保存 & 10. 收录状态保存
# ---------------------------------------------------------------------------

def test_published_url_and_index_status_saved():
    r = client.post("/api/content/tasks", json={
        "title": "发布测试", "content_cluster": "ENTITY", "content_type": "ENTITY_PAGE", "priority": "P1",
    })
    task_id = r.json()["task"]["id"]

    r2 = client.put(f"/api/content/tasks/{task_id}", json={
        "status": "published", "published_url": "https://www.pnda.com.cn/oem",
    })
    task = r2.json()["task"]
    assert task["published_url"] == "https://www.pnda.com.cn/oem"
    assert task["published_at"] is not None  # 标记已发布时自动记时间，不需要用户手填

    r3 = client.put(f"/api/content/tasks/{task_id}", json={
        "index_status": "indexed", "index_notes": "已在百度搜到",
    })
    task2 = r3.json()["task"]
    assert task2["index_status"] == "indexed"
    assert task2["index_checked_at"] is not None
    assert task2["index_notes"] == "已在百度搜到"


def test_index_status_defaults_to_unchecked_never_auto_claims_indexed():
    """系统不能自己假设页面已被收录——新建任务的index_status必须是unchecked。"""
    r = client.post("/api/content/tasks", json={
        "title": "收录测试", "content_cluster": "ENTITY", "content_type": "ENTITY_PAGE", "priority": "P1",
    })
    assert r.json()["task"]["index_status"] == "unchecked"


# ---------------------------------------------------------------------------
# 11. 复测日期保存 & 12. 一键复测 & 13. baseline/retest对比
# ---------------------------------------------------------------------------

def test_retest_due_date_saved():
    r = client.post("/api/content/tasks", json={
        "title": "复测日期测试", "content_cluster": "ENTITY", "content_type": "ENTITY_PAGE", "priority": "P1",
    })
    task_id = r.json()["task"]["id"]
    r2 = client.put(f"/api/content/tasks/{task_id}", json={
        "status": "waiting_retest", "retest_status": "waiting", "retest_due_at": "2026-09-14",
    })
    assert r2.json()["task"]["retest_due_at"] == "2026-09-14"
    assert r2.json()["task"]["retest_status"] == "waiting"


def test_one_click_retest_saves_snapshot_and_compares_to_baseline():
    r = client.post("/api/content/tasks", json={
        "title": "复测对比测试", "content_cluster": "OEM_ODM", "content_type": "TOPIC_PAGE", "priority": "P1",
        "target_queries": ["车载冰箱OEM厂家有哪些"],
        "baseline_brand_mentioned": False, "baseline_recommended": False,
        "baseline_snapshot": {"per_query": [{"query": "车载冰箱OEM厂家有哪些", "brand_mentioned": False, "recommended": False}]},
    })
    task_id = r.json()["task"]["id"]

    r2 = client.post(f"/api/content/tasks/{task_id}/retest", json={
        "results": [{"query": "车载冰箱OEM厂家有哪些", "brand_mentioned": True, "recommended": True, "rank": 2}],
    })
    assert r2.status_code == 200
    body = r2.json()
    assert body["task"]["status"] == "retested"
    assert body["task"]["retest_status"] == "completed"
    assert body["task"]["retest_rank"] == 2
    assert body["comparison"]["verdict"] == "结果改善"


def test_retest_requires_non_empty_results():
    r = client.post("/api/content/tasks", json={
        "title": "空复测测试", "content_cluster": "ENTITY", "content_type": "ENTITY_PAGE", "priority": "P1",
    })
    task_id = r.json()["task"]["id"]
    r2 = client.post(f"/api/content/tasks/{task_id}/retest", json={"results": []})
    assert r2.status_code == 400


# ---------------------------------------------------------------------------
# 14. 一个内容关联多个Query
# ---------------------------------------------------------------------------

def test_task_can_have_multiple_target_queries_and_retest_all():
    r = client.post("/api/content/tasks", json={
        "title": "多Query测试", "content_cluster": "OEM_ODM", "content_type": "TOPIC_PAGE", "priority": "P1",
        "target_queries": ["q1", "q2", "q3", "q4", "q5"],
        "baseline_snapshot": {"per_query": [{"query": f"q{i}", "brand_mentioned": False} for i in range(1, 6)]},
    })
    task_id = r.json()["task"]["id"]
    assert len(r.json()["task"]["target_queries"]) == 5

    r2 = client.post(f"/api/content/tasks/{task_id}/retest", json={
        "results": [
            {"query": "q1", "brand_mentioned": True}, {"query": "q2", "brand_mentioned": True},
            {"query": "q3", "brand_mentioned": False}, {"query": "q4", "brand_mentioned": False},
            {"query": "q5", "brand_mentioned": False},
        ],
    })
    comparison = r2.json()["comparison"]
    assert comparison["total_queries"] == 5
    assert comparison["baseline_mentioned_count"] == 0
    assert comparison["retest_mentioned_count"] == 2


# ---------------------------------------------------------------------------
# 15. SQLite升级后历史诊断数据不丢失（API层再验证一次）
# ---------------------------------------------------------------------------

def test_existing_diagnosis_history_survives_content_matrix_usage():
    run_id, _ = _run_diagnosis_with_items([("测试问题", "manufacturer", "high", False)])
    client.post("/api/content/tasks", json={
        "title": "不影响历史测试", "content_cluster": "ENTITY", "content_type": "ENTITY_PAGE", "priority": "P1",
    })
    r = client.get(f"/api/diagnose/deep/runs/{run_id}")
    assert r.status_code == 200
    assert len(r.json()["items"]) == 1


# ---------------------------------------------------------------------------
# 16. 内容矩阵筛选
# ---------------------------------------------------------------------------

def test_content_tasks_filtering_by_multiple_dimensions():
    client.post("/api/content/tasks", json={
        "title": "筛选测试A", "content_cluster": "OEM_ODM", "content_type": "TOPIC_PAGE",
        "priority": "P1", "query_intent": "manufacturer", "commercial_value": "high",
        "geo_gaps": ["BRAND_ABSENCE"],
    })
    client.post("/api/content/tasks", json={
        "title": "筛选测试B", "content_cluster": "AFTER_SALES", "content_type": "FAQ",
        "priority": "P3", "query_intent": "after_sales", "commercial_value": "low",
        "geo_gaps": ["CITATION_GAP"],
    })
    r = client.get("/api/content/tasks", params={"content_cluster": "OEM_ODM", "commercial_value": "high"})
    titles = [t["title"] for t in r.json()["tasks"]]
    assert "筛选测试A" in titles
    assert "筛选测试B" not in titles


# ---------------------------------------------------------------------------
# 17. 内容矩阵Dashboard统计
# ---------------------------------------------------------------------------

def test_content_dashboard_reflects_real_created_tasks():
    before = client.get("/api/content/dashboard").json()["total_tasks"]
    client.post("/api/content/tasks", json={
        "title": "Dashboard测试", "content_cluster": "ENTITY", "content_type": "ENTITY_PAGE", "priority": "P1",
    })
    after = client.get("/api/content/dashboard").json()["total_tasks"]
    assert after == before + 1


# ---------------------------------------------------------------------------
# 18. 删除内容任务不能删除原始诊断
# ---------------------------------------------------------------------------

def test_deleting_content_task_does_not_delete_source_diagnosis():
    run_id, items = _run_diagnosis_with_items([("删除测试问题", "manufacturer", "high", False)])
    r = client.post("/api/content/tasks", json={
        "title": "待删除任务", "content_cluster": "OEM_ODM", "content_type": "TOPIC_PAGE",
        "priority": "P1", "source_diagnosis_id": run_id, "source_diagnosis_item_ids": [items[0]["id"]],
    })
    task_id = r.json()["task"]["id"]

    r2 = client.delete(f"/api/content/tasks/{task_id}")
    assert r2.status_code == 200
    assert client.get(f"/api/content/tasks/{task_id}").status_code == 404

    r3 = client.get(f"/api/diagnose/deep/runs/{run_id}")
    assert r3.status_code == 200
    assert len(r3.json()["items"]) == 1


# ---------------------------------------------------------------------------
# 19. 页面刷新数据仍存在
# ---------------------------------------------------------------------------

def test_content_task_survives_fresh_requests_simulating_refresh():
    r = client.post("/api/content/tasks", json={
        "title": "刷新测试", "content_cluster": "ENTITY", "content_type": "ENTITY_PAGE", "priority": "P1",
    })
    task_id = r.json()["task"]["id"]
    r1 = client.get(f"/api/content/tasks/{task_id}")
    r2 = client.get(f"/api/content/tasks/{task_id}")
    assert r1.json()["task"] == r2.json()["task"]


# ---------------------------------------------------------------------------
# 20. 不存在API Key泄露
# ---------------------------------------------------------------------------

def test_no_api_key_leaks_in_any_content_endpoint_response():
    fake_key = os.environ["DEEPSEEK_API_KEY"]
    r = client.post("/api/content/tasks", json={
        "title": "Key泄露测试", "content_cluster": "ENTITY", "content_type": "ENTITY_PAGE", "priority": "P1",
    })
    assert fake_key not in r.text

    r2 = client.get("/api/content/dashboard")
    assert fake_key not in r2.text

    r3 = client.get("/api/content/meta")
    assert fake_key not in r3.text

    r4 = client.get("/api/content/tasks")
    assert fake_key not in r4.text


# ---------------------------------------------------------------------------
# 批量创建接口（batch-from-diagnosis）本身
# ---------------------------------------------------------------------------

def test_batch_create_from_diagnosis_creates_multiple_real_tasks():
    run_id, items = _run_diagnosis_with_items([
        ("中国车载冰箱OEM厂家有哪些？", "manufacturer", "high", False),
        ("汽车主机厂配套车载冰箱供应商有哪些？", "automotive_supplier", "high", False),
    ])
    r = client.post("/api/content/tasks/batch-suggest-from-diagnosis", json={"diagnosis_id": run_id})
    suggestions = r.json()["suggestions"]
    assert len(suggestions) == 2

    r2 = client.post("/api/content/tasks/batch-from-diagnosis", json={"suggestions": suggestions})
    assert r2.status_code == 200
    assert r2.json()["created_count"] == 2
    for task in r2.json()["tasks"]:
        assert task["source_diagnosis_id"] == run_id


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
