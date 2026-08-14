"""
content_matrix.py 的离线自动化测试。纯函数测试，不碰数据库/网络。
运行方式：python test_content_matrix.py
"""

import re

import content_matrix as cm


def _item(question, query_intent="manufacturer", commercial_value="high",
          brand_mentioned=False, recommended=False, rank=None, status="success",
          gaps=None, actions=None, competitors=None, citations=None, item_id=1):
    return {
        "id": item_id, "diagnosis_id": 1, "question": question,
        "query_intent": query_intent, "commercial_value": commercial_value,
        "status": status, "brand_mentioned": brand_mentioned, "recommended": recommended,
        "rank": rank, "competitors": competitors or [], "citations": citations or [],
        "gaps": gaps if gaps is not None else [{"type": "BRAND_ABSENCE", "label": "品牌未进入AI答案", "evidence": "..."}],
        "actions": actions if actions is not None else [
            {"priority": "P1", "action_type": "ENTITY_BUILDING", "action": "...", "reason": "..."}
        ],
    }


# ---------------------------------------------------------------------------
# 内容集群 / 内容类型 推断
# ---------------------------------------------------------------------------

def test_manufacturer_oem_odm_intents_all_map_to_oem_odm_cluster():
    for intent in ("manufacturer", "OEM", "ODM"):
        assert cm.infer_content_cluster(intent) == "OEM_ODM"


def test_automotive_supplier_maps_to_automotive_cluster():
    assert cm.infer_content_cluster("automotive_supplier") == "AUTOMOTIVE"


def test_distributor_procurement_maps_to_distributor_cluster():
    assert cm.infer_content_cluster("distributor_procurement") == "DISTRIBUTOR"


def test_consumer_intent_with_brand_absence_gap_maps_to_entity_cluster():
    cluster = cm.infer_content_cluster("consumer_brand", ["BRAND_ABSENCE"])
    assert cluster == "ENTITY"


def test_pure_industry_knowledge_gap_maps_to_industry_cluster():
    cluster = cm.infer_content_cluster("other", ["INDUSTRY_KNOWLEDGE_GAP"])
    assert cluster == "INDUSTRY"


def test_content_type_inferred_consistently_from_cluster():
    assert cm.infer_content_type("OEM_ODM") == "TOPIC_PAGE"
    assert cm.infer_content_type("ENTITY") == "ENTITY_PAGE"
    assert cm.infer_content_type("DISTRIBUTOR") == "PROCUREMENT_GUIDE"


# ---------------------------------------------------------------------------
# 优先级推断（优先复用diagnosis_analyzer的action priority）
# ---------------------------------------------------------------------------

def test_priority_reuses_existing_action_priority_first():
    # 即使intent/commercial_value看起来像P2/P3，只要action已经算出P1，就应该用P1。
    priority = cm.infer_priority("usage", "low", action_priorities=["P1", "P2"])
    assert priority == "P1"


def test_priority_falls_back_to_high_value_intent_rule():
    priority = cm.infer_priority("manufacturer", "medium", action_priorities=None)
    assert priority == "P1"


def test_priority_falls_back_to_p3_for_low_value_generic_intent():
    priority = cm.infer_priority("after_sales", "low", action_priorities=None)
    assert priority == "P3"


def test_all_high_value_b2b_intents_are_p1_by_rule():
    for intent in cm.HIGH_VALUE_INTENTS:
        assert cm.infer_priority(intent, "low", None) == "P1"


# ---------------------------------------------------------------------------
# 标题建议：不做标题党
# ---------------------------------------------------------------------------

def test_title_suggestions_avoid_clickbait_ranking_language():
    banned_phrases = ["十大", "最强", "排名第一", "全网最"]
    for cluster, _ in cm.CONTENT_CLUSTERS:
        title, alts = cm.suggest_titles("测试Query", cluster)
        for text in [title] + alts:
            for phrase in banned_phrases:
                assert phrase not in text, f"{cluster} 的标题里出现了标题党用语: {text}"


def test_title_suggestion_returns_main_and_two_alternates_for_oem_odm():
    title, alts = cm.suggest_titles("中国车载冰箱OEM厂家有哪些？", "OEM_ODM")
    assert title
    assert len(alts) == 2


# ---------------------------------------------------------------------------
# 内容大纲：不编造具体企业事实
# ---------------------------------------------------------------------------

def test_outline_never_fabricates_specific_facts():
    """大纲模板里不能出现具体年份/面积/产能这类看起来像真实企业数据的内容，
    需要这些信息的地方必须用【待人工补充/核实】占位，不能自己编。
    """
    suspicious_patterns = [
        re.compile(r"[12]\d{3}年"),
        re.compile(r"\d+[,，]?\d*\s*[㎡平方米]"),
        re.compile(r"\d+\s*(万台|台/年|件/年)"),
    ]
    for cluster, _ in cm.CONTENT_CLUSTERS:
        outline = cm.suggest_outline("测试标题", cluster)
        for section in outline:
            for pattern in suspicious_patterns:
                assert not pattern.search(section["text"]), (
                    f"{cluster} 大纲里出现疑似编造的企业事实: {section['text']}"
                )


def test_outline_has_h1_matching_title():
    outline = cm.suggest_outline("车载冰箱OEM厂家怎么选？", "OEM_ODM")
    assert outline[0]["level"] == "H1"
    assert outline[0]["text"] == "车载冰箱OEM厂家怎么选？"


def test_facts_required_differ_by_cluster_and_are_not_fabricated_values():
    oem_facts = cm.facts_required_for_cluster("OEM_ODM")
    industry_facts = cm.facts_required_for_cluster("INDUSTRY")
    assert "OEM能力" in oem_facts
    assert oem_facts != industry_facts
    # facts_required 只能是"需要核实的事实名称"，不能是具体数值。
    for fact in oem_facts:
        assert not re.search(r"\d{4}", fact)


# ---------------------------------------------------------------------------
# 相似任务判断（跨intent同cluster也要能识别为相似）
# ---------------------------------------------------------------------------

def test_similar_queries_within_same_cluster_are_flagged():
    existing = [{
        "id": 1, "content_cluster": "OEM_ODM", "query_intent": "manufacturer",
        "target_queries": ["中国车载冰箱OEM厂家有哪些？"], "target_query": "中国车载冰箱OEM厂家有哪些？",
    }]
    hits = cm.find_similar_tasks("车载冰箱OEM代工推荐哪些厂家？", "OEM_ODM", "OEM", existing)
    assert len(hits) == 1
    assert hits[0]["similarity"] > 0


def test_similar_check_ignores_intent_mismatch_within_same_cluster():
    """"厂家寻找"(manufacturer)和"OEM"是不同intent但同属OEM_ODM集群，
    不能因为intent字面不同就判定成"不相似"，否则会漏掉本该合并的任务。
    """
    existing = [{
        "id": 1, "content_cluster": "OEM_ODM", "query_intent": "manufacturer",
        "target_queries": ["车载冰箱厂家有哪些"], "target_query": "车载冰箱厂家有哪些",
    }]
    hits = cm.find_similar_tasks("车载冰箱厂家推荐", "OEM_ODM", "OEM", existing)
    assert len(hits) == 1


def test_dissimilar_queries_not_flagged():
    existing = [{
        "id": 1, "content_cluster": "OEM_ODM", "query_intent": "manufacturer",
        "target_queries": ["中国车载冰箱OEM厂家有哪些？"], "target_query": "中国车载冰箱OEM厂家有哪些？",
    }]
    hits = cm.find_similar_tasks("车载冰箱怎么保养？", "AFTER_SALES", "after_sales", existing)
    assert hits == []


def test_different_cluster_never_flagged_as_similar_even_if_wording_close():
    existing = [{
        "id": 1, "content_cluster": "AFTER_SALES", "query_intent": "after_sales",
        "target_queries": ["车载冰箱怎么保养？"], "target_query": "车载冰箱怎么保养？",
    }]
    hits = cm.find_similar_tasks("车载冰箱怎么用？", "SCENARIO", "usage", existing)
    assert hits == []


# ---------------------------------------------------------------------------
# 单条建议构建（预览用）
# ---------------------------------------------------------------------------

def test_build_task_suggestion_from_diagnosis_item():
    item = _item("中国车载冰箱OEM厂家有哪些？")
    s = cm.build_task_suggestion(item, run_id=7)
    assert s["content_cluster"] == "OEM_ODM"
    assert s["priority"] == "P1"
    assert s["source_diagnosis_id"] == 7
    assert s["source_diagnosis_item_ids"] == [1]
    assert s["target_queries"] == ["中国车载冰箱OEM厂家有哪些？"]
    assert "BRAND_ABSENCE" in s["geo_gaps"]
    assert s["baseline_snapshot"]["per_query"][0]["brand_mentioned"] is False


# ---------------------------------------------------------------------------
# 批量聚合：不能20题生成20篇，同cluster不同intent也要合并
# ---------------------------------------------------------------------------

def test_batch_aggregation_merges_similar_queries_not_one_per_question():
    items = [
        _item("中国车载冰箱OEM厂家有哪些？", query_intent="manufacturer", item_id=1),
        _item("车载冰箱OEM代工推荐哪些厂家？", query_intent="OEM", item_id=2),
        _item("哪些车载冰箱厂家支持OEM？", query_intent="ODM", item_id=3),
    ]
    suggestions = cm.cluster_diagnosis_items_into_suggestions(items, run_id=1)
    assert len(suggestions) == 1
    assert suggestions[0]["covered_query_count"] == 3
    assert set(suggestions[0]["target_queries"]) == {
        "中国车载冰箱OEM厂家有哪些？", "车载冰箱OEM代工推荐哪些厂家？", "哪些车载冰箱厂家支持OEM？",
    }


def test_batch_aggregation_produces_no_duplicate_titles():
    """回归测试：分组逻辑曾经错误地按 (cluster, intent) 分组，导致同一个OEM_ODM
    专题因为intent不同被拆成好几条一模一样标题的建议。这里验证修好之后
    标题不重复。
    """
    items = [
        _item("q1", query_intent="manufacturer", item_id=1),
        _item("q2", query_intent="OEM", item_id=2),
        _item("q3", query_intent="ODM", item_id=3),
        _item("q4", query_intent="manufacturer", item_id=4),
    ]
    suggestions = cm.cluster_diagnosis_items_into_suggestions(items, run_id=1)
    titles = [s["title"] for s in suggestions]
    assert len(titles) == len(set(titles))


def test_batch_aggregation_excludes_items_without_meaningful_gap():
    items = [
        _item("q1", gaps=[{"type": "NO_CLEAR_GAP", "label": "x", "evidence": "x"}]),
        _item("q2", status="failed", gaps=[]),
        _item("q3", brand_mentioned=True, gaps=[{"type": "BRAND_ABSENCE", "label": "x", "evidence": "x"}]),
    ]
    suggestions = cm.cluster_diagnosis_items_into_suggestions(items, run_id=1)
    # 只有 q3 命中了有意义的Gap（NO_CLEAR_GAP不算，失败题不算）
    assert len(suggestions) == 1
    assert suggestions[0]["target_queries"] == ["q3"]


def test_batch_aggregation_different_clusters_stay_separate():
    items = [
        _item("q1", query_intent="manufacturer", item_id=1),
        _item("q2", query_intent="automotive_supplier", item_id=2),
    ]
    suggestions = cm.cluster_diagnosis_items_into_suggestions(items, run_id=1)
    clusters = {s["content_cluster"] for s in suggestions}
    assert clusters == {"OEM_ODM", "AUTOMOTIVE"}


def test_batch_aggregation_priority_ordering_p1_first():
    items = [
        _item("q1", query_intent="after_sales", commercial_value="low",
              gaps=[{"type": "BRAND_ABSENCE", "label": "x", "evidence": "x"}], actions=[]),
        _item("q2", query_intent="manufacturer", commercial_value="high", item_id=2),
    ]
    suggestions = cm.cluster_diagnosis_items_into_suggestions(items, run_id=1)
    assert suggestions[0]["priority"] == "P1"
    assert suggestions[0]["content_cluster"] == "OEM_ODM"


# ---------------------------------------------------------------------------
# Dashboard 聚合
# ---------------------------------------------------------------------------

def test_dashboard_stats_all_zero_when_no_tasks():
    stats = cm.compute_content_dashboard([])
    assert stats["total_tasks"] == 0
    assert stats["p1_count"] == 0
    assert stats["brand_absence_count"] == 0


def test_dashboard_stats_counts_real_tasks_not_fake_data():
    tasks = [
        {"priority": "P1", "status": "planning", "source_diagnosis_id": 1,
         "commercial_value": "high", "target_queries": ["q1", "q2"],
         "geo_gaps": ["BRAND_ABSENCE"], "retest_status": "not_scheduled"},
        {"priority": "P1", "status": "writing", "source_diagnosis_id": None,
         "commercial_value": "low", "target_queries": ["q3"],
         "geo_gaps": ["CITATION_GAP", "MANUFACTURER_IDENTITY_GAP"], "retest_status": "waiting"},
        {"priority": "P2", "status": "published", "source_diagnosis_id": 1,
         "commercial_value": "medium", "target_queries": [], "target_query": "q4",
         "geo_gaps": [], "retest_status": "not_scheduled"},
    ]
    stats = cm.compute_content_dashboard(tasks)
    assert stats["total_tasks"] == 3
    assert stats["p1_count"] == 2
    assert stats["p2_count"] == 1
    assert stats["status_counts"]["planning"] == 1
    assert stats["status_counts"]["published"] == 1
    assert stats["from_diagnosis_count"] == 2
    assert stats["high_value_query_count"] == 2  # 只有第一条是high且有2个query
    assert stats["brand_absence_count"] == 1
    assert stats["citation_gap_count"] == 1
    assert stats["manufacturer_identity_gap_count"] == 1
    assert stats["waiting_retest_count"] == 1


# ---------------------------------------------------------------------------
# Baseline vs Retest 对比
# ---------------------------------------------------------------------------

def test_baseline_vs_retest_improvement_detected():
    baseline = {"per_query": [{"query": "q1", "brand_mentioned": False, "recommended": False}]}
    retest = {"per_query": [{"query": "q1", "brand_mentioned": True, "recommended": True}]}
    result = cm.compare_baseline_retest(baseline, retest)
    assert result["verdict"] == "结果改善"


def test_baseline_vs_retest_no_change():
    baseline = {"per_query": [{"query": "q1", "brand_mentioned": True, "recommended": False}]}
    retest = {"per_query": [{"query": "q1", "brand_mentioned": True, "recommended": False}]}
    result = cm.compare_baseline_retest(baseline, retest)
    assert result["verdict"] == "无明显变化"


def test_baseline_vs_retest_regression_detected():
    baseline = {"per_query": [{"query": "q1", "brand_mentioned": True, "recommended": True}]}
    retest = {"per_query": [{"query": "q1", "brand_mentioned": False, "recommended": False}]}
    result = cm.compare_baseline_retest(baseline, retest)
    assert result["verdict"] == "结果下降"


def test_baseline_vs_retest_multi_query_breakdown():
    baseline = {"per_query": [
        {"query": "q1", "brand_mentioned": False}, {"query": "q2", "brand_mentioned": False},
        {"query": "q3", "brand_mentioned": False}, {"query": "q4", "brand_mentioned": False},
        {"query": "q5", "brand_mentioned": False},
    ]}
    retest = {"per_query": [
        {"query": "q1", "brand_mentioned": True}, {"query": "q2", "brand_mentioned": True},
        {"query": "q3", "brand_mentioned": False}, {"query": "q4", "brand_mentioned": False},
        {"query": "q5", "brand_mentioned": False},
    ]}
    result = cm.compare_baseline_retest(baseline, retest)
    assert result["total_queries"] == 5
    assert result["baseline_mentioned_count"] == 0
    assert result["retest_mentioned_count"] == 2


def test_baseline_vs_retest_before_retest_happens():
    baseline = {"per_query": [{"query": "q1", "brand_mentioned": False}]}
    result = cm.compare_baseline_retest(baseline, None)
    assert result["verdict"] == "尚未复测"


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
