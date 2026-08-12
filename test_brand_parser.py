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


def test_real_world_generic_phrases_not_extracted_as_competitors():
    """回归测试：生产环境用真实DeepSeek Key跑深度诊断时发现的严重误判——一段
    正常的长回答被BRAND_SUFFIX_PATTERN抽出了"这类""关于""选择""主流""国内"
    "有些""源自意大利"等一整批通用词/国家名当成"竞品品牌"，几乎污染了整个
    竞品TOP10统计。根因是该正则原本把句首(^)/句号(。)/空白(\\s)都当作合法
    边界，几乎每个新分句开头都会触发一次误抽取。
    """
    text = (
        "车载冰箱市场上有很多选择，国内品牌和国际品牌都有不错的产品。\n"
        "关于这类品牌，主流品牌包括阿路卡、大有等。国内品牌普遍性价比更高，\n"
        "而有些品牌是全球知名品牌，比如源自意大利的一些高端品牌，也有厂家专注于中国市场。"
    )
    result = parse_geo_answer(text, "deepseek-v4-flash")
    garbage_terms = {"这类", "关于", "选择", "主流", "国内", "有些", "全球",
                      "源自意大利", "中国", "意大利", "也有"}
    assert not (set(result["competitors"]) & garbage_terms), (
        f"混入了通用词/国家名: {set(result['competitors']) & garbage_terms}"
    )
    assert result["competitors"] == ["阿路卡", "大有"]


def test_enumeration_tail_quantifier_stripped_from_candidate():
    """"车载梦想等几个品牌"这类枚举收尾里的"等几个/等几家/等多个"要被清洗掉，
    不能把量词尾巴当成品牌名的一部分。
    """
    result = parse_geo_answer("市面上比较知名的有阿路卡、大有、车载梦想等几个品牌。", "deepseek-v4-flash")
    assert "车载梦想等几个" not in result["competitors"]
    assert "车载梦想" in result["competitors"]


def test_colon_enumeration_without_verb_recognized():
    """"以下几个品牌：A、B、C"这种冒号直接列举（没有"有/包括"等动词）也要能
    识别成并列竞品列表，这是真实DeepSeek回答里很常见的写法。
    """
    result = parse_geo_answer(
        "车载冰箱推荐以下几个品牌：英得尔、冰虎、科敏，性价比都不错。", "deepseek-v4-flash"
    )
    assert result["competitors"] == ["英得尔", "冰虎", "科敏"]


def test_advice_style_list_items_not_extracted_as_competitors():
    """回归测试：合并上一轮修复后，用户反馈又看到了新一批误判——"只是放几瓶水"
    "家庭""挑选""这两个""全球""它是""除了看"这些通用词/句子片段。根因是不同
    的bug：很多真实DeepSeek列表其实是"购买建议/注意事项"而非"品牌排名"，例如
    "1. 只是放几瓶水，不需要买太大容量的" "2. 挑选时注意压缩机品牌"，这类列表项
    完全不是品牌名开头，但原来的逻辑无脑截取每条列表项前2-8个字当候选品牌名。

    修复后要求候选词必须紧跟"（英文名）/——/-/："这类"品牌名后接说明"的分隔符
    信号才提取，没有这个信号就不提取，宁可漏检也不能把整句话开头当成品牌名。
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


def test_brand_led_list_items_still_extracted_after_stricter_pattern():
    """收紧列表项提取规则后，真正"品牌名开头+分隔符+说明"的列表项还是要能正常
    提取，不能矫枉过正把所有列表都清空。覆盖"（英文名）——" "——" "：" 三种
    常见分隔符写法。
    """
    text = (
        "车载冰箱推荐以下品牌：\n"
        "1. 普能达（PUNEDA）— 性价比高\n"
        "2. 阿路卡（ALPICOOL）— 老牌厂家\n"
        "3. 英得尔——国内知名品牌\n"
        "4. 大有：口碑不错"
    )
    result = parse_geo_answer(text, "deepseek-v4-flash")
    assert result["competitors"] == ["阿路卡", "英得尔", "大有"]
    assert result["rank"] == 1


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
