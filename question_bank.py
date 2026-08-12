"""
GEO 深度诊断 - 正式问题库

不做每次完全随机生成问题：这里维护一份可复用、覆盖11个Query Intent的
真实问题库，深度诊断按用户选择的问题类型（或默认分布）从库里抽取固定
数量的问题，抽取过程是确定性的（轮询各intent），不使用随机数，方便
测试和复现。

commercial_value（业务价值）标注原则：
- 普能达/PUNEDA 是车载冰箱制造商（B2B/OEM/ODM属性明显），因此
  厂家寻找/OEM/ODM/主机厂配套/经销商采购 这五类 Query 对应真实商业决策
  （找工厂、找代工、找供应商），标为 high。
- C端消费者场景（品牌好不好、买哪个型号、使用/售后）流量大但决策权重
  相对分散，标为 medium/low。
- 技术/对比类问题标为 medium：有一定参考价值，但不直接对应采购决策。

Query Intent 取值和 diagnosis_analyzer.INTENT_LABELS_CN 保持一致，
保证问题库标注的intent和diagnosis_analyzer实际分类结果尽量吻合
（问题文案里特意包含了能触发对应intent关键词规则的词）。
"""

# (question, query_intent, commercial_value)
_RAW_QUESTIONS = [
    # ---- C端品牌 (consumer_brand) ----
    ("车载冰箱哪个品牌好？", "consumer_brand", "medium"),
    ("车载冰箱什么牌子靠谱？", "consumer_brand", "medium"),
    ("自驾游车载冰箱哪个品牌好？", "consumer_brand", "medium"),
    ("露营车载冰箱哪个品牌口碑好？", "consumer_brand", "medium"),
    ("房车用车载冰箱选哪个品牌好？", "consumer_brand", "low"),

    # ---- C端产品 (consumer_product) ----
    ("车载冰箱买哪个型号性价比高？", "consumer_product", "low"),
    ("压缩机车载冰箱哪一款制冷效果好？", "consumer_product", "low"),
    ("半导体车载冰箱选哪个型号省电？", "consumer_product", "low"),
    ("车载冰箱哪款适合自驾长途用？", "consumer_product", "low"),

    # ---- 厂家寻找 (manufacturer) —— B2B高价值 ----
    ("车载冰箱厂家推荐", "manufacturer", "high"),
    ("中国有哪些做车载冰箱的生产厂家？", "manufacturer", "high"),
    ("压缩机车载冰箱的生产商都有哪些？", "manufacturer", "high"),
    ("半导体车载冰箱制造商推荐一下", "manufacturer", "high"),
    ("出口用车载冰箱的生产厂家有哪些？", "manufacturer", "high"),

    # ---- OEM —— B2B高价值 ----
    ("中国车载冰箱OEM厂家有哪些？", "OEM", "high"),
    ("车载冰箱OEM代工推荐哪些厂家？", "OEM", "high"),
    ("Portable refrigerator OEM manufacturer China", "OEM", "high"),

    # ---- ODM —— B2B高价值 ----
    ("车载冰箱ODM开发能力强的厂家有哪些？", "ODM", "high"),
    ("哪些厂家可以做车载冰箱ODM定制开发？", "ODM", "high"),
    ("Car refrigerator ODM manufacturer recommendation", "ODM", "high"),

    # ---- 主机厂/汽车配套 (automotive_supplier) —— B2B高价值 ----
    ("哪些车载冰箱厂家有汽车主机厂配套经验？", "automotive_supplier", "high"),
    ("能给整车厂做车载冰箱配套的厂家有哪些？", "automotive_supplier", "high"),
    ("有主机厂配套经验的车载冰箱供应商推荐", "automotive_supplier", "high"),

    # ---- 经销商采购 (distributor_procurement) —— B2B高价值 ----
    ("车载冰箱经销商采购渠道推荐", "distributor_procurement", "high"),
    ("车载冰箱代理商招商，有哪些厂家可以合作？", "distributor_procurement", "high"),
    ("批发车载冰箱去哪里找靠谱厂家？", "distributor_procurement", "medium"),

    # ---- 技术能力 (technical) ----
    ("压缩机车载冰箱的制冷原理是什么？", "technical", "medium"),
    ("车载冰箱压缩机技术选型要注意什么参数？", "technical", "medium"),
    ("车载冰箱制冷量参数怎么选？", "technical", "medium"),

    # ---- 使用场景 (usage) ----
    ("车载冰箱耗电大吗，怎么使用更省电？", "usage", "low"),
    ("车载冰箱在房车上怎么安装？", "usage", "low"),
    ("自驾游车载冰箱使用方法和注意事项", "usage", "low"),

    # ---- 售后 (after_sales) ----
    ("车载冰箱售后一般保修多久？", "after_sales", "medium"),
    ("车载冰箱压缩机坏了保修怎么处理？", "after_sales", "medium"),

    # ---- 对比 (comparison) ----
    ("车载冰箱压缩机式和半导体式对比哪个更好？", "comparison", "medium"),
    ("车载冰箱不同品牌对比，哪个更值得买？", "comparison", "medium"),
]

QUESTION_BANK = [
    {"question": q, "query_intent": intent, "commercial_value": value}
    for q, intent, value in _RAW_QUESTIONS
]

ALL_INTENTS = sorted({item["query_intent"] for item in QUESTION_BANK})

# 默认20题分布：偏向B2B高价值Query（厂家/OEM/ODM/主机厂配套/经销商采购），
# 不能让20题全部是C端消费者问题。总计20题，其中12题来自五个B2B高价值intent。
DEFAULT_INTENT_WEIGHTS = {
    "manufacturer": 3,
    "OEM": 3,
    "ODM": 2,
    "automotive_supplier": 2,
    "distributor_procurement": 2,
    "consumer_brand": 2,
    "consumer_product": 2,
    "comparison": 1,
    "technical": 1,
    "usage": 1,
    "after_sales": 1,
}


def _questions_by_intent():
    by_intent = {}
    for item in QUESTION_BANK:
        by_intent.setdefault(item["query_intent"], []).append(item)
    return by_intent


def select_questions(intents=None, count=20):
    """从问题库里确定性地抽取 count 道题。

    - intents 为空/None 时，按 DEFAULT_INTENT_WEIGHTS 的权重分布抽取，
      保证B2B高价值Query占较高比例，不会全部是C端消费者问题。
    - intents 非空时，只在选中的intent范围内轮询抽取，尽量均匀分布；
      如果选中的intent题量不够 count，题库内该intent的题目会被循环复用
      （标注顺序不变，不使用随机数，结果可复现）。
    - 不会返回重复的问题文本（同一轮询周期内跳过已选过的，除非题库
      本身不够，才允许循环复用）。
    """
    by_intent = _questions_by_intent()

    if intents:
        valid_intents = [i for i in intents if i in by_intent]
        if not valid_intents:
            valid_intents = list(DEFAULT_INTENT_WEIGHTS.keys())
        weights = {i: 1 for i in valid_intents}
    else:
        valid_intents = [i for i in DEFAULT_INTENT_WEIGHTS if i in by_intent]
        weights = {i: DEFAULT_INTENT_WEIGHTS[i] for i in valid_intents}

    # 按权重展开成一个抽取序列：例如 manufacturer 权重3，就在序列里出现3次，
    # 序列内部按intent顺序轮询，保证不是「先把一个intent抽满再抽下一个」。
    plan = []
    max_weight = max(weights.values())
    for round_idx in range(max_weight):
        for intent in valid_intents:
            if round_idx < weights[intent]:
                plan.append(intent)

    selected = []
    cursors = {i: 0 for i in valid_intents}
    used_questions = set()

    def _take_next(intent):
        pool = by_intent[intent]
        n = len(pool)
        for _ in range(n):
            idx = cursors[intent] % n
            cursors[intent] += 1
            candidate = pool[idx]
            if candidate["question"] not in used_questions:
                used_questions.add(candidate["question"])
                return dict(candidate)
        # 题库确实不够，允许复用（极端情况：intents选得太窄）
        candidate = pool[cursors[intent] % n]
        cursors[intent] += 1
        return dict(candidate)

    plan_idx = 0
    while len(selected) < count:
        if not plan:
            break
        intent = plan[plan_idx % len(plan)]
        selected.append(_take_next(intent))
        plan_idx += 1

    return selected[:count]
