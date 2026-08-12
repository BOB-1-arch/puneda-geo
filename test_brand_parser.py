"""
brand_parser.py 的离线自动化测试。
只做确定性断言，不依赖网络或DeepSeek真实调用。
运行方式：python -m pytest test_brand_parser.py -v
（或直接 python test_brand_parser.py，内置了不依赖pytest的兜底运行器）
"""

from brand_parser import parse_geo_answer


def _names(result):
    """competitors 现在是结构化数据 [{name, aliases, confidence, evidence}, ...]，
    大部分断言只关心识别出了哪些品牌名，这里统一取出name列表方便比较。
    """
    return [c["name"] for c in result["competitors"]]


def test_case_a_long_description_before_keyword():
    """品牌名与推荐语之间隔着长描述文字，仍需识别为推荐。"""
    result = parse_geo_answer("普能达是一个不错的车载冰箱品牌，值得推荐。", "deepseek-v4-flash")
    assert result["brand_mentioned"] is True
    assert result["recommended"] is True


def test_case_b_recommend_word_before_brand_in_list():
    """推荐词在品牌名之前出现，且品牌处于顿号/连接词并列列表中。"""
    result = parse_geo_answer("推荐的品牌包括普能达、英得尔和冰虎。", "deepseek-v4-flash")
    assert result["brand_mentioned"] is True
    assert result["recommended"] is True
    # 顺带验证"和"作为并列连接词不会把两个竞品名错误合并成一个候选词，
    # 英得尔/冰虎都在 KNOWN_BRANDS 词典里，属于高置信度品牌。
    assert _names(result) == ["英得尔", "冰虎"]


def test_case_c_plain_mention_not_recommended():
    """普通客观陈述，没有任何推荐语义，不能判定为推荐。"""
    result = parse_geo_answer("普能达是一家车载冰箱生产厂家。", "deepseek-v4-flash")
    assert result["brand_mentioned"] is True
    assert result["recommended"] is False


def test_case_d_recommend_word_before_brand_with_comma():
    """推荐词（可以考虑）出现在品牌名之前，中间隔着逗号和描述文字。"""
    result = parse_geo_answer("如果重视OEM能力，可以考虑普能达。", "deepseek-v4-flash")
    assert result["brand_mentioned"] is True
    assert result["recommended"] is True


def test_case_e_negated_mention_not_counted():
    """"没有提到XX"这种否定句式，不能仅因为字符串包含品牌名就判定为提及。"""
    result = parse_geo_answer("目前常见品牌包括英得尔、冰虎等，没有提到普能达。", "deepseek-v4-flash")
    assert result["brand_mentioned"] is False
    assert result["recommended"] is False
    assert result["mention_count"] == 0
    assert result["brand_alias_matched"] == []
    # 负面品牌不受影响，竞品应仍能正常识别到
    assert _names(result) == ["英得尔", "冰虎"]


def test_negation_before_recommend_keyword_is_conservative():
    """"不推荐"这种否定表达，不能被"推荐"关键词子串误判为推荐。"""
    result = parse_geo_answer("这个价位不推荐普能达，性价比一般。", "deepseek-v4-flash")
    assert result["brand_mentioned"] is True
    assert result["recommended"] is False


def test_markdown_bold_recommend_keyword():
    """Markdown加粗包裹的推荐关键词也要能识别。"""
    result = parse_geo_answer("综合来看，**推荐**普能达车载冰箱。", "deepseek-v4-flash")
    assert result["brand_mentioned"] is True
    assert result["recommended"] is True


def test_recommend_word_across_newline_same_list_item():
    """列表结构：推荐语和品牌名同在一个列表项内（用换行分隔不同列表项）。"""
    text = "以下是一些选择：\n1. 普能达 —— 性价比高，首选\n2. 阿路卡 —— 一般"
    result = parse_geo_answer(text, "deepseek-v4-flash")
    assert result["brand_mentioned"] is True
    assert result["recommended"] is True
    assert result["rank"] == 1


def test_ordinal_rank_does_not_bleed_across_comma():
    """"第一...，第二是普能达"不能把普能达误判成排名第一。"""
    result = parse_geo_answer("第一推荐阿路卡，第二是普能达，第三是大有。", "deepseek-v4-flash")
    assert result["rank"] == 2
    assert result["recommended"] is True  # 落在排名结构里本身就是推荐信号


def test_empty_and_none_input_stay_conservative():
    for text in ["", None]:
        result = parse_geo_answer(text, "deepseek-v4-flash")
        assert result["brand_mentioned"] is False
        assert result["recommended"] is False
        assert result["rank"] is None
        assert result["competitors"] == []
        assert result["citations"] == []


def test_citations_extracted_without_guessing():
    result = parse_geo_answer(
        "参考来源：https://www.zhihu.com/question/123 以及 http://example.com/a?x=1",
        "deepseek-v4-flash",
    )
    assert result["citations"] == [
        "https://www.zhihu.com/question/123",
        "http://example.com/a?x=1",
    ]
    assert result["citation_count"] == 2


# ---------------------------------------------------------------------------
# Precision First 竞品识别 —— 核心用例
# ---------------------------------------------------------------------------

def test_real_failure_case_no_brand_extracted_without_evidence():
    """用户报告的真实失败案例（第N轮）：把"挑选""不能只看""国产""这个""国内高端"
    这些普通词/描述词识别成了竞品品牌。这句话本身没有任何真实品牌名，也没有
    任何高置信度结构，正确结果必须是空列表——不能为了让结果看起来丰富而
    猜测品牌。
    """
    text = "选择车载冰箱不能只看品牌，国产高端产品也不错，这个价位性价比很高。"
    result = parse_geo_answer(text, "deepseek-v4-flash")
    assert result["competitors"] == []
    names = _names(result)
    for forbidden in ["挑选", "不能只看", "国产", "这个", "国内高端", "选择", "国产高端"]:
        assert forbidden not in names, f"混入了通用词: {forbidden}"


def test_known_brands_with_cn_en_parens_recognized_and_aliases_merged():
    """历史正确案例：中英文括号结构的品牌名要能识别，且中英文alias要合并成
    一条，不能把同一个品牌的中文名和英文名算成两个竞品。
    """
    text = "推荐品牌包括英得尔（Indel B）、冰虎（Alpicool）、科敏（KEMIN）和美固（MOBICOOL）。"
    result = parse_geo_answer(text, "deepseek-v4-flash")
    assert _names(result) == ["英得尔", "冰虎", "科敏", "美固"]
    for c in result["competitors"]:
        assert c["confidence"] == "high"
        assert c["evidence"]  # 每条都必须带原文证据，不能是空字符串
    # 中英文alias确实合并到了同一条目上，不是拆成两条
    by_name = {c["name"]: c for c in result["competitors"]}
    assert "Alpicool" in by_name["冰虎"]["aliases"]
    assert "MOBICOOL" in by_name["美固"]["aliases"]


def test_no_evidence_no_confusion_returns_empty():
    """"选择车载冰箱不能只看品牌，国产高端产品现在也很多"——没有任何品牌结构，
    必须返回空列表。"""
    result = parse_geo_answer(
        "选择车载冰箱不能只看品牌，国产高端产品现在也很多。", "deepseek-v4-flash"
    )
    assert result["competitors"] == []


def test_every_competitor_has_confidence_and_evidence_fields():
    """competitors 必须是结构化数据，每条都带 confidence 和非空 evidence，
    不能是裸字符串。"""
    result = parse_geo_answer(
        "英得尔（Indel B）口碑不错。", "deepseek-v4-flash"
    )
    assert len(result["competitors"]) == 1
    c = result["competitors"][0]
    assert set(c.keys()) == {"name", "aliases", "confidence", "evidence"}
    assert c["confidence"] == "high"
    assert c["evidence"]


def test_markdown_bold_brand_title_recognized():
    """Markdown明确品牌标题："1. **英得尔（Indel B）**" 这种写法要能识别。"""
    text = "1. **英得尔（Indel B）**\n2. **冰虎（Alpicool）**"
    result = parse_geo_answer(text, "deepseek-v4-flash")
    assert _names(result) == ["英得尔", "冰虎"]


def test_own_brand_never_enters_competitors():
    """主品牌普能达/PUNEDA 不能进入竞品列表，即使写成"普能达（PUNEDA）"
    这种和竞品一样的括号结构。"""
    result = parse_geo_answer("普能达（PUNEDA）是一家车载冰箱厂家。", "deepseek-v4-flash")
    assert result["brand_mentioned"] is True
    assert result["competitors"] == []


def test_advice_style_list_items_not_extracted_as_competitors():
    """回归测试：很多真实DeepSeek列表其实是"购买建议/注意事项"而非"品牌排名"，
    例如"1. 只是放几瓶水，不需要买太大容量的" "2. 挑选时注意压缩机品牌"。
    这类列表项完全不含品牌名，也没有任何高置信度结构，必须返回空列表。
    """
    text = (
        "车载冰箱选购建议：\n"
        "1. 只是放几瓶水，不需要买太大容量的\n"
        "2. 家庭日常使用建议选择20L左右\n"
        "3. 长期露营或多人出行建议选择更大容量\n"
        "4. 挑选时注意压缩机品牌和噪音水平\n"
        "5. 这两个参数最重要：制冷速度和耗电量\n"
        "6. 全球主流品牌都有对应产品线\n"
        "7. 近几年推出了很多半导体制冷新品\n"
        "8. 它是判断产品质量的重要指标\n"
        "9. 除了看容量还要看外观设计"
    )
    result = parse_geo_answer(text, "deepseek-v4-flash")
    assert result["competitors"] == []


def test_real_world_generic_phrases_not_extracted_as_competitors():
    """回归测试：一段正常的长回答不能被误抽出"这类""关于""选择""主流""国内"
    "有些""源自意大利"等通用词/国家名当成"竞品品牌"。阿路卡/大有属于明确的
    "品牌包括A、B"枚举结构，且不在强制排除词库里，属于高置信度候选。
    """
    text = (
        "车载冰箱市场上有很多选择，国内品牌和国际品牌都有不错的产品。\n"
        "关于这类品牌，主流品牌包括阿路卡、大有等。国内品牌普遍性价比更高，\n"
        "而有些品牌是全球知名品牌，比如源自意大利的一些高端品牌，也有厂家专注于中国市场。"
    )
    result = parse_geo_answer(text, "deepseek-v4-flash")
    names = _names(result)
    garbage_terms = {"这类", "关于", "选择", "主流", "国内", "有些", "全球",
                      "源自意大利", "中国", "意大利", "也有"}
    assert not (set(names) & garbage_terms), f"混入了通用词/国家名: {set(names) & garbage_terms}"
    assert names == ["阿路卡", "大有"]


def test_colon_enumeration_without_verb_recognized():
    """"以下几个品牌：A、B、C"这种冒号直接列举（没有"有/包括"等动词）也要能
    识别成并列竞品列表，这是真实DeepSeek回答里很常见的写法。
    """
    result = parse_geo_answer(
        "车载冰箱推荐以下几个品牌：英得尔、冰虎、科敏，性价比都不错。", "deepseek-v4-flash"
    )
    assert _names(result) == ["英得尔", "冰虎", "科敏"]


def test_no_structure_no_dictionary_hit_stays_empty():
    """"市面上比较知名的有A、B、C等几个品牌"——"品牌"在枚举之后而不是之前，
    不满足任何一种高置信度结构（不在词典里、没有中英文括号、也不是"品牌：
    A、B、C"这种明确提示句式），Precision First 下宁可漏掉也不能猜测。
    """
    result = parse_geo_answer(
        "市面上比较知名的有拉菲、大有、车载梦想等几个品牌。", "deepseek-v4-flash"
    )
    assert result["competitors"] == []


# ---------------------------------------------------------------------------
# 回归测试：品牌裸冒号 + 描述性分句被误当成"品牌列表"
# ---------------------------------------------------------------------------

def test_real_failure_case_again_with_full_forbidden_word_set():
    """用户报告的真实失败案例，逐字符复核：不能出现"挑选""不能只看""国产"
    "这个""国内高端"任意一个作为竞品名。"""
    text = "选择车载冰箱不能只看品牌，国产高端产品也不错，这个价位性价比很高，值得挑选。"
    result = parse_geo_answer(text, "deepseek-v4-flash")
    assert result["competitors"] == []
    names = _names(result)
    for forbidden in ["挑选", "不能只看", "国产", "这个", "国内高端"]:
        assert forbidden not in names, f"混入了通用词: {forbidden}"


def test_bare_colon_hint_with_descriptive_clause_not_extracted():
    """"品牌：主要看压缩机和口碑"——裸冒号后面跟的是"选购要看什么"的描述句，
    不是品牌枚举。裸冒号触发的BRAND_HINT_PATTERN只应捕获单个候选词，且必须
    通过ENUMERATION_GUARD_CHARS二次校验，"口碑"这类商业描述词也在强制排除
    词库里，不能被识别成竞品。
    """
    text = "买车载冰箱的时候，品牌：主要看压缩机和口碑，不能只看价格，国产高端产品也不错。"
    result = parse_geo_answer(text, "deepseek-v4-flash")
    assert result["competitors"] == []


def test_bare_colon_multi_clause_list_not_extracted_as_brands():
    """"品牌：容量、噪音和功耗需要综合评估"表面符合"品牌：A、B和C"的形状，
    但裸冒号信号太弱，不能触发多项列表解析（BRAND_LIST_PATTERN现在要求
    "有/包括/如/推荐"这类显式枚举动词），"容量""噪音""功耗"这些产品参数词
    也不能被当成竞品品牌。
    """
    text = "这类车载冰箱品牌：容量、噪音和功耗需要综合评估，不能只看价格。"
    result = parse_geo_answer(text, "deepseek-v4-flash")
    assert result["competitors"] == []


def test_bare_colon_verb_phrase_not_extracted_as_brand():
    """"品牌：外观和做工都不错，值得入手"——"和"连接的是两个描述性短语，
    不是品牌名并列。ENUMERATION_GUARD_CHARS命中"都"字后应整体拒绝。
    """
    text = "选择时品牌：外观和做工都不错，值得入手。"
    result = parse_geo_answer(text, "deepseek-v4-flash")
    assert result["competitors"] == []


def test_explicit_verb_enumeration_of_known_brands_still_works():
    """裸冒号分支收紧后，"品牌包括A、B"这种带显式枚举动词的写法必须继续生效，
    不能连真实的、已知品牌的枚举列表也一起漏掉。"""
    result = parse_geo_answer(
        "关于这类品牌，主流品牌包括阿路卡、大有等。", "deepseek-v4-flash"
    )
    assert _names(result) == ["阿路卡", "大有"]


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
