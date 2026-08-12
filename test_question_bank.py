"""
question_bank.py 的离线自动化测试。
运行方式：python test_question_bank.py
"""

from collections import Counter

from question_bank import QUESTION_BANK, DEFAULT_INTENT_WEIGHTS, select_questions
from diagnosis_analyzer import classify_query_intent


def test_bank_has_all_required_intents():
    intents = {item["query_intent"] for item in QUESTION_BANK}
    required = {
        "consumer_brand", "consumer_product", "manufacturer", "OEM", "ODM",
        "automotive_supplier", "distributor_procurement", "technical",
        "usage", "after_sales", "comparison",
    }
    assert required.issubset(intents)


def test_bank_questions_self_classify_consistently():
    """问题库里每道题标注的 query_intent，必须能被 diagnosis_analyzer 的真实分类
    规则独立分类出同样的结果——否则批量诊断落库时存的intent会和实际诊断跑出来的
    intent对不上，统计口径会乱。这是一个回归测试，防止以后改问题库文案时踩坑。
    """
    mismatches = []
    for item in QUESTION_BANK:
        actual = classify_query_intent(item["question"])
        if actual != item["query_intent"]:
            mismatches.append((item["question"], item["query_intent"], actual))
    assert not mismatches, f"以下问题的声明intent和实际分类不一致: {mismatches}"


def test_default_selection_returns_20_unique_questions():
    qs = select_questions(None, 20)
    assert len(qs) == 20
    assert len(set(q["question"] for q in qs)) == 20


def test_default_selection_skews_toward_high_commercial_value():
    """不能让20题全部是C端消费者问题：默认分布下B2B高价值Query占比要过半。"""
    qs = select_questions(None, 20)
    high_value_count = sum(1 for q in qs if q["commercial_value"] == "high")
    assert high_value_count >= 10, f"高价值Query只有{high_value_count}条，应占多数"

    b2b_intents = {"manufacturer", "OEM", "ODM", "automotive_supplier", "distributor_procurement"}
    b2b_count = sum(1 for q in qs if q["query_intent"] in b2b_intents)
    assert b2b_count >= 10, f"B2B意图问题只有{b2b_count}条，不能全是C端消费者问题"


def test_default_selection_matches_configured_weights():
    qs = select_questions(None, 20)
    counter = Counter(q["query_intent"] for q in qs)
    for intent, weight in DEFAULT_INTENT_WEIGHTS.items():
        assert counter.get(intent, 0) == weight


def test_selection_scoped_to_chosen_intents():
    qs = select_questions(["manufacturer", "OEM"], 10)
    assert len(qs) == 10
    assert all(q["query_intent"] in ("manufacturer", "OEM") for q in qs)


def test_selection_with_unknown_intent_falls_back_to_default():
    qs = select_questions(["not_a_real_intent"], 5)
    assert len(qs) == 5  # 无效intent时应回退到默认分布，而不是返回空列表


def test_selection_count_smaller_than_bank():
    qs = select_questions(None, 5)
    assert len(qs) == 5


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
