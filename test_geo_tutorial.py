"""
geo_tutorial.py 的离线自动化测试。纯函数测试，不碰数据库/网络。
运行方式：python test_geo_tutorial.py
"""

from diagnosis_analyzer import GAP_LABELS_CN
from geo_tutorial import BASIC_SECTIONS, GAP_TUTORIALS, build_tutorial_report


def _item(question, gaps, status="success"):
    return {"question": question, "status": status, "gaps": gaps}


def _gap(gap_type):
    return {"type": gap_type, "label": GAP_LABELS_CN.get(gap_type, gap_type), "evidence": "..."}


def test_no_diagnosis_data_returns_only_basics():
    """还没跑过深度诊断时，只展示固定的GEO基础知识，不编造针对性建议。"""
    report = build_tutorial_report(None)
    assert report["has_diagnosis_data"] is False
    assert report["personalized"] == []
    assert len(report["basics"]) > 0


def test_empty_items_list_treated_same_as_no_data():
    report = build_tutorial_report([])
    assert report["has_diagnosis_data"] is False
    assert report["personalized"] == []


def test_basics_always_present_regardless_of_diagnosis_data():
    with_data = build_tutorial_report([_item("q1", [_gap("BRAND_ABSENCE")])])
    without_data = build_tutorial_report(None)
    assert with_data["basics"] == without_data["basics"] == BASIC_SECTIONS


def test_every_real_gap_type_has_a_matching_tutorial():
    """diagnosis_analyzer 里除 NO_CLEAR_GAP 外的10种Gap类型都必须有对应教程，
    不能诊断出一个Gap、教程库里却找不到对应内容。"""
    real_gap_types = set(GAP_LABELS_CN.keys()) - {"NO_CLEAR_GAP"}
    assert real_gap_types == set(GAP_TUTORIALS.keys())


def test_no_clear_gap_never_produces_a_personalized_tutorial():
    """NO_CLEAR_GAP本身不是一个需要教程指导的具体问题，不应该出现在个性化列表里。"""
    items = [_item("q1", [_gap("NO_CLEAR_GAP")])]
    report = build_tutorial_report(items)
    assert report["personalized"] == []


def test_personalized_ordered_by_gap_frequency_descending():
    """出现次数越多的Gap，越应该排在教程列表前面——用户最该优先看的应该最先看到。"""
    items = [
        _item("q1", [_gap("BRAND_ABSENCE")]),
        _item("q2", [_gap("BRAND_ABSENCE")]),
        _item("q3", [_gap("BRAND_ABSENCE")]),
        _item("q4", [_gap("CITATION_GAP")]),
    ]
    report = build_tutorial_report(items)
    types_in_order = [p["gap_type"] for p in report["personalized"]]
    assert types_in_order[0] == "BRAND_ABSENCE"
    assert report["personalized"][0]["affected_count"] == 3
    assert report["personalized"][1]["affected_count"] == 1


def test_personalized_tutorial_carries_affected_questions_for_drilldown():
    """每条个性化教程都要带上命中该Gap的具体问题列表，方便前端"点击查看对应问题"。"""
    items = [
        _item("q1", [_gap("CITATION_GAP")]),
        _item("q2", [_gap("CITATION_GAP")]),
    ]
    report = build_tutorial_report(items)
    citation_tutorial = report["personalized"][0]
    assert set(citation_tutorial["affected_questions"]) == {"q1", "q2"}
    assert citation_tutorial["action_type"] == "THIRD_PARTY_MEDIA"


def test_failed_items_do_not_contribute_gaps():
    """失败题没有真实回答，不应该被当成任何Gap的证据。"""
    items = [_item("q1", [], status="failed")]
    report = build_tutorial_report(items)
    assert report["personalized"] == []


def test_every_tutorial_has_required_fields_and_non_empty_content():
    """每篇教程都必须有完整字段，不能是半成品。"""
    for gap_type, tutorial in GAP_TUTORIALS.items():
        for field in ("id", "title", "action_type", "summary", "why", "how_to", "example"):
            assert field in tutorial, f"{gap_type} 缺少字段 {field}"
        assert tutorial["why"], f"{gap_type} 的 why 不能为空"
        assert tutorial["how_to"], f"{gap_type} 的 how_to 不能为空"
        assert tutorial["example"].strip(), f"{gap_type} 的 example 不能为空"


def test_every_basic_section_has_required_fields_and_non_empty_content():
    for section in BASIC_SECTIONS:
        for field in ("id", "title", "summary", "content"):
            assert field in section, f"{section.get('id')} 缺少字段 {field}"
        assert section["content"], f"{section['id']} 的 content 不能为空"


def test_multiple_gap_types_all_appear_when_present():
    """一次诊断里同时命中多种Gap时，个性化列表要把它们都覆盖到，不能只挑一个。"""
    items = [
        _item("q1", [_gap("BRAND_ABSENCE")]),
        _item("q2", [_gap("RANK_GAP")]),
        _item("q3", [_gap("SOURCE_AUTHORITY_GAP")]),
    ]
    report = build_tutorial_report(items)
    types_found = {p["gap_type"] for p in report["personalized"]}
    assert types_found == {"BRAND_ABSENCE", "RANK_GAP", "SOURCE_AUTHORITY_GAP"}


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
