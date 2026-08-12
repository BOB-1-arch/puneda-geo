"""
aggregate.py 的离线自动化测试。纯函数测试，不碰数据库/网络。
运行方式：python test_aggregate.py
"""

from aggregate import (
    compute_run_stats,
    aggregate_competitors,
    aggregate_gaps,
    aggregate_high_value,
    aggregate_action_items,
    build_full_report,
)


def _item(question, status="success", commercial_value="medium", query_intent="consumer_brand",
          brand_mentioned=False, mention_count=0, recommended=False, rank=None,
          competitors=None, citations=None, gaps=None, actions=None, error_message=None):
    return {
        "question": question, "status": status, "commercial_value": commercial_value,
        "query_intent": query_intent, "brand_mentioned": brand_mentioned,
        "mention_count": mention_count, "recommended": recommended, "rank": rank,
        "competitors": competitors or [], "citations": citations or [],
        "gaps": gaps or [], "actions": actions or [], "error_message": error_message,
    }


def _comp(name, aliases=None, confidence="high"):
    """构造 brand_parser 现在产出的结构化竞品数据 {name, aliases, confidence, evidence}。"""
    return {"name": name, "aliases": aliases or [], "confidence": confidence, "evidence": name}


# 1. 20题完整成功
def test_20_questions_all_success():
    items = [_item(f"q{i}", brand_mentioned=(i % 2 == 0)) for i in range(20)]
    stats = compute_run_stats(items)
    assert stats["total_questions"] == 20
    assert stats["success_count"] == 20
    assert stats["failed_count"] == 0
    assert stats["brand_mentioned_count"] == 10
    assert stats["brand_mention_rate"] == 50.0


# 2. 其中3题API失败
def test_3_questions_failed_among_20():
    items = [_item(f"q{i}") for i in range(17)] + [
        _item(f"fail{i}", status="failed", error_message="超时") for i in range(3)
    ]
    stats = compute_run_stats(items)
    assert stats["total_questions"] == 20
    assert stats["success_count"] == 17
    assert stats["failed_count"] == 3
    # 失败题不计入提及率等分母
    assert stats["brand_mention_rate"] == 0.0


# 3. 全部品牌未提及
def test_all_brand_not_mentioned():
    items = [_item(f"q{i}", brand_mentioned=False) for i in range(5)]
    stats = compute_run_stats(items)
    assert stats["brand_mentioned_count"] == 0
    assert stats["brand_mention_rate"] == 0.0


# 4. 部分品牌提及
def test_partial_brand_mentioned():
    items = [_item("q1", brand_mentioned=True), _item("q2", brand_mentioned=False),
              _item("q3", brand_mentioned=True), _item("q4", brand_mentioned=False)]
    stats = compute_run_stats(items)
    assert stats["brand_mentioned_count"] == 2
    assert stats["brand_mention_rate"] == 50.0


# 5. 全部rank=null
def test_all_rank_null_avg_rank_is_none_no_crash():
    items = [_item(f"q{i}", rank=None) for i in range(5)]
    stats = compute_run_stats(items)
    assert stats["avg_rank"] is None
    assert stats["top3_count"] == 0
    assert stats["top3_rate"] == 0.0


def test_avg_rank_only_over_ranked_items():
    items = [_item("q1", rank=1), _item("q2", rank=None), _item("q3", rank=5)]
    stats = compute_run_stats(items)
    assert stats["avg_rank"] == 3.0  # (1+5)/2，不把None算进去
    assert stats["top3_count"] == 1  # 只有rank=1进前3


# 6. 有Citation/无Citation混合
def test_citation_mixed():
    items = [_item("q1", citations=["http://a.com"]), _item("q2", citations=[])]
    stats = compute_run_stats(items)
    assert stats["citation_count"] == 1
    assert stats["citation_rate"] == 50.0


# 7. competitors重复聚合
def test_competitors_aggregated_across_items():
    items = [
        _item("q1", competitors=[_comp("英得尔"), _comp("冰虎")]),
        _item("q2", competitors=[_comp("英得尔")]),
        _item("q3", competitors=[_comp("英得尔"), _comp("科敏")]),
    ]
    top = aggregate_competitors(items)
    by_name = {c["name"]: c for c in top}
    assert by_name["英得尔"]["appearance_count"] == 3
    assert by_name["冰虎"]["appearance_count"] == 1
    assert by_name["科敏"]["appearance_count"] == 1
    # 按出现次数降序排列
    assert top[0]["name"] == "英得尔"


def test_competitors_early_mention_only_counts_first_three():
    items = [_item("q1", competitors=[_comp("A"), _comp("B"), _comp("C"), _comp("D"), _comp("E")])]
    top = aggregate_competitors(items)
    by_name = {c["name"]: c for c in top}
    assert by_name["A"]["early_mention_count"] == 1
    assert by_name["D"]["early_mention_count"] == 0  # 第4位，不算前3
    assert by_name["D"]["appearance_count"] == 1


def test_low_confidence_competitors_excluded_from_aggregation():
    """只统计 confidence=high 的竞品，medium/low 不进入TOP10聚合统计。"""
    items = [
        _item("q1", competitors=[_comp("英得尔", confidence="high"),
                                   _comp("某某疑似品牌", confidence="medium")]),
    ]
    top = aggregate_competitors(items)
    names = [c["name"] for c in top]
    assert names == ["英得尔"]
    assert "某某疑似品牌" not in names


def test_competitor_aliases_merged_not_counted_as_two_brands():
    """"冰虎"和"Alpicool"是同一个品牌的中英文名，跨题出现时必须归并成一条，
    不能在TOP10里算成两个不同的竞品。"""
    items = [
        _item("q1", competitors=[_comp("冰虎", aliases=["Alpicool"])]),
        _item("q2", competitors=[_comp("冰虎", aliases=["Alpicool"])]),
    ]
    top = aggregate_competitors(items)
    assert len(top) == 1
    assert top[0]["name"] == "冰虎"
    assert top[0]["appearance_count"] == 2
    assert "Alpicool" in top[0]["aliases"]


# 8. Gap重复聚合
def test_gaps_aggregated_with_question_list():
    items = [
        _item("q1", gaps=[{"type": "BRAND_ABSENCE", "label": "品牌未进入AI答案"}]),
        _item("q2", gaps=[{"type": "BRAND_ABSENCE", "label": "品牌未进入AI答案"}]),
        _item("q3", gaps=[{"type": "CITATION_GAP", "label": "缺少引用来源"}]),
        _item("q4", gaps=[{"type": "NO_CLEAR_GAP", "label": "暂无明确Gap"}]),
    ]
    gaps = aggregate_gaps(items)
    by_type = {g["type"]: g for g in gaps}
    assert by_type["BRAND_ABSENCE"]["count"] == 2
    assert set(by_type["BRAND_ABSENCE"]["questions"]) == {"q1", "q2"}
    assert by_type["CITATION_GAP"]["count"] == 1
    assert "NO_CLEAR_GAP" not in by_type  # NO_CLEAR_GAP不计入问题类Gap统计


# 9. Action去重
def test_action_items_deduplicated_and_counted():
    action = {"priority": "P1", "action_type": "ENTITY_BUILDING", "action": "补充实体信息",
              "reason": "r", "gap_type": "BRAND_ABSENCE"}
    items = [
        _item("q1", actions=[dict(action)]),
        _item("q2", actions=[dict(action)]),
        _item("q3", actions=[dict(action)]),
    ]
    merged = aggregate_action_items(items)
    assert len(merged) == 1  # 三题产生的同一条建议应合并成一条
    assert merged[0]["affected_queries_count"] == 3
    assert set(merged[0]["affected_queries"]) == {"q1", "q2", "q3"}


def test_action_items_different_actions_not_merged():
    items = [
        _item("q1", actions=[{"priority": "P1", "action_type": "ENTITY_BUILDING", "action": "A",
                                "reason": "r1", "gap_type": "BRAND_ABSENCE"}]),
        _item("q2", actions=[{"priority": "P2", "action_type": "THIRD_PARTY_MEDIA", "action": "B",
                                "reason": "r2", "gap_type": "CITATION_GAP"}]),
    ]
    merged = aggregate_action_items(items)
    assert len(merged) == 2
    # P1排在P2前面
    assert merged[0]["priority"] == "P1"


# 10. High Value专项统计
def test_high_value_only_scopes_to_high_commercial_value_items():
    items = [
        _item("q1", commercial_value="high", brand_mentioned=True),
        _item("q2", commercial_value="high", brand_mentioned=False),
        _item("q3", commercial_value="medium", brand_mentioned=True),  # 不应计入high value统计
    ]
    hv = aggregate_high_value(items)
    assert hv["high_value_count"] == 2
    assert hv["brand_mention_rate"] == 50.0  # 只算q1/q2两条，不含q3
    assert hv["uncovered_questions"] == ["q2"]


def test_high_value_with_no_high_items_returns_none_rates():
    items = [_item("q1", commercial_value="medium")]
    hv = aggregate_high_value(items)
    assert hv["high_value_count"] == 0
    assert hv["brand_mention_rate"] is None  # 没有高价值题时不能编造一个假的百分比


def test_build_full_report_smoke():
    items = [_item(f"q{i}") for i in range(3)]
    report = build_full_report(items)
    assert set(report.keys()) == {
        "stats", "competitors_top", "gaps_top", "query_intent_distribution",
        "high_value", "action_items",
    }


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
