"""
GEO 深度诊断 - 聚合统计

输入是一批 diagnosis_items（storage.get_run_items 的返回结构），
全部是纯函数，不碰数据库、不碰网络，方便单测。

诚实原则（和 brand_parser / diagnosis_analyzer 一致）：
- 只统计现有数据结构里真实存在的字段，不编造。
- 竞品TOP10只统计"出现次数"和"按识别顺序进入前3的次数"两个可靠指标；
  brand_parser 目前不追踪"某个竞品是否被AI判定为推荐"，这个信号不存在，
  所以这里不做"竞品推荐次数"这种编造不出来的统计（详见 aggregate_competitors
  的函数说明）。
"""

from collections import Counter

HIGH_VALUE_INTENTS = {"manufacturer", "OEM", "ODM", "automotive_supplier", "distributor_procurement"}

_PRIORITY_ORDER = {"P1": 0, "P2": 1, "P3": 2}


def _rate(numerator, denominator):
    if not denominator:
        return None
    return round(numerator / denominator * 100, 1)


def compute_run_stats(items):
    """items: 一次run里全部的 diagnosis_items（含失败的）。"""
    total = len(items)
    success_items = [it for it in items if it.get("status") == "success"]
    failed_items = [it for it in items if it.get("status") != "success"]

    mentioned = [it for it in success_items if it.get("brand_mentioned")]
    recommended = [it for it in success_items if it.get("recommended")]
    top3 = [it for it in success_items if it.get("rank") is not None and it["rank"] <= 3]
    ranked = [it for it in success_items if it.get("rank") is not None]
    with_citation = [it for it in success_items if it.get("citations")]

    avg_rank = None
    if ranked:
        avg_rank = round(sum(it["rank"] for it in ranked) / len(ranked), 1)

    return {
        "total_questions": total,
        "success_count": len(success_items),
        "failed_count": len(failed_items),
        "brand_mentioned_count": len(mentioned),
        "brand_mention_rate": _rate(len(mentioned), len(success_items)),
        "recommended_count": len(recommended),
        "recommend_rate": _rate(len(recommended), len(success_items)),
        "top3_count": len(top3),
        "top3_rate": _rate(len(top3), len(success_items)),
        "avg_rank": avg_rank,
        "citation_count": len(with_citation),
        "citation_rate": _rate(len(with_citation), len(success_items)),
    }


def aggregate_competitors(items, top_n=10):
    """竞争品牌TOP10：出现次数 + 按识别顺序进入该题前3位的次数。

    "前3位"是指该竞品在该题 competitors 列表里的顺序位置（brand_parser
    按首次出现顺序排列），不是AI在答案里给出的排名数字——brand_parser
    目前只对本方品牌计算 rank，不对竞品计算排名，所以这里只能给出一个
    有真实依据、但含义更弱的"识别顺序前3"指标，不冒充成"AI排名前3"。
    """
    appearance = Counter()
    early_mention = Counter()  # 在该题 competitors 列表前3位出现

    for it in items:
        if it.get("status") != "success":
            continue
        competitors = it.get("competitors") or []
        for name in competitors:
            appearance[name] += 1
        for name in competitors[:3]:
            early_mention[name] += 1

    ranked = sorted(appearance.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]
    return [
        {
            "name": name,
            "appearance_count": count,
            "early_mention_count": early_mention.get(name, 0),
        }
        for name, count in ranked
    ]


def aggregate_gaps(items, top_n=10):
    """GEO Gap 聚合：按 gap.type 计数，附带命中该Gap的问题列表（用于前端
    "点击Gap查看对应问题"）。NO_CLEAR_GAP 不计入问题类Gap统计。
    """
    counter = Counter()
    label_by_type = {}
    questions_by_type = {}

    for it in items:
        if it.get("status") != "success":
            continue
        for gap in it.get("gaps") or []:
            gtype = gap.get("type")
            if gtype == "NO_CLEAR_GAP":
                continue
            counter[gtype] += 1
            label_by_type[gtype] = gap.get("label", gtype)
            questions_by_type.setdefault(gtype, [])
            if it["question"] not in questions_by_type[gtype]:
                questions_by_type[gtype].append(it["question"])

    ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]
    return [
        {
            "type": gtype,
            "label": label_by_type.get(gtype, gtype),
            "count": count,
            "questions": questions_by_type.get(gtype, []),
        }
        for gtype, count in ranked
    ]


def aggregate_query_intent_distribution(items):
    counter = Counter()
    for it in items:
        counter[it.get("query_intent") or "other"] += 1
    return dict(sorted(counter.items(), key=lambda kv: -kv[1]))


def aggregate_high_value(items):
    """B2B高商业价值Query（厂家寻找/OEM/ODM/主机厂配套/经销商采购）专项统计。"""
    high_items = [it for it in items if it.get("commercial_value") == "high"]
    success_high = [it for it in high_items if it.get("status") == "success"]

    mentioned = [it for it in success_high if it.get("brand_mentioned")]
    recommended = [it for it in success_high if it.get("recommended")]
    top3 = [it for it in success_high if it.get("rank") is not None and it["rank"] <= 3]
    uncovered = [it for it in success_high if not it.get("brand_mentioned")]

    return {
        "high_value_count": len(high_items),
        "brand_mention_rate": _rate(len(mentioned), len(success_high)),
        "recommend_rate": _rate(len(recommended), len(success_high)),
        "top3_rate": _rate(len(top3), len(success_high)),
        "uncovered_questions": [it["question"] for it in uncovered],
    }


def aggregate_action_items(items):
    """把20题各自的 action_items 去重聚合，同一个 (priority, action_type, action)
    只保留一条，附带影响到的问题列表和数量。
    """
    merged = {}
    order = []
    for it in items:
        if it.get("status") != "success":
            continue
        for a in it.get("actions") or []:
            key = (a.get("priority"), a.get("action_type"), a.get("action"))
            if key not in merged:
                merged[key] = {
                    "priority": a.get("priority"),
                    "action_type": a.get("action_type"),
                    "action": a.get("action"),
                    "reason": a.get("reason"),
                    "gap_type": a.get("gap_type"),
                    "affected_queries": [],
                }
                order.append(key)
            q = it.get("question")
            if q and q not in merged[key]["affected_queries"]:
                merged[key]["affected_queries"].append(q)

    result = [merged[k] for k in order]
    for r in result:
        r["affected_queries_count"] = len(r["affected_queries"])
    result.sort(key=lambda r: (_PRIORITY_ORDER.get(r["priority"], 9), -r["affected_queries_count"]))
    return result


def build_full_report(items):
    """一次性把所有聚合结果打包，供 API 返回 / 前端渲染报告使用。"""
    return {
        "stats": compute_run_stats(items),
        "competitors_top": aggregate_competitors(items),
        "gaps_top": aggregate_gaps(items),
        "query_intent_distribution": aggregate_query_intent_distribution(items),
        "high_value": aggregate_high_value(items),
        "action_items": aggregate_action_items(items),
    }
