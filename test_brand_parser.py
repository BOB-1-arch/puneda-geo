"""
brand_parser.py 的离线自动化测试。
只做确定性断言，不依赖网络或DeepSeek真实调用。
运行方式：python -m pytest test_brand_parser.py -v
（或直接 python test_brand_parser.py，内置了不依赖pytest的兜底运行器）
"""

from brand_parser import parse_geo_answer


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
    # 顺带验证"和"作为并列连接词不会把两个竞品名错误合并成一个候选词
    assert result["competitors"] == ["英得尔", "冰虎"]


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
    assert result["competitors"] == ["英得尔", "冰虎"]


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


def test_generic_descriptive_sentence_not_extracted_as_competitor():
    """回归测试：上一轮修复过的误识别不能复发。"""
    text = "车载冰箱推荐以下品牌：\n1. 普能达（PUNEDA）— 性价比高\n2. 阿路卡（ALPICOOL）— 老牌厂家\n3. 车载梦想 — 性能不错"
    result = parse_geo_answer(text, "deepseek-v4-flash")
    assert result["competitors"] == ["阿路卡", "车载梦想"]
    assert "老牌" not in result["competitors"]
    assert "车载冰箱推荐以下" not in result["competitors"]


def test_own_brand_followed_by_descriptive_suffix_not_extracted_as_competitor():
    """回归测试：深度诊断批量测试中新发现的问题——本方品牌名紧跟"是一家...厂家/
    品牌/车载冰箱"这类描述句式时，BRAND_SUFFIX_PATTERN 会把"普能达是一家"整体
    误抽成竞品名。真实竞品名不可能包含本方品牌的字符串，因此按"包含"过滤。
    """
    result = parse_geo_answer("普能达是一家车载冰箱生产厂家，主要做OEM代工。", "deepseek-v4-flash")
    assert result["brand_mentioned"] is True
    assert result["competitors"] == []
    assert "普能达是一家" not in result["competitors"]


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
