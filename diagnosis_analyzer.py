"""
GEO 诊断分析模块 - 第一版

把 brand_parser 产出的"数据提取结果"（品牌是否提及/推荐/排名/竞品/引用来源）
进一步转化为"现状 → 问题 → 证据 → 原因类型 → 改善动作"的诊断结果。

设计原则（和 brand_parser.py 保持一致）：
- 只用确定性规则（关键词匹配 / 简单结构识别），不调用任何AI模型去"猜"原因。
- 严格区分三类内容：
  observations（客观事实，直接来自 brand_parser 的结构化字段，不含推断）
  inferences（带"可能/更可能/从当前回答看"这类证据边界词的推断，禁止写成断言）
  actions（改善建议，必须能追溯到某一个具体 Gap）
- 每一个 Gap 必须带 evidence，禁止输出"品牌影响力不足"这种没有证据的空话。
- industry_knowledge_quality 只给 high/medium/low/unknown 四档 + 理由，不做虚假的0-100打分。
- 规则覆盖不到的情况一律返回 unknown / NO_CLEAR_GAP，不做没有证据支撑的推测。

已知局限：
- Query Intent 分类、回答匹配度、行业认知质量，都是基于关键词命中的规则判断，
  不是语义理解，遇到用词生僻或委婉表达的问题/回答时可能分类不准，建议人工抽样核对。
- 一句话里同时议论多个品牌时（如"厂家推荐工厂能力强，普能达仅供参考"），规则无法
  精确区分某个信号词具体指向哪个品牌，这是和 brand_parser 里说明的一样的启发式局限。
"""

import re

from brand_parser import BRAND_ALIASES

BRAND_NAME_CN = BRAND_ALIASES[0]
BRAND_NAME_EN = BRAND_ALIASES[1]
BRAND_WEBSITE = "https://www.pnda.com.cn"
INDUSTRY = "车载冰箱"

# ---------------------------------------------------------------------------
# Query Intent 分类
# ---------------------------------------------------------------------------

INTENT_LABELS_CN = {
    "consumer_brand": "C端品牌",
    "consumer_product": "C端产品",
    "manufacturer": "厂家寻找",
    "OEM": "OEM",
    "ODM": "ODM",
    "automotive_supplier": "主机厂/配套",
    "distributor_procurement": "经销商采购",
    "technical": "技术问题",
    "usage": "使用问题",
    "after_sales": "售后问题",
    "comparison": "对比问题",
    "other": "其他",
}

# 需要区分"制造商/OEM身份"与"消费品牌口碑"的Query Intent集合，
# 用于回答匹配度判断、行业认知判断、MANUFACTURER_IDENTITY_GAP 判定。
MANUFACTURER_INTENTS = {"manufacturer", "OEM", "ODM", "automotive_supplier"}

_AUTO_SUPPLIER_KEYWORDS = ["主机厂", "整车厂", "车厂配套", "配套经验"]
_DISTRIBUTOR_KEYWORDS = ["经销商", "代理商", "分销", "批发", "采购渠道", "招商加盟", "招商"]
_MANUFACTURER_KEYWORDS = ["厂家", "生产商", "制造商", "代工厂", "生产厂"]
_COMPARISON_KEYWORDS = ["对比", "比较", "哪个更好", "区别", "PK"]
_AFTER_SALES_KEYWORDS = ["售后", "保修", "维修", "退换货", "质保"]
_USAGE_KEYWORDS = ["怎么用", "使用方法", "耗电", "怎么安装", "如何使用", "怎么接电", "怎么保养", "使用寿命"]
_TECHNICAL_KEYWORDS = ["参数", "原理", "技术规格", "压缩机技术", "选型", "制冷量", "半导体制冷", "压缩机制冷"]
_CONSUMER_PRODUCT_KEYWORDS = ["哪款", "哪个型号", "买哪个", "哪一款", "选哪个型号"]
_CONSUMER_BRAND_KEYWORDS = ["品牌好", "什么牌子", "牌子好", "品牌推荐", "哪个品牌", "哪家品牌"]


def classify_query_intent(question: str) -> str:
    """基于关键词命中的规则分类，命中优先级从"更具体"到"更笼统"排列，
    避免"哪些车载冰箱厂家有主机厂配套经验"这种同时含"厂家"和"主机厂"的问题
    被笼统归到厂家寻找，而不是更具体的主机厂/配套。
    """
    q = question or ""
    ql = q.lower()

    if "oem" in ql:
        return "OEM"
    if "odm" in ql:
        return "ODM"
    if any(kw in q for kw in _AUTO_SUPPLIER_KEYWORDS):
        return "automotive_supplier"
    if any(kw in q for kw in _DISTRIBUTOR_KEYWORDS):
        return "distributor_procurement"
    if any(kw in q for kw in _MANUFACTURER_KEYWORDS):
        return "manufacturer"
    if any(kw in q for kw in _COMPARISON_KEYWORDS) or "vs" in ql:
        return "comparison"
    if any(kw in q for kw in _AFTER_SALES_KEYWORDS):
        return "after_sales"
    if any(kw in q for kw in _USAGE_KEYWORDS):
        return "usage"
    if any(kw in q for kw in _TECHNICAL_KEYWORDS):
        return "technical"
    if any(kw in q for kw in _CONSUMER_PRODUCT_KEYWORDS):
        return "consumer_product"
    if any(kw in q for kw in _CONSUMER_BRAND_KEYWORDS):
        return "consumer_brand"
    return "other"


# ---------------------------------------------------------------------------
# 回答匹配度 / 行业认知质量 —— 共用的信号词
# ---------------------------------------------------------------------------

MANUFACTURER_SIGNAL_KEYWORDS = [
    "工厂", "代工", "生产能力", "制造商", "OEM", "ODM", "产能",
    "供应商", "出口", "贴牌", "生产厂家", "配套能力",
]
CONSUMER_SIGNAL_KEYWORDS = [
    "值得购买", "性价比高", "选购建议", "消费者", "值得入手",
    "口碑不错", "口碑较好", "购买时", "用户评价", "值得推荐",
]
COOLING_TECH_TERMS = ["压缩机式", "半导体制冷", "压缩机制冷", "半导体式", "压缩机技术"]
# 已知的高频"品类混淆"表述，规则很窄，只覆盖明确的误用写法，不做语义推断。
CATEGORY_CONFUSION_HINTS = ["车载冰箱也叫车载空调", "车载冰箱就是车载空调", "车载冰箱等同于车载冷风扇"]

_MIN_MEANINGFUL_LENGTH = 8


def judge_answer_fit(query_intent: str, raw_answer: str):
    """返回 (answer_fit, evidence)。answer_fit ∈ good/partial/poor/unknown。
    只依据关键词命中的规则判断，命中/未命中的具体词都写进 evidence，不凭感觉判断。
    """
    text = (raw_answer or "").strip()
    intent_label = INTENT_LABELS_CN.get(query_intent, query_intent)

    if len(text) < _MIN_MEANINGFUL_LENGTH:
        return "unknown", "原始回答内容过短，没有足够信息判断是否回应了问题意图。"

    if query_intent in MANUFACTURER_INTENTS:
        mfr_hits = [kw for kw in MANUFACTURER_SIGNAL_KEYWORDS if kw in text]
        consumer_hits = [kw for kw in CONSUMER_SIGNAL_KEYWORDS if kw in text]
        if mfr_hits and not consumer_hits:
            return "good", f'回答中出现制造商/OEM相关表述（如"{mfr_hits[0]}"），与"{intent_label}"这一问题意图一致。'
        if mfr_hits and consumer_hits:
            return (
                "partial",
                f'回答中同时出现制造商相关表述（如"{mfr_hits[0]}"）和消费者购买导向表述'
                f'（如"{consumer_hits[0]}"），未能明确聚焦制造商/OEM身份。',
            )
        if consumer_hits and not mfr_hits:
            return (
                "partial",
                f'问题要求"{intent_label}"，但回答主要围绕消费者购买/品牌口碑展开'
                f'（如出现"{consumer_hits[0]}"），未见明显制造商/工厂/OEM相关表述。',
            )
        return "unknown", "回答内容不足以判断是否覆盖了制造商/OEM角度。"

    if query_intent == "comparison":
        if any(kw in text for kw in _COMPARISON_KEYWORDS):
            return "good", "回答中出现明确的对比类表述，与问题意图一致。"
        return "unknown", "未在回答中识别到明确的对比类表述，不足以判断匹配度。"

    # 其余Query Intent（consumer_brand / consumer_product / technical / usage /
    # after_sales / distributor_procurement / other）：只做"是否围绕行业话题展开"
    # 这个最基础的规则判断，不做更细的语义匹配。
    if INDUSTRY in text or any(alias in text for alias in BRAND_ALIASES):
        return "good", f'回答内容围绕"{INDUSTRY}"相关话题展开，与问题意图基本一致。'
    return "unknown", f'回答中未出现"{INDUSTRY}"等行业相关词，不足以判断是否匹配问题意图。'


def assess_industry_knowledge(query_intent: str, raw_answer: str, answer_fit: str):
    """返回 (industry_knowledge_quality, reason)。
    quality ∈ high/medium/low/unknown，不做0-100打分，reason 必须是观察到的具体信号。
    """
    text = (raw_answer or "").strip()
    if not text:
        return "unknown", "原始回答为空，没有足够信息判断行业认知质量。"

    positive_signals = []
    negative_signals = []

    # 维度1：是否混淆品牌商和制造商（仅在厂家/OEM类Query下适用）
    if query_intent in MANUFACTURER_INTENTS:
        mfr_hits = [kw for kw in MANUFACTURER_SIGNAL_KEYWORDS if kw in text]
        consumer_hits = [kw for kw in CONSUMER_SIGNAL_KEYWORDS if kw in text]
        if consumer_hits and not mfr_hits:
            negative_signals.append("厂家/OEM类问题下，回答仍以消费品牌购买建议为主，未区分制造商与品牌商概念")
        elif mfr_hits:
            positive_signals.append("能够在回答中区分制造商/OEM与消费品牌的表述")

    # 维度2：是否出现明显类别错误（窄规则，只覆盖已知的高频混淆写法）
    hit_confusion = [h for h in CATEGORY_CONFUSION_HINTS if h in text]
    if hit_confusion:
        negative_signals.append(f'出现疑似品类混淆表述："{hit_confusion[0]}"')

    # 维度3：是否能正确使用压缩机式/半导体制冷等行业分类术语
    tech_hits = [kw for kw in COOLING_TECH_TERMS if kw in text]
    if tech_hits:
        positive_signals.append(f'能够正确使用"{tech_hits[0]}"等行业制冷技术分类术语')

    # 维度5：是否真正回答Query（回答匹配度的结论直接复用）
    if answer_fit == "good":
        positive_signals.append("回答与问题意图基本匹配")
    elif answer_fit == "poor":
        negative_signals.append("回答与问题意图明显不匹配")

    # 维度4（是否有可信引用来源）由调用方在 detect_gaps 里结合 citations 单独判断，
    # 此处不重复处理，避免和 CITATION_GAP / SOURCE_AUTHORITY_GAP 的证据来源打架。

    if negative_signals and not positive_signals:
        return "low", "；".join(negative_signals) + "。"
    if negative_signals and positive_signals:
        return (
            "medium",
            "同时观察到正向信号（" + "；".join(positive_signals) + "）和负向信号（"
            + "；".join(negative_signals) + "），暂定为中等。",
        )
    if positive_signals:
        level = "high" if len(positive_signals) >= 2 else "medium"
        return level, "；".join(positive_signals) + "。"
    return "unknown", "当前回答中未观察到足够的行业认知相关信号，无法判断。"


# ---------------------------------------------------------------------------
# GEO Gap 诊断
# ---------------------------------------------------------------------------

GAP_LABELS_CN = {
    "BRAND_ABSENCE": "品牌未进入AI答案",
    "RECOMMENDATION_GAP": "品牌被提及但未被推荐",
    "RANK_GAP": "品牌出现但排序靠后",
    "ENTITY_GAP": "品牌实体认知不足",
    "MANUFACTURER_IDENTITY_GAP": "制造商/OEM身份未被识别",
    "QUERY_INTENT_MISMATCH": "回答与问题意图不匹配",
    "COMPETITOR_DOMINANCE": "该问题被竞品明显占据",
    "CITATION_GAP": "缺少可验证引用来源",
    "SOURCE_AUTHORITY_GAP": "引用来源权威性不足",
    "INDUSTRY_KNOWLEDGE_GAP": "行业认知存在不足",
    "NO_CLEAR_GAP": "暂无明确Gap",
}

# 引用来源里常见的UGC/论坛类平台，命中即视为"权威性存疑"的具体证据。
# 只在"全部引用来源都命中"时才下 SOURCE_AUTHORITY_GAP 的结论，避免主观臆断。
LOW_AUTHORITY_DOMAIN_HINTS = [
    "zhihu.com", "zhidao.baidu", "tieba.baidu", "xiaohongshu.com",
    "douyin.com", "toutiao.com", "bilibili.com", "weibo.com",
]

_RANK_GAP_THRESHOLD = 3  # 排名 > 3 视为"靠后"


def _gap(gap_type: str, evidence: str) -> dict:
    return {"type": gap_type, "label": GAP_LABELS_CN.get(gap_type, gap_type), "evidence": evidence}


def detect_gaps(
    question: str,
    raw_answer: str,
    parsed: dict,
    query_intent: str,
    answer_fit: str,
    industry_knowledge_quality: str,
    industry_knowledge_reason: str,
) -> list:
    intent_label = INTENT_LABELS_CN.get(query_intent, query_intent)
    text = raw_answer or ""

    brand_mentioned = bool(parsed.get("brand_mentioned"))
    mention_count = parsed.get("mention_count", 0) or 0
    recommended = bool(parsed.get("recommended"))
    rank = parsed.get("rank")
    competitors = parsed.get("competitors") or []
    citations = parsed.get("citations") or []

    gaps = []

    if not brand_mentioned:
        comp_str = f'（{"、".join(competitors[:6])}）' if competitors else ""
        evidence = (
            f'本次回答共识别到 {len(competitors)} 个竞品品牌{comp_str}，'
            f'但未出现"{BRAND_NAME_CN}"或"{BRAND_NAME_EN}"。'
        )
        gaps.append(_gap("BRAND_ABSENCE", evidence))
    else:
        if not recommended:
            evidence = f'"{BRAND_NAME_CN}"被提及 {mention_count} 次，但回答中没有将其判定为推荐/首选的表述。'
            gaps.append(_gap("RECOMMENDATION_GAP", evidence))

        if rank is not None and rank > _RANK_GAP_THRESHOLD:
            evidence = f'"{BRAND_NAME_CN}"在识别到的排序结构中位列第 {rank} 位，排序靠后。'
            gaps.append(_gap("RANK_GAP", evidence))

        if mention_count <= 1 and not recommended and rank is None:
            evidence = (
                f'"{BRAND_NAME_CN}"仅被一笔带过地提及 {mention_count} 次，'
                f'回答中没有围绕它给出任何定性描述（未推荐、未排序）。'
            )
            gaps.append(_gap("ENTITY_GAP", evidence))

        if query_intent in MANUFACTURER_INTENTS:
            mfr_hits = [kw for kw in MANUFACTURER_SIGNAL_KEYWORDS if kw in text]
            if not mfr_hits:
                evidence = (
                    f'问题涉及"{intent_label}"，"{BRAND_NAME_CN}"虽被提及，但回答中未出现'
                    f'制造商/OEM/工厂相关表述，无法确认AI将其识别为制造商身份。'
                )
                gaps.append(_gap("MANUFACTURER_IDENTITY_GAP", evidence))

    if answer_fit in ("partial", "poor"):
        degree = "部分" if answer_fit == "partial" else "明显"
        evidence = f'问题被分类为"{intent_label}"，但回答内容{degree}偏离该意图（详见回答匹配度判断依据）。'
        gaps.append(_gap("QUERY_INTENT_MISMATCH", evidence))

    if not brand_mentioned and len(competitors) >= 3:
        evidence = f'回答中出现了 {len(competitors)} 个竞品品牌（{"、".join(competitors[:6])}），"{BRAND_NAME_CN}"未进入候选范围。'
        gaps.append(_gap("COMPETITOR_DOMINANCE", evidence))

    if not citations:
        gaps.append(_gap("CITATION_GAP", "原始回答未包含任何URL或可识别的引用来源。"))
    else:
        low_hits = [u for u in citations if any(h in u for h in LOW_AUTHORITY_DOMAIN_HINTS)]
        if low_hits and len(low_hits) == len(citations):
            evidence = (
                f'回答给出的 {len(citations)} 个引用来源均为社区/UGC类平台（如 {low_hits[0]}），'
                f'未见官方或权威媒体来源。'
            )
            gaps.append(_gap("SOURCE_AUTHORITY_GAP", evidence))

    if industry_knowledge_quality == "low":
        gaps.append(_gap("INDUSTRY_KNOWLEDGE_GAP", industry_knowledge_reason))

    if not gaps:
        gaps.append(_gap("NO_CLEAR_GAP", "当前证据不足以识别出明确的GEO Gap。"))

    return gaps


# ---------------------------------------------------------------------------
# Observations / Inferences
# ---------------------------------------------------------------------------

def build_observations(question: str, parsed: dict, query_intent_label: str) -> list:
    """纯客观事实陈述，直接来自 brand_parser 的结构化字段，不含任何推断措辞。"""
    brand_mentioned = bool(parsed.get("brand_mentioned"))
    mention_count = parsed.get("mention_count", 0) or 0
    rank = parsed.get("rank")
    competitors = parsed.get("competitors") or []
    citations = parsed.get("citations") or []

    obs = [f'问题"{question}"被分类为"{query_intent_label}"（Query Intent）。']
    if brand_mentioned:
        obs.append(f'"{BRAND_NAME_CN}"/"{BRAND_NAME_EN}"在原始回答中被提及 {mention_count} 次。')
    else:
        obs.append(f'"{BRAND_NAME_CN}"/"{BRAND_NAME_EN}"未在原始回答中出现。')
    obs.append(f'识别到的排序信息：{("第%d位" % rank) if rank is not None else "无（未识别到明确的排序结构）"}。')
    obs.append(f'识别到的竞品品牌：{("、".join(competitors)) if competitors else "无"}。')
    obs.append(f'识别到的引用来源数量：{len(citations)}。')
    return obs


def build_inferences(gaps: list) -> list:
    """带证据边界词的推断，禁止把推断写成事实。"""
    inferences = []
    for g in gaps:
        if g["type"] == "NO_CLEAR_GAP":
            continue
        inferences.append(f'从当前回答看，可能存在"{g["label"]}"问题：{g["evidence"]}')
    if not inferences:
        inferences.append("从当前回答看，暂未观察到需要重点关注的问题，仍建议结合更多问题样本持续观察。")
    return inferences


def build_diagnosis_summary(
    question: str,
    parsed: dict,
    query_intent_label: str,
    answer_fit: str,
    gaps: list,
) -> str:
    """控制在大致100-200字，格式类似需求文档给出的示例，保留"可能/更可能/从当前回答看"
    这类证据边界词，不把推断写成事实。
    """
    brand_mentioned = bool(parsed.get("brand_mentioned"))
    mention_count = parsed.get("mention_count", 0) or 0

    if brand_mentioned:
        opening = f'在"{question}"这一问题中，DeepSeek提及了{BRAND_NAME_CN}/{BRAND_NAME_EN}（{mention_count}次）'
    else:
        opening = f'在"{question}"这一问题中，DeepSeek未提及{BRAND_NAME_CN}/{BRAND_NAME_EN}'

    clauses = [opening]
    if answer_fit == "partial":
        clauses.append(f'回答与"{query_intent_label}"这一问题意图存在一定偏差')
    elif answer_fit == "poor":
        clauses.append(f'回答明显偏离了"{query_intent_label}"这一问题意图')

    gap_types = [g["type"] for g in gaps if g["type"] != "NO_CLEAR_GAP"]
    if "CITATION_GAP" in gap_types:
        clauses.append("同时未提供外部引用来源")

    body = "，".join(clauses) + "。"

    tail_gap_labels = [
        GAP_LABELS_CN.get(t, t) for t in gap_types if t != "CITATION_GAP"
    ][:2]
    if tail_gap_labels:
        tail = (
            f'当前结果更可能反映出{"、".join(tail_gap_labels)}等问题，'
            f'具体原因需结合官网内容覆盖及第三方信源进一步核实，从当前回答看暂不能确定根本原因。'
        )
    else:
        tail = "从当前回答看暂未发现需要重点关注的问题，建议结合更多问题样本持续观察后续多轮诊断结果。"

    return body + tail


# ---------------------------------------------------------------------------
# 改善建议：Gap -> Action 的显式映射表，保证每条 action 都能追溯到具体 Gap
# ---------------------------------------------------------------------------

GAP_ACTION_MAP = {
    "BRAND_ABSENCE": [
        {
            "priority": "P1",
            "action_type": "ENTITY_BUILDING",
            "action": f'在权威第三方平台（行业媒体、问答社区等）补充{BRAND_NAME_CN}/{BRAND_NAME_EN}的品牌介绍与实体信息，提升被AI模型检索到的概率。',
        },
    ],
    "RECOMMENDATION_GAP": [
        {
            "priority": "P2",
            "action_type": "CASE_STUDY",
            "action": f'产出{BRAND_NAME_CN}产品的真实使用评测/案例内容，强化正面推荐信号。',
        },
    ],
    "RANK_GAP": [
        {
            "priority": "P2",
            "action_type": "THIRD_PARTY_MEDIA",
            "action": f'增加第三方媒体对{BRAND_NAME_CN}的对比测评覆盖，提升在同类品牌列表中的排序位置。',
        },
    ],
    "ENTITY_GAP": [
        {
            "priority": "P2",
            "action_type": "STRUCTURED_DATA",
            "action": f'在官网和第三方百科类平台完善{BRAND_NAME_CN}的结构化品牌信息（成立时间/主营产品/资质等）。',
        },
    ],
    "MANUFACTURER_IDENTITY_GAP": [
        {
            "priority": "P1",
            "action_type": "OEM_CONTENT",
            "action": f'在官网建立"{INDUSTRY}OEM/ODM制造能力"专题页，明确展示{BRAND_NAME_CN}的制造商/代工身份。',
        },
    ],
    "QUERY_INTENT_MISMATCH": [
        {
            "priority": "P1",
            "action_type": "OEM_CONTENT",
            "action": f'针对"{INDUSTRY}厂家/OEM"类查询产出专门内容，明确区分品牌商与制造商定位，避免与消费品牌概念混淆。',
        },
    ],
    "COMPETITOR_DOMINANCE": [
        {
            "priority": "P2",
            "action_type": "INDUSTRY_CONTENT",
            "action": f'分析当前占据答案的竞品内容策略，针对性补充{BRAND_NAME_CN}在同类问题下的差异化内容。',
        },
    ],
    "CITATION_GAP": [
        {
            "priority": "P2",
            "action_type": "THIRD_PARTY_MEDIA",
            "action": f'推动权威媒体/行业网站发布可被引用的{BRAND_NAME_CN}相关报道或测评，增加可被AI引用的信源。',
        },
    ],
    "SOURCE_AUTHORITY_GAP": [
        {
            "priority": "P2",
            "action_type": "THIRD_PARTY_MEDIA",
            "action": "争取行业协会、主流媒体等高权威站点的报道或收录，替代当前以社区/UGC为主的引用来源。",
        },
    ],
    "INDUSTRY_KNOWLEDGE_GAP": [
        {
            "priority": "P3",
            "action_type": "INDUSTRY_CONTENT",
            "action": f'产出{INDUSTRY}行业科普内容（压缩机式/半导体制冷分类、选购要点等），帮助模型建立正确的行业认知。',
        },
    ],
    "NO_CLEAR_GAP": [
        {
            "priority": "P3",
            "action_type": "RETEST",
            "action": "当前证据不足以定位明确问题，建议扩大测试问题样本后重新诊断。",
        },
    ],
}

_PRIORITY_ORDER = {"P1": 0, "P2": 1, "P3": 2}


def build_action_items(gaps: list, question: str) -> list:
    items = []
    seen = set()
    for g in gaps:
        for tmpl in GAP_ACTION_MAP.get(g["type"], []):
            key = (tmpl["action_type"], tmpl["action"])
            if key in seen:
                continue
            seen.add(key)
            items.append({
                "priority": tmpl["priority"],
                "action_type": tmpl["action_type"],
                "action": tmpl["action"],
                "reason": g["evidence"],
                "target_query": question,
                "gap_type": g["type"],  # 便于追溯 action 与 gap 的对应关系
            })
    items.sort(key=lambda x: _PRIORITY_ORDER.get(x["priority"], 9))
    return items


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def diagnose(question: str, raw_answer: str, parsed: dict) -> dict:
    """把 brand_parser.parse_geo_answer 的结构化结果，进一步转化为GEO诊断结果。

    参数：
        question: 用户提出的原始问题
        raw_answer: DeepSeek（或其他平台）返回的原始回答文本
        parsed: brand_parser.parse_geo_answer(raw_answer, model) 的返回值，
            至少包含 brand_mentioned / mention_count / recommended / rank /
            competitors / citations 这些字段。

    返回：
        dict，包含 query_intent、answer_fit、industry_knowledge_quality、
        gaps、observations、inferences、diagnosis_summary、action_items。
    """
    question = question or ""
    raw_answer = raw_answer or ""
    parsed = parsed or {}

    query_intent = classify_query_intent(question)
    query_intent_label = INTENT_LABELS_CN[query_intent]

    answer_fit, answer_fit_evidence = judge_answer_fit(query_intent, raw_answer)

    industry_knowledge_quality, industry_knowledge_reason = assess_industry_knowledge(
        query_intent, raw_answer, answer_fit
    )

    gaps = detect_gaps(
        question, raw_answer, parsed, query_intent, answer_fit,
        industry_knowledge_quality, industry_knowledge_reason,
    )

    observations = build_observations(question, parsed, query_intent_label)
    inferences = build_inferences(gaps)
    diagnosis_summary = build_diagnosis_summary(question, parsed, query_intent_label, answer_fit, gaps)
    action_items = build_action_items(gaps, question)

    return {
        "question": question,
        "query_intent": query_intent,
        "query_intent_label": query_intent_label,
        "answer_fit": answer_fit,
        "answer_fit_evidence": answer_fit_evidence,
        "industry_knowledge_quality": industry_knowledge_quality,
        "industry_knowledge_reason": industry_knowledge_reason,
        "gaps": gaps,
        "observations": observations,
        "inferences": inferences,
        "diagnosis_summary": diagnosis_summary,
        "action_items": action_items,
    }
