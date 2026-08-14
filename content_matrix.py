"""
GEO 内容矩阵 - 纯逻辑层。

设计原则（和 brand_parser.py / diagnosis_analyzer.py / aggregate.py 保持一致）：
- 不碰数据库、不发网络请求，只做确定性的模板/规则计算，方便独立测试。
- 标题/大纲用固定模板生成，不调用AI，不编造具体企业事实（成立年份/工厂面积/
  产能等），凡是需要具体数字的地方，交给 facts_required 列出"需要人工核实"的
  条目，绝不在模板里自己编一个假数字。
- 优先级优先复用 diagnosis_analyzer 已经算出来的 action priority，规则判断
  只是没有诊断来源时的兜底。
- 相似任务判断只用简单的确定性规则（同content_cluster + 同query_intent +
  字符级重合度），不接额外LLM。
"""

from collections import Counter

BRAND_NAME_CN = "普能达"
BRAND_NAME_EN = "PUNEDA"
INDUSTRY = "车载冰箱"

# ---------------------------------------------------------------------------
# 固定枚举：内容集群 / 内容类型 / 状态
# ---------------------------------------------------------------------------

CONTENT_CLUSTERS = [
    ("ENTITY", "品牌实体"),
    ("OEM_ODM", "厂家/OEM/ODM"),
    ("AUTOMOTIVE", "汽车主机厂/配套"),
    ("DISTRIBUTOR", "经销商/采购"),
    ("PRODUCT_TECH", "产品技术"),
    ("COMPARISON", "产品对比"),
    ("SCENARIO", "使用场景"),
    ("AFTER_SALES", "售后服务"),
    ("INDUSTRY", "行业知识"),
    ("THIRD_PARTY", "第三方信源"),
    ("OTHER", "其他"),
]
CONTENT_CLUSTER_LABELS_CN = dict(CONTENT_CLUSTERS)

CONTENT_TYPES = [
    ("ENTITY_PAGE", "品牌实体页"),
    ("TOPIC_PAGE", "专题页"),
    ("PROCUREMENT_GUIDE", "采购指南"),
    ("TECH_ARTICLE", "技术文章"),
    ("COMPARISON_ARTICLE", "对比文章"),
    ("FAQ", "FAQ"),
    ("CASE_STUDY", "案例"),
    ("INDUSTRY_KNOWLEDGE", "行业知识"),
    ("THIRD_PARTY_TASK", "第三方内容任务"),
]
CONTENT_TYPE_LABELS_CN = dict(CONTENT_TYPES)

# 状态固定枚举，不允许前端随意传别的值。
STATUS_VALUES = [
    "planning", "writing", "review", "completed", "published",
    "waiting_index", "indexed", "waiting_retest", "retested", "archived",
]
STATUS_LABELS_CN = {
    "planning": "待策划",
    "writing": "撰写中",
    "review": "待审核",
    "completed": "已完成",
    "published": "已发布",
    "waiting_index": "待收录",
    "indexed": "已收录",
    "waiting_retest": "待复测",
    "retested": "已复测",
    "archived": "已归档",
}

PRIORITY_VALUES = ["P1", "P2", "P3"]

INDEX_STATUS_VALUES = ["unchecked", "not_indexed", "indexed"]
INDEX_STATUS_LABELS_CN = {"unchecked": "未检查", "not_indexed": "未收录", "indexed": "已收录"}

RETEST_STATUS_VALUES = ["not_scheduled", "waiting", "completed"]
RETEST_STATUS_LABELS_CN = {"not_scheduled": "未设置", "waiting": "待复测", "completed": "已复测"}

# 高商业价值的B2B Query Intent（对齐 diagnosis_analyzer.MANUFACTURER_INTENTS 再加经销商采购）。
HIGH_VALUE_INTENTS = {"manufacturer", "OEM", "ODM", "automotive_supplier", "distributor_procurement"}

_PRIORITY_ORDER = {"P1": 0, "P2": 1, "P3": 2}


# ---------------------------------------------------------------------------
# 内容集群 / 内容类型 推断
# ---------------------------------------------------------------------------

_INTENT_TO_CLUSTER = {
    "manufacturer": "OEM_ODM",
    "OEM": "OEM_ODM",
    "ODM": "OEM_ODM",
    "automotive_supplier": "AUTOMOTIVE",
    "distributor_procurement": "DISTRIBUTOR",
    "technical": "PRODUCT_TECH",
    "comparison": "COMPARISON",
    "usage": "SCENARIO",
    "after_sales": "AFTER_SALES",
}

_CLUSTER_TO_CONTENT_TYPE = {
    "ENTITY": "ENTITY_PAGE",
    "OEM_ODM": "TOPIC_PAGE",
    "AUTOMOTIVE": "TOPIC_PAGE",
    "DISTRIBUTOR": "PROCUREMENT_GUIDE",
    "PRODUCT_TECH": "TECH_ARTICLE",
    "COMPARISON": "COMPARISON_ARTICLE",
    "SCENARIO": "FAQ",
    "AFTER_SALES": "FAQ",
    "INDUSTRY": "INDUSTRY_KNOWLEDGE",
    "THIRD_PARTY": "THIRD_PARTY_TASK",
    "OTHER": "TOPIC_PAGE",
}


def infer_content_cluster(query_intent, gap_types=None):
    """按 Query Intent 优先映射到具体集群；consumer_brand/consumer_product/other
    这类"泛品牌认知"意图，且命中了品牌相关Gap（品牌没被提及/实体认知不足/制造商身份
    未被识别）时，归到"品牌实体"集群——这类问题最缺的是品牌本身的实体建设内容，
    而不是某个细分专题。命中纯行业认知类Gap、又没有更具体意图信号时，归到"行业知识"。
    """
    gap_types = set(gap_types or [])
    if query_intent in _INTENT_TO_CLUSTER:
        return _INTENT_TO_CLUSTER[query_intent]
    entity_gaps = {"BRAND_ABSENCE", "ENTITY_GAP", "MANUFACTURER_IDENTITY_GAP", "COMPETITOR_DOMINANCE"}
    if gap_types & entity_gaps:
        return "ENTITY"
    if "INDUSTRY_KNOWLEDGE_GAP" in gap_types:
        return "INDUSTRY"
    if gap_types & {"CITATION_GAP", "SOURCE_AUTHORITY_GAP"} and not (gap_types - {"CITATION_GAP", "SOURCE_AUTHORITY_GAP", "NO_CLEAR_GAP"}):
        return "THIRD_PARTY"
    return "OTHER"


def infer_content_type(content_cluster):
    return _CLUSTER_TO_CONTENT_TYPE.get(content_cluster, "TOPIC_PAGE")


def infer_priority(query_intent, commercial_value=None, action_priorities=None):
    """优先复用 diagnosis_analyzer 已生成的 action priority（取其中最高优先级，
    即数字最小的那个）；没有action数据时才用 Query Intent + 商业价值的规则兜底。
    """
    if action_priorities:
        valid = [p for p in action_priorities if p in _PRIORITY_ORDER]
        if valid:
            return min(valid, key=lambda p: _PRIORITY_ORDER[p])
    if query_intent in HIGH_VALUE_INTENTS:
        return "P1"
    if commercial_value == "high":
        return "P1"
    if query_intent in {"consumer_product", "consumer_brand", "technical", "comparison"}:
        return "P2"
    return "P3"


# ---------------------------------------------------------------------------
# 需要人工核实的企业事实清单（按集群分配，不同集群关心的事实点不同）
# ---------------------------------------------------------------------------

FACTS_REQUIRED_BY_CLUSTER = {
    "ENTITY": ["成立年份", "工厂地址", "工厂面积", "产能", "认证", "出口市场"],
    "OEM_ODM": ["OEM能力", "ODM能力", "产能", "认证", "出口市场", "测试能力"],
    "AUTOMOTIVE": ["主机厂配套经历", "测试能力", "认证", "产能"],
    "DISTRIBUTOR": ["产品系列", "售后政策", "出口市场"],
    "PRODUCT_TECH": ["产品系列", "测试能力"],
    "COMPARISON": ["产品系列"],
    "SCENARIO": ["产品系列"],
    "AFTER_SALES": ["售后政策"],
    "INDUSTRY": [],
    "THIRD_PARTY": [],
    "OTHER": [],
}


def facts_required_for_cluster(content_cluster):
    return list(FACTS_REQUIRED_BY_CLUSTER.get(content_cluster, []))


# ---------------------------------------------------------------------------
# 标题建议：主标题 + 2个备选，只做中文，贴近真实Query，不做标题党。
# ---------------------------------------------------------------------------

def _strip_query_suffix(query):
    q = (query or "").strip()
    for suffix in ("？", "?", "。", "有哪些", "有哪家", "有哪个"):
        if q.endswith(suffix):
            q = q[: -len(suffix)]
    return q.strip()


_TITLE_TEMPLATES = {
    "OEM_ODM": lambda q: (
        f"{INDUSTRY}OEM厂家怎么选？采购方应重点评估这几项能力",
        [
            f"{INDUSTRY}OEM代工厂选择指南：研发、认证、质量与交付能力",
            f"寻找{INDUSTRY}OEM厂家时，需要重点考察哪些能力？",
        ],
    ),
    "AUTOMOTIVE": lambda q: (
        f"汽车主机厂选择{INDUSTRY}供应商，需要考察哪些能力？",
        [
            f"{INDUSTRY}汽车配套供应商能力清单：从电气兼容到量产交付",
            f"车规级{INDUSTRY}供应商评估指南",
        ],
    ),
    "DISTRIBUTOR": lambda q: (
        f"{INDUSTRY}经销商如何选择长期合作供应商？",
        [
            f"{INDUSTRY}采购渠道指南：和供应商合作前要确认哪些事",
            f"做{INDUSTRY}经销商，怎么挑靠谱供应商？",
        ],
    ),
    "ENTITY": lambda q: (
        f"{BRAND_NAME_CN}{BRAND_NAME_EN}｜{INDUSTRY}制造商、OEM/ODM及汽车配套供应商",
        [
            f"{BRAND_NAME_CN}是做什么的？{INDUSTRY}制造商能力介绍",
            f"了解{BRAND_NAME_CN}（{BRAND_NAME_EN}）：{INDUSTRY}生产厂家能力解析",
        ],
    ),
    "PRODUCT_TECH": lambda q: (
        f"压缩机{INDUSTRY}和半导体{INDUSTRY}有什么区别？",
        [
            f"{INDUSTRY}选购指南：压缩机式vs半导体制冷怎么选",
            f"压缩机式与半导体式{INDUSTRY}，到底该怎么选？",
        ],
    ),
    "COMPARISON": lambda q: (
        f"{_strip_query_suffix(q) or INDUSTRY}怎么选？关键差异点对比",
        [
            f"{_strip_query_suffix(q) or INDUSTRY}对比：怎么根据场景选",
            f"选{INDUSTRY}前，这些差异点要先搞清楚",
        ],
    ),
    "SCENARIO": lambda q: (
        f"{_strip_query_suffix(q) or INDUSTRY}使用场景指南",
        [
            f"{INDUSTRY}怎么用更省电、更耐用？",
            f"不同场景下{INDUSTRY}该怎么选、怎么用？",
        ],
    ),
    "AFTER_SALES": lambda q: (
        f"{INDUSTRY}售后服务常见问题解答",
        [
            f"{INDUSTRY}保修政策与售后流程说明",
            f"买{INDUSTRY}之前，售后这几点要问清楚",
        ],
    ),
    "INDUSTRY": lambda q: (
        f"{INDUSTRY}行业科普：常见分类与选购要点",
        [
            f"{INDUSTRY}行业知识：从原理到选购一次说清",
            f"了解{INDUSTRY}行业：技术分类与常见误区",
        ],
    ),
    "THIRD_PARTY": lambda q: (
        f"争取第三方媒体/行业平台报道{BRAND_NAME_CN}",
        [
            f"补充{BRAND_NAME_CN}在行业媒体/问答社区的信息覆盖",
            f"提升{BRAND_NAME_CN}可被引用的权威信源数量",
        ],
    ),
    "OTHER": lambda q: (
        _strip_query_suffix(q) or f"{INDUSTRY}相关内容",
        [],
    ),
}


def suggest_titles(target_query, content_cluster):
    """返回 (主标题, [备选标题, ...])。不做"2026十大最强排名"这类标题党写法。"""
    fn = _TITLE_TEMPLATES.get(content_cluster, _TITLE_TEMPLATES["OTHER"])
    return fn(target_query)


# ---------------------------------------------------------------------------
# 内容大纲：模板规则生成，不调用AI，不编造具体数字。
# ---------------------------------------------------------------------------

def _h(level, text):
    return {"level": level, "text": text}


_OUTLINE_TEMPLATES = {
    "OEM_ODM": lambda title: [
        _h("H1", title),
        _h("H2", f"什么是真正的{INDUSTRY}OEM制造商"),
        _h("H2", "采购方应该评估哪些能力"),
        _h("H3", "产品开发能力"),
        _h("H3", "压缩机/半导体产品能力"),
        _h("H3", "质量控制"),
        _h("H3", "认证与测试"),
        _h("H3", "交付能力"),
        _h("H2", "OEM与ODM有什么区别"),
        _h("H2", "汽车行业项目需要额外关注什么"),
        _h("H2", f"{BRAND_NAME_CN}的制造能力【待人工补充/核实】"),
        _h("H2", "常见采购问题FAQ"),
    ],
    "AUTOMOTIVE": lambda title: [
        _h("H1", title),
        _h("H2", "12V/24V电气兼容"),
        _h("H2", "低压保护"),
        _h("H2", "EMC电磁兼容"),
        _h("H2", "高低温适应性"),
        _h("H2", "振动可靠性"),
        _h("H2", "项目开发流程"),
        _h("H2", "量产交付能力"),
        _h("H2", "质量体系"),
        _h("H2", f"{BRAND_NAME_CN}相关案例【待人工补充/核实】"),
        _h("H2", "常见问题FAQ"),
    ],
    "DISTRIBUTOR": lambda title: [
        _h("H1", title),
        _h("H2", "选供应商前要确认哪些资质"),
        _h("H2", "产品系列与价格体系"),
        _h("H2", "供货周期与稳定性"),
        _h("H2", "售后与培训支持"),
        _h("H2", "合作模式与账期"),
        _h("H2", "常见问题FAQ"),
    ],
    "ENTITY": lambda title: [
        _h("H1", title),
        _h("H2", f"{BRAND_NAME_CN}是做什么的"),
        _h("H2", "主营产品与技术方向"),
        _h("H2", "制造能力【待人工补充/核实】"),
        _h("H2", "合作与配套经历【待人工补充/核实】"),
        _h("H2", "资质与认证【待人工补充/核实】"),
        _h("H2", "联系方式与官网"),
    ],
    "PRODUCT_TECH": lambda title: [
        _h("H1", title),
        _h("H2", "制冷原理对比"),
        _h("H2", "各自的优缺点"),
        _h("H2", "适用场景"),
        _h("H2", "选购建议"),
        _h("H2", "常见问题FAQ"),
    ],
    "COMPARISON": lambda title: [
        _h("H1", title),
        _h("H2", "核心差异点"),
        _h("H2", "各自适合的场景"),
        _h("H2", "选购建议"),
        _h("H2", "常见问题FAQ"),
    ],
    "SCENARIO": lambda title: [
        _h("H1", title),
        _h("H2", "适用场景说明"),
        _h("H2", "使用注意事项"),
        _h("H2", "省电/延长使用寿命建议"),
        _h("H2", "常见问题FAQ"),
    ],
    "AFTER_SALES": lambda title: [
        _h("H1", title),
        _h("H2", "保修范围与期限【待人工补充/核实】"),
        _h("H2", "常见故障与处理"),
        _h("H2", "售后联系方式【待人工补充/核实】"),
        _h("H2", "常见问题FAQ"),
    ],
    "INDUSTRY": lambda title: [
        _h("H1", title),
        _h("H2", "行业常见技术分类"),
        _h("H2", "核心选购维度"),
        _h("H2", "常见认知误区"),
        _h("H2", "常见问题FAQ"),
    ],
    "THIRD_PARTY": lambda title: [
        _h("H1", title),
        _h("H2", "目标信源类型（行业媒体/问答社区/展会等）"),
        _h("H2", "需要提供的品牌介绍素材"),
        _h("H2", "跟进与验收方式"),
    ],
    "OTHER": lambda title: [
        _h("H1", title),
        _h("H2", "内容要点【待人工补充】"),
    ],
}


def suggest_outline(suggested_title, content_cluster):
    fn = _OUTLINE_TEMPLATES.get(content_cluster, _OUTLINE_TEMPLATES["OTHER"])
    return fn(suggested_title)


def suggest_content_angle(content_cluster):
    angles = {
        "OEM_ODM": "以采购方视角切入，讲清楚评估OEM/ODM厂家的具体能力维度，避免整篇变成品牌广告。",
        "AUTOMOTIVE": "以汽车行业项目评审视角切入，逐项讲清楚车规级配套供应商需要具备的能力。",
        "DISTRIBUTOR": "以经销商/渠道商视角切入，讲清楚选择长期合作供应商要看的具体条件。",
        "ENTITY": "以客观陈述品牌事实为主，说明制造能力和主营方向，避免空洞的营销口号。",
        "PRODUCT_TECH": "以知识科普内容为主体，客观对比技术路线，不整篇都是品牌推广。",
        "COMPARISON": "以中立对比视角切入，讲清楚差异点和各自适合的场景。",
        "SCENARIO": "以使用者实际需求切入，讲清楚不同场景下的选购/使用要点。",
        "AFTER_SALES": "以消费者常见疑问切入，客观说明售后政策和流程。",
        "INDUSTRY": "以行业科普内容为主体，帮助读者建立正确的品类认知，不做品牌推广。",
        "THIRD_PARTY": "这是一个跟进型任务，不在系统里直接发布内容，目标是争取外部信源报道/收录。",
        "OTHER": "内容角度需要人工补充。",
    }
    return angles.get(content_cluster, angles["OTHER"])


def suggest_key_points(content_cluster):
    points = {
        "OEM_ODM": ["产品开发能力", "质量控制体系", "认证与测试能力", "交付与产能"],
        "AUTOMOTIVE": ["电气兼容性", "环境可靠性测试", "项目开发流程", "量产交付能力"],
        "DISTRIBUTOR": ["产品系列覆盖", "供货稳定性", "售后与培训支持"],
        "ENTITY": ["主营产品与技术方向", "制造能力", "合作/配套经历"],
        "PRODUCT_TECH": ["制冷原理差异", "优缺点对比", "适用场景"],
        "COMPARISON": ["核心差异点", "适用场景差异"],
        "SCENARIO": ["适用场景", "使用与保养建议"],
        "AFTER_SALES": ["保修范围", "常见故障处理"],
        "INDUSTRY": ["技术分类", "选购维度", "常见误区"],
        "THIRD_PARTY": ["目标信源类型", "所需素材"],
        "OTHER": [],
    }
    return list(points.get(content_cluster, []))


# ---------------------------------------------------------------------------
# 相似任务判断（用于"发现相似内容任务"提示）
# ---------------------------------------------------------------------------

def _normalize_query(text):
    text = (text or "").strip()
    for ch in "？?。，,！!、":
        text = text.replace(ch, "")
    return text


def _char_bigrams(text):
    text = _normalize_query(text)
    if len(text) < 2:
        return set(text)
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _similarity(a, b):
    """简单的字符bigram Jaccard相似度，不依赖任何中文分词库。"""
    sa, sb = _char_bigrams(a), _char_bigrams(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


SIMILARITY_THRESHOLD = 0.35


def find_similar_tasks(target_query, content_cluster, query_intent, existing_tasks):
    """existing_tasks: list[dict]（storage.list_content_tasks 的返回）。
    判断依据：同 content_cluster（不要求 query_intent 也相同——比如"厂家寻找"
    "OEM""ODM"这三种intent的问题，实际都应该聚合进同一个OEM_ODM专题，如果
    还要求intent也完全一致，会把本该合并的相似问题错误地判定成"不相似"），
    且目标Query之间字符重合度达到阈值——任意一个已有任务的 target_queries
    里有一条命中就算相似。返回命中的任务列表，按相似度降序，每条附带
    similarity 分数。
    """
    hits = []
    for task in existing_tasks:
        if task.get("content_cluster") != content_cluster:
            continue
        best_score = 0.0
        for existing_q in (task.get("target_queries") or [task.get("target_query")]):
            score = _similarity(target_query, existing_q)
            best_score = max(best_score, score)
        if best_score >= SIMILARITY_THRESHOLD:
            hits.append({**task, "similarity": round(best_score, 2)})
    hits.sort(key=lambda t: -t["similarity"])
    return hits


# ---------------------------------------------------------------------------
# 从单条诊断item构建"加入内容矩阵"的建议（预览用，不落库）
# ---------------------------------------------------------------------------

_MEANINGFUL_GAP_TYPES = {
    "BRAND_ABSENCE", "RECOMMENDATION_GAP", "RANK_GAP", "ENTITY_GAP",
    "MANUFACTURER_IDENTITY_GAP", "QUERY_INTENT_MISMATCH", "COMPETITOR_DOMINANCE",
    "CITATION_GAP", "SOURCE_AUTHORITY_GAP", "INDUSTRY_KNOWLEDGE_GAP",
}


def build_task_suggestion(item, run_id=None):
    """item: 一条 diagnosis_items 记录（dict，storage.get_run_items 的返回形状）。
    返回一个可以直接拿去展示"加入内容矩阵"预览窗口、或用户确认后原样传给
    create_content_task 的建议 dict（此函数本身不落库）。
    """
    gaps = item.get("gaps") or []
    gap_types = [g.get("type") for g in gaps if g.get("type") in _MEANINGFUL_GAP_TYPES]
    action_priorities = [a.get("priority") for a in (item.get("actions") or [])]
    action_types = [a.get("action_type") for a in (item.get("actions") or [])]

    query_intent = item.get("query_intent")
    commercial_value = item.get("commercial_value")
    content_cluster = infer_content_cluster(query_intent, gap_types)
    content_type = infer_content_type(content_cluster)
    priority = infer_priority(query_intent, commercial_value, action_priorities)

    target_query = item.get("question", "")
    main_title, alt_titles = suggest_titles(target_query, content_cluster)
    outline = suggest_outline(main_title, content_cluster)

    return {
        "title": main_title,
        "content_cluster": content_cluster,
        "content_type": content_type,
        "priority": priority,
        "target_query": target_query,
        "target_queries": [target_query] if target_query else [],
        "query_intent": query_intent,
        "commercial_value": commercial_value,
        "source_diagnosis_id": run_id if run_id is not None else item.get("diagnosis_id"),
        "source_diagnosis_item_ids": [item["id"]] if item.get("id") is not None else [],
        "geo_gaps": gap_types,
        "action_type": action_types[0] if action_types else None,
        "reason": gaps[0].get("evidence") if gaps else None,
        "suggested_title": main_title,
        "alt_titles": alt_titles,
        "content_angle": suggest_content_angle(content_cluster),
        "outline": outline,
        "key_points": suggest_key_points(content_cluster),
        "facts_required": facts_required_for_cluster(content_cluster),
        "entity_facts": [],
        "baseline_brand_mentioned": item.get("brand_mentioned"),
        "baseline_recommended": item.get("recommended"),
        "baseline_rank": item.get("rank"),
        "baseline_snapshot": {
            "per_query": [{
                "query": target_query,
                "brand_mentioned": item.get("brand_mentioned"),
                "recommended": item.get("recommended"),
                "rank": item.get("rank"),
                "competitors": item.get("competitors") or [],
                "citations": item.get("citations") or [],
            }],
        },
    }


# ---------------------------------------------------------------------------
# 深度诊断批量聚合：把20题里相似的Query聚合成少量内容任务建议（不是20题20篇）
# ---------------------------------------------------------------------------

def cluster_diagnosis_items_into_suggestions(items, run_id=None):
    """items: 一次深度诊断的 diagnosis_items 列表。
    聚合规则：只按 content_cluster 分组，不再叠加 query_intent——因为
    "厂家寻找""OEM""ODM"这三种intent的问题都会映射到同一个OEM_ODM集群，
    如果分组时还要求intent也完全一致，会把本该合并成一篇专题的相似问题
    拆成好几条几乎一样的建议（同一个标题重复出现好几次），这正是需求里
    明确要求避免的"20题生成20篇"的翻版问题。
    每组产出一条聚合后的建议，target_queries 覆盖组内所有Query。
    只对"有意义的Gap"（排除 NO_CLEAR_GAP、排除成功但已被品牌覆盖的题）的题目聚合，
    不是不分青红皂白把20题全部塞进建议里。
    """
    groups = {}  # cluster -> {queries, item_ids, gap_types(set), action_priorities, commercial_values, intents}
    for item in items:
        if item.get("status") != "success":
            continue
        gaps = item.get("gaps") or []
        gap_types = [g.get("type") for g in gaps if g.get("type") in _MEANINGFUL_GAP_TYPES]
        if not gap_types:
            continue  # 没有明确Gap的题目，不需要内容任务
        query_intent = item.get("query_intent")
        content_cluster = infer_content_cluster(query_intent, gap_types)
        g = groups.setdefault(content_cluster, {
            "queries": [], "item_ids": [], "gap_types": set(),
            "action_priorities": [], "commercial_values": [], "intents": [],
        })
        if item.get("question") and item["question"] not in g["queries"]:
            g["queries"].append(item["question"])
        if item.get("id") is not None:
            g["item_ids"].append(item["id"])
        g["gap_types"].update(gap_types)
        g["action_priorities"].extend(a.get("priority") for a in (item.get("actions") or []))
        g["commercial_values"].append(item.get("commercial_value"))
        if query_intent:
            g["intents"].append(query_intent)

    suggestions = []
    for content_cluster, g in groups.items():
        # 代表性intent：组内出现次数最多的那个，只用于展示/分类，不影响标题生成
        # （标题/大纲模板本身只依赖content_cluster）。
        query_intent = Counter(g["intents"]).most_common(1)[0][0] if g["intents"] else None
        content_type = infer_content_type(content_cluster)
        commercial_value = "high" if "high" in g["commercial_values"] else (
            "medium" if "medium" in g["commercial_values"] else "low"
        )
        priority = infer_priority(query_intent, commercial_value, g["action_priorities"])
        # 用组内第一条Query作为标题生成的基础，标题描述的是这一整个专题而不是单个问题。
        representative_query = g["queries"][0] if g["queries"] else ""
        main_title, alt_titles = suggest_titles(representative_query, content_cluster)
        outline = suggest_outline(main_title, content_cluster)
        suggestions.append({
            "title": main_title,
            "content_cluster": content_cluster,
            "content_type": content_type,
            "priority": priority,
            "target_query": representative_query,
            "target_queries": g["queries"],
            "query_intent": query_intent,
            "commercial_value": commercial_value,
            "source_diagnosis_id": run_id,
            "source_diagnosis_item_ids": g["item_ids"],
            "geo_gaps": sorted(g["gap_types"]),
            "suggested_title": main_title,
            "alt_titles": alt_titles,
            "content_angle": suggest_content_angle(content_cluster),
            "outline": outline,
            "key_points": suggest_key_points(content_cluster),
            "facts_required": facts_required_for_cluster(content_cluster),
            "covered_query_count": len(g["queries"]),
        })

    suggestions.sort(key=lambda s: (_PRIORITY_ORDER.get(s["priority"], 9), -s["covered_query_count"]))
    return suggestions


# ---------------------------------------------------------------------------
# Dashboard 聚合统计
# ---------------------------------------------------------------------------

def compute_content_dashboard(tasks):
    """tasks: list[dict]（storage.list_content_tasks 的返回，不做任何筛选，全量传入）。
    全部基于真实存量任务统计，没有任务就是0，不编造数据。
    """
    total = len(tasks)
    by_priority = Counter(t.get("priority") for t in tasks)
    by_status = Counter(t.get("status") for t in tasks)

    from_diagnosis = sum(1 for t in tasks if t.get("source_diagnosis_id") is not None)
    high_value_query_count = sum(
        len(t.get("target_queries") or ([t["target_query"]] if t.get("target_query") else []))
        for t in tasks if t.get("commercial_value") == "high"
    )

    gap_counter = Counter()
    for t in tasks:
        for g in (t.get("geo_gaps") or []):
            gap_counter[g] += 1

    waiting_retest = sum(1 for t in tasks if t.get("retest_status") == "waiting")

    return {
        "total_tasks": total,
        "p1_count": by_priority.get("P1", 0),
        "p2_count": by_priority.get("P2", 0),
        "p3_count": by_priority.get("P3", 0),
        "status_counts": {s: by_status.get(s, 0) for s in STATUS_VALUES},
        "from_diagnosis_count": from_diagnosis,
        "high_value_query_count": high_value_query_count,
        "brand_absence_count": gap_counter.get("BRAND_ABSENCE", 0),
        "citation_gap_count": gap_counter.get("CITATION_GAP", 0),
        "manufacturer_identity_gap_count": gap_counter.get("MANUFACTURER_IDENTITY_GAP", 0),
        "waiting_retest_count": waiting_retest,
    }


# ---------------------------------------------------------------------------
# Baseline vs Retest 对比
# ---------------------------------------------------------------------------

def compare_baseline_retest(baseline_snapshot, retest_snapshot):
    """baseline_snapshot / retest_snapshot: {"per_query": [{"query","brand_mentioned",...}, ...]}
    返回每个Query维度的提及数对比 + 一个不做复杂打分的简单结论
    （结果改善 / 无明显变化 / 结果下降）。
    """
    baseline_items = (baseline_snapshot or {}).get("per_query", [])
    retest_items = (retest_snapshot or {}).get("per_query", [])

    baseline_mentioned = sum(1 for it in baseline_items if it.get("brand_mentioned"))
    retest_mentioned = sum(1 for it in retest_items if it.get("brand_mentioned"))
    total_queries = max(len(baseline_items), len(retest_items))

    baseline_recommended = sum(1 for it in baseline_items if it.get("recommended"))
    retest_recommended = sum(1 for it in retest_items if it.get("recommended"))

    if not retest_items:
        verdict = "尚未复测"
    elif retest_mentioned > baseline_mentioned or retest_recommended > baseline_recommended:
        verdict = "结果改善"
    elif retest_mentioned < baseline_mentioned or retest_recommended < baseline_recommended:
        verdict = "结果下降"
    else:
        verdict = "无明显变化"

    return {
        "total_queries": total_queries,
        "baseline_mentioned_count": baseline_mentioned,
        "retest_mentioned_count": retest_mentioned,
        "baseline_recommended_count": baseline_recommended,
        "retest_recommended_count": retest_recommended,
        "verdict": verdict,
    }
