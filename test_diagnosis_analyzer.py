"""
diagnosis_analyzer.py 的离线自动化测试。
只做确定性断言，不依赖网络或DeepSeek真实调用。
运行方式：python test_diagnosis_analyzer.py
"""

from brand_parser import parse_geo_answer
from diagnosis_analyzer import diagnose, GAP_ACTION_MAP, MANUFACTURER_INTENTS


def _run(question, raw_answer, model="deepseek-v4-flash"):
    parsed = parse_geo_answer(raw_answer, model)
    return parsed, diagnose(question, raw_answer, parsed)


# ---------------------------------------------------------------------------
# 1. 品牌未出现
# ---------------------------------------------------------------------------

def test_brand_absent_produces_brand_absence_gap():
    parsed, d = _run(
        "车载冰箱哪个品牌好？",
        "车载冰箱推荐以下品牌：\n1. 英得尔——性价比高\n2. 冰虎——制冷效果好",
    )
    assert parsed["brand_mentioned"] is False
    gap_types = [g["type"] for g in d["gaps"]]
    assert "BRAND_ABSENCE" in gap_types
    # 证据必须是具体陈述，不能是空话
    gap = next(g for g in d["gaps"] if g["type"] == "BRAND_ABSENCE")
    assert "普能达" in gap["evidence"] or "PUNEDA" in gap["evidence"]


# ---------------------------------------------------------------------------
# 2. 品牌出现但未推荐
# ---------------------------------------------------------------------------

def test_brand_mentioned_not_recommended_produces_recommendation_gap():
    parsed, d = _run(
        "车载冰箱哪个品牌好？",
        "普能达是一家生产车载冰箱的厂家，产品线比较齐全。",
    )
    assert parsed["brand_mentioned"] is True
    assert parsed["recommended"] is False
    gap_types = [g["type"] for g in d["gaps"]]
    assert "RECOMMENDATION_GAP" in gap_types
    assert "BRAND_ABSENCE" not in gap_types


# ---------------------------------------------------------------------------
# 3. 品牌进入Top3（不应触发RANK_GAP，rank本身即推荐信号）
# ---------------------------------------------------------------------------

def test_brand_in_top3_no_rank_gap():
    parsed, d = _run(
        "车载冰箱哪个品牌好？",
        "车载冰箱推荐：\n1. 普能达——性价比高\n2. 英得尔——口碑不错\n3. 冰虎——制冷快",
    )
    assert parsed["rank"] == 1
    assert parsed["recommended"] is True
    gap_types = [g["type"] for g in d["gaps"]]
    assert "RANK_GAP" not in gap_types
    assert "RECOMMENDATION_GAP" not in gap_types
    assert "BRAND_ABSENCE" not in gap_types


# ---------------------------------------------------------------------------
# 4. 厂家问题被回答成消费品牌
# ---------------------------------------------------------------------------

def test_manufacturer_query_answered_as_consumer_brand():
    parsed, d = _run(
        "车载冰箱厂家推荐",
        "以下几个品牌口碑不错，值得考虑：英得尔、冰虎、科敏，性价比都很高，用户评价不错。",
    )
    assert d["query_intent"] == "manufacturer"
    assert d["answer_fit"] == "partial"
    gap_types = [g["type"] for g in d["gaps"]]
    assert "QUERY_INTENT_MISMATCH" in gap_types


# ---------------------------------------------------------------------------
# 5. 无 Citation
# ---------------------------------------------------------------------------

def test_no_citation_produces_citation_gap():
    parsed, d = _run("车载冰箱哪个品牌好？", "普能达是不错的品牌，值得推荐。")
    assert parsed["citations"] == []
    gap_types = [g["type"] for g in d["gaps"]]
    assert "CITATION_GAP" in gap_types


# ---------------------------------------------------------------------------
# 6. 有 Citation（不应触发CITATION_GAP；权威性判断基于具体证据）
# ---------------------------------------------------------------------------

def test_citation_present_no_citation_gap():
    parsed, d = _run(
        "车载冰箱哪个品牌好？",
        "普能达值得推荐。参考：https://www.pnda.com.cn/about",
    )
    assert len(parsed["citations"]) == 1
    gap_types = [g["type"] for g in d["gaps"]]
    assert "CITATION_GAP" not in gap_types


def test_citation_all_low_authority_produces_source_authority_gap():
    """全部引用来源都命中已知的UGC/论坛域名时，才下"权威性不足"的结论，
    这是有具体证据支撑的判断，不是凭空猜测。"""
    parsed, d = _run(
        "车载冰箱哪个品牌好？",
        "普能达值得推荐。参考：https://www.zhihu.com/question/123",
    )
    gap_types = [g["type"] for g in d["gaps"]]
    assert "SOURCE_AUTHORITY_GAP" in gap_types
    gap = next(g for g in d["gaps"] if g["type"] == "SOURCE_AUTHORITY_GAP")
    assert "zhihu.com" in gap["evidence"]


# ---------------------------------------------------------------------------
# 7. AI行业认知不足
# ---------------------------------------------------------------------------

def test_low_industry_knowledge_quality():
    parsed, d = _run(
        "车载冰箱厂家推荐",
        "以下几个品牌口碑不错，值得考虑：英得尔、冰虎，性价比都很高，用户评价不错。",
    )
    assert d["industry_knowledge_quality"] == "low"
    gap_types = [g["type"] for g in d["gaps"]]
    assert "INDUSTRY_KNOWLEDGE_GAP" in gap_types
    # reason必须是具体信号，不能是空泛评分
    assert d["industry_knowledge_reason"]
    assert "0" not in d["industry_knowledge_quality"]  # 确保不是数字打分


# ---------------------------------------------------------------------------
# 8. 无足够证据时返回 unknown
# ---------------------------------------------------------------------------

def test_insufficient_evidence_returns_unknown():
    parsed, d = _run("你好", "嗯")
    assert d["answer_fit"] == "unknown"
    assert d["industry_knowledge_quality"] == "unknown"


def test_unrelated_intent_with_no_industry_words_is_unknown():
    parsed, d = _run("你好，今天天气怎么样", "今天天气不错，适合出门。")
    assert d["query_intent"] == "other"
    assert d["answer_fit"] == "unknown"


# ---------------------------------------------------------------------------
# 9. 不允许把推断写成事实
# ---------------------------------------------------------------------------

def test_inferences_carry_evidence_boundary_language():
    parsed, d = _run(
        "车载冰箱厂家推荐",
        "以下几个品牌口碑不错：英得尔、冰虎，性价比都很高。",
    )
    hedge_words = ("可能", "更可能", "从当前回答看")
    assert d["inferences"], "应至少产生一条inference"
    for inf in d["inferences"]:
        assert any(w in inf for w in hedge_words), f"inference缺少证据边界词: {inf}"
    # diagnosis_summary 同样必须带边界词，不能写成确定性断言
    assert any(w in d["diagnosis_summary"] for w in hedge_words)
    # observations 是纯客观陈述，不应包含推断措辞
    for obs in d["observations"]:
        assert "可能" not in obs and "更可能" not in obs


def test_summary_length_roughly_in_range():
    parsed, d = _run(
        "车载冰箱厂家推荐",
        "以下几个品牌口碑不错：英得尔、冰虎，性价比都很高，用户评价不错。",
    )
    length = len(d["diagnosis_summary"])
    assert 60 <= length <= 260, f"诊断结论长度超出合理范围: {length}"


# ---------------------------------------------------------------------------
# 10. action_items 和 Gap 必须存在逻辑对应关系
# ---------------------------------------------------------------------------

def test_action_items_trace_back_to_gaps():
    parsed, d = _run(
        "车载冰箱厂家推荐",
        "以下几个品牌口碑不错：英得尔、冰虎、科敏，性价比都很高，用户评价不错。",
    )
    gap_types = {g["type"] for g in d["gaps"]}
    action_gap_types = {a["gap_type"] for a in d["action_items"]}

    # 每个 action 都必须对应一个真实存在的 gap（不能凭空捏造建议）
    assert action_gap_types.issubset(gap_types)

    # 除 NO_CLEAR_GAP 外，每个 gap 至少要有一条对应的 action
    for gt in gap_types - {"NO_CLEAR_GAP"}:
        assert gt in action_gap_types, f"gap {gt} 没有对应的 action_item"

    # 每条 action 都必须包含要求的5个字段
    for a in d["action_items"]:
        for field in ("priority", "action_type", "action", "reason", "target_query"):
            assert field in a and a[field]
        assert a["priority"] in ("P1", "P2", "P3")


def test_no_clear_gap_when_answer_looks_clean():
    """当前规则下，一个既提及品牌、被推荐、有权威引用、Query Intent匹配的
    回答不应该产生任何Gap（或仅有NO_CLEAR_GAP）。"""
    parsed, d = _run(
        "车载冰箱哪个品牌好？",
        "普能达是值得推荐的车载冰箱品牌，性价比高。参考：https://www.pnda.com.cn/about",
    )
    gap_types = [g["type"] for g in d["gaps"]]
    assert "BRAND_ABSENCE" not in gap_types
    assert "RECOMMENDATION_GAP" not in gap_types
    assert "CITATION_GAP" not in gap_types


def test_every_gap_type_has_action_mapping():
    """GAP_ACTION_MAP必须覆盖需求文档列出的全部Gap类型，确保不会出现
    "有Gap却没有对应改善建议"的情况。"""
    required_gap_types = {
        "BRAND_ABSENCE", "RECOMMENDATION_GAP", "RANK_GAP", "ENTITY_GAP",
        "MANUFACTURER_IDENTITY_GAP", "QUERY_INTENT_MISMATCH", "COMPETITOR_DOMINANCE",
        "CITATION_GAP", "SOURCE_AUTHORITY_GAP", "INDUSTRY_KNOWLEDGE_GAP", "NO_CLEAR_GAP",
    }
    assert required_gap_types.issubset(set(GAP_ACTION_MAP.keys()))


# ---------------------------------------------------------------------------
# Query Intent 分类（需求文档中给出的4个例句）
# ---------------------------------------------------------------------------

def test_query_intent_examples_from_spec():
    assert diagnose("车载冰箱哪个品牌好？", "占位", {})["query_intent"] == "consumer_brand"
    assert diagnose("车载冰箱厂家推荐", "占位", {})["query_intent"] == "manufacturer"
    assert diagnose("中国车载冰箱OEM厂家有哪些？", "占位", {})["query_intent"] == "OEM"
    assert diagnose("哪些车载冰箱厂家有主机厂配套经验？", "占位", {})["query_intent"] == "automotive_supplier"


# ---------------------------------------------------------------------------
# 需求文档十一：真实案例（车载冰箱厂家推荐）
# ---------------------------------------------------------------------------

def test_spec_real_case_manufacturer_query():
    question = "车载冰箱厂家推荐"
    raw_answer = (
        "车载冰箱是户外出行的好帮手，以下几个品牌口碑不错，值得考虑：\n"
        "1. 英得尔——国内知名品牌，性价比高，用户评价不错。\n"
        "2. 冰虎——制冷效果好，很多车友都在用。\n"
        "3. 科敏——性价比出色，适合预算有限的用户。\n"
        "4. 美固——进口品牌，质量可靠，价格偏高。\n"
        "购买时建议关注压缩机品牌和噪音水平，选择口碑较好的品牌更放心。"
    )
    parsed, d = _run(question, raw_answer)

    assert parsed["brand_mentioned"] is False
    assert d["query_intent"] == "manufacturer"
    assert d["answer_fit"] == "partial"

    gap_types = {g["type"] for g in d["gaps"]}
    assert {"BRAND_ABSENCE", "QUERY_INTENT_MISMATCH", "CITATION_GAP"}.issubset(gap_types)

    # 不允许把推断写成事实
    forbidden_assertions = ["确定因为", "一定是因为", "肯定是"]
    for phrase in forbidden_assertions:
        assert phrase not in d["diagnosis_summary"]
        for inf in d["inferences"]:
            assert phrase not in inf


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
