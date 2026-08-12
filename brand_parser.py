"""
GEO 回答结构化解析模块 - 第一版
只用确定性规则（字符串匹配 / 正则 / 简单结构识别），不调用任何AI模型去"猜"排名或品牌。
拿不准的字段一律返回 None / 空值，由调用方（前端）显示为"无法判断"，绝不编造。

已知局限（如实说明，不夸大准确率）：
- 竞品品牌抽取是基于"编号列表项 / X品牌 / X车载冰箱 / X厂家"等文本模式的启发式规则，
  不是基于人工维护的品牌词库，也不是NLP实体识别模型，存在漏检和误检的可能，
  建议第一版上线后人工抽样核对，再考虑是否需要补充品牌词库或引入更强的解析方式。
- 排名判定只在能识别出"编号列表 / 中文序数词"结构、且本方品牌落在其中时才给出，
  没有这种结构就一律返回 None，不做任何基于常识的推测。
- 推荐语义判定以"句子"为单位（句子边界：。！？；;和换行），在同一句内查找推荐类关键词，
  不再依赖固定字符窗口。如果一句话里同时议论了多个品牌（例如"A是首选，B仅供参考"），
  规则解析无法区分关键词具体指向谁，这是和竞品抽取一样的、已知的启发式局限。
- 品牌提及的否定识别（如"没有提到XX"）只覆盖了常见的否定触发词组合，不是通用的NLP
  否定检测，遇到未覆盖的否定表达方式仍可能被误判为"提及"。
"""

import re
from datetime import datetime

BRAND_ALIASES = ["普能达", "PUNEDA"]

RECOMMEND_KEYWORDS = [
    "值得推荐", "推荐", "值得考虑", "可以考虑", "值得选择", "建议选择",
    "优选", "首选", "口碑较好", "口碑不错", "性价比高", "值得入手",
    "不错的选择", "值得关注",
]

# 推荐类关键词前紧邻这些否定词时不算数（"不推荐" "没有推荐" 等），
# 避免把负面表达误判成推荐，保持保守判断。
NEGATION_BEFORE_KEYWORD_PATTERN = re.compile(r'(?:不|没有|没|未|无|别|并不|从不|不太|也不)$')

# "没有提到XX" "未提及XX" 这类否定句式，紧跟其后的品牌名不能算作真实提及，
# 避免仅因为字符串里出现了品牌名就误判 brand_mentioned=True。
# 注意：这里只覆盖"是否被提及/包含"的否定，不包括"推荐/选择"——
# "不推荐XX"里的XX其实是被提到了，只是没有被推荐，那属于 recommended 的否定，
# 由 NEGATION_BEFORE_KEYWORD_PATTERN 单独处理，混进这里会把"提到但不推荐"
# 误判成"根本没提到"。
NEGATED_MENTION_PATTERN = re.compile(
    r'(?:没有|没|未|不|无|并未|也没|从未)'
    r'(?:提到|提及|包括|涉及|包含|出现|说到)'
    r'[^\n，。！？；;：:]{0,8}'
)

# 句子边界：中文强终止标点 + 分号 + 换行。逗号/顿号不算边界，
# 因为"品牌名与推荐语之间有描述文字"这种场景常用逗号分隔，需要留在同一句里判断。
SENTENCE_SPLIT_PATTERN = re.compile(r'[。！？\n；;]')

# 编号/项目符号列表项： "1. xxx" "1、xxx" "①xxx" "-xxx" "*xxx" 等
LIST_MARKER_PATTERN = re.compile(
    r'(?:^|\n)\s*(?:[0-9]{1,2}[.、)．]|[①②③④⑤⑥⑦⑧⑨⑩]|[-•*])\s*([^\n]+)'
)

ORDINAL_WORDS = ["第一", "第二", "第三", "第四", "第五", "第六", "第七", "第八"]

URL_PATTERN = re.compile(r'https?://[^\s\)\]\>，。；、"\'》]+')

# "XX品牌" "XX车载冰箱" "XX厂家" 这种紧邻关键词的命名模式。
# 边界故意收紧到"，,、:：（("这类"列举中的一项"信号，不包括句首(^)/句号(。)/
# 空白(\s)——真实DeepSeek长回答里，"国内品牌""这类品牌""关于...品牌""主流品牌"
# "有些品牌"这种泛泛而谈的描述句，几乎每一句都会在句首或空白后触发一次误抽取
# （曾经把"这类""关于""选择""主流""国内""有些"整批抽成"竞品品牌"）。用句首/句号/
# 空白做边界太容易撞上新起一句的位置，宁可漏检一些行文里句首出现的真实品牌名，
# 也不能让通用描述词大批量污染竞品统计。
BRAND_SUFFIX_PATTERN = re.compile(
    r'(?<=[，,、:：\(（])([A-Za-z一-龥]{2,8})(?:品牌|车载冰箱|厂家)'
)

# "品牌有A、B、C" "厂家推荐：A、B和C" "推荐以下品牌：A、B、C" 这种并列列表。
# 触发词支持"有/包括/如/推荐"这几个动词，也支持"品牌/厂家"后面直接跟冒号
# （真实DeepSeek回答里"以下几个品牌：A、B、C"这种冒号直接列举极常见，
# 原来要求动词紧跟冒号，漏掉了这种没有动词、直接用冒号列举的写法）。
# 分隔符支持顿号"、"及中文连接词"和""及"，这两种连接词在这类语境里同样表示
# "并列的最后一项"而不是另起一句；不用逗号/句号，因为它们通常表示切换到不相关的
# 另一个小句，用它们切分会把无关的后半句错误地并入候选列表。
BRAND_LIST_PATTERN = re.compile(
    r'(?:品牌|厂家)(?:(?:有|包括|如|推荐)[:：]?|[:：])\s*'
    r'((?:[A-Za-z一-龥]{2,8}(?:、|和|及))+[A-Za-z一-龥]{2,8}(?=[，,。;；:：\s、！？]|$))'
)

# 车载冰箱行业/GEO场景下常见的通用词，不是具体品牌名，抽取到要过滤掉
# 含关系一律过滤（而不仅是完全相等），避免"车载冰箱推荐以下""选择车载冰箱"
# 这类夹带通用词的整句被 BRAND_SUFFIX_PATTERN 误当成品牌名抽出来。
GENERIC_TERMS = {
    "车载冰箱", "冰箱", "压缩机", "品牌", "厂家", "产品", "市场", "行业",
    "消费者", "用户", "价格", "质量", "性能", "选购", "推荐",
    "老牌", "知名", "正规", "靠谱", "以下", "如下", "这些", "上述",
    "目前", "常见", "市面上", "众多", "部分", "某些",
    # 泛指/关联词和国家名：真实DeepSeek长回答里常以"国内品牌""关于XX品牌"
    # "选择品牌时""主流品牌""是全球品牌""源自XX的品牌""有些品牌"这类句式
    # 展开描述，这些词不是品牌名，作为 BRAND_SUFFIX_PATTERN 的兜底过滤。
    "关于", "这类", "那些", "选择", "主流", "国内", "国外", "有些",
    "全球", "源自", "各种", "不同", "多个", "一些", "其中", "包括",
    "中国", "意大利", "美国", "德国", "日本", "韩国", "法国", "英国",
    "也有", "还有", "并有", "又有", "更有", "没有",
}


def _clean_candidate(name: str) -> str:
    name = name.strip(" 　、,，。.;；:：()（）\"'“”‘’")
    # "等" 本身，或"等几个/等几家/等多个/等一些"这类"等+量词"收尾，
    # 都是枚举列表的收尾语气词，不属于品牌名本身。
    name = re.sub(r'等(?:等|[一二三四五六七八九十几]{1,3}[个家种些]?)?$', '', name)
    return name.strip()


def _find_valid_alias_matches(text: str, alias: str):
    """返回未被否定句式修饰的真实提及（正则 match 对象列表）。
    命中 NEGATED_MENTION_PATTERN（如"没有提到XX"）的那次出现不算真实提及，
    但同一品牌在文本别处的正常提及仍然会被计入。
    """
    negated_spans = [m.span() for m in NEGATED_MENTION_PATTERN.finditer(text)]
    flags = re.IGNORECASE if alias.isascii() else 0
    matches = []
    for m in re.finditer(re.escape(alias), text, flags):
        if any(ns <= m.start() and m.end() <= ne for ns, ne in negated_spans):
            continue
        matches.append(m)
    return matches


def _sentence_spans(text: str):
    """按句子级终止符切分文本，返回 [(start, end), ...]。"""
    spans = []
    start = 0
    for m in SENTENCE_SPLIT_PATTERN.finditer(text):
        end = m.start()
        if end > start:
            spans.append((start, end))
        start = m.end()
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def _sentence_containing(spans, pos: int):
    for s, e in spans:
        if s <= pos < e:
            return s, e
    return None


def _has_recommend_keyword(sentence: str) -> bool:
    """在同一句话范围内查找推荐类关键词，且要求关键词前面不是否定词。
    先去掉 Markdown 加粗符号 **，避免"**推荐**普能达"这类写法漏检。
    """
    cleaned = sentence.replace('**', '')
    for kw in RECOMMEND_KEYWORDS:
        for m in re.finditer(re.escape(kw), cleaned):
            prefix = cleaned[max(0, m.start() - 3): m.start()]
            if NEGATION_BEFORE_KEYWORD_PATTERN.search(prefix):
                continue
            return True
    return False


def parse_geo_answer(raw_answer: str, model: str) -> dict:
    text = raw_answer or ""

    # 1 & 2：品牌是否出现 + 出现次数（原始回答本身不做任何修改，只读）。
    # "没有提到XX" 这类否定句式命中的位置不计入真实提及。
    brand_alias_matched = []
    mention_count = 0
    brand_mention_matches = []  # [(alias, match), ...]，供推荐判定复用位置信息
    for alias in BRAND_ALIASES:
        matches = _find_valid_alias_matches(text, alias)
        if matches:
            brand_alias_matched.append(alias)
            mention_count += len(matches)
            brand_mention_matches.extend((alias, m) for m in matches)
    brand_mentioned = mention_count > 0

    # 识别列表结构，供排名判定 + 竞品抽取共用
    list_items = LIST_MARKER_PATTERN.findall(text)

    # 4：排名。只有识别到明确的列表/序数结构，且本方品牌落在其中才给出数值，否则 None
    rank = None
    if list_items:
        for idx, item in enumerate(list_items, start=1):
            if any(alias.lower() in item.lower() for alias in BRAND_ALIASES):
                rank = idx
                break
    if rank is None:
        for i, word in enumerate(ORDINAL_WORDS, start=1):
            # 逗号也作为窗口边界：中文"第一...，第二...，第三..."这种表述里，
            # 逗号就是分隔不同序数分句的标志，窗口越过逗号会把品牌名错误地
            # 归到相邻的另一个序数上（比如把"第二是普能达"误判成"第一"）。
            m = re.search(re.escape(word) + r'[^\n。；，]{0,15}', text)
            if m and any(alias.lower() in m.group(0).lower() for alias in BRAND_ALIASES):
                rank = i
                break

    # 5 & 6：竞品抽取 + 首次出现顺序
    competitor_order = []
    seen = set()

    def add_candidate(name: str):
        name = _clean_candidate(name)
        if not name or len(name) < 2 or len(name) > 12:
            return
        # 含"的"几乎必然说明抓到的是描述性短句/从句片段（如"售后完善的"），
        # 真实品牌名不会带这个虚词，直接过滤。
        if "的" in name:
            return
        # 只要候选词里夹带任一通用词就过滤（而非要求完全相等），
        # 拦掉"车载冰箱推荐以下""选择车载冰箱"这类整句被误抽的情况。
        if any(term in name for term in GENERIC_TERMS):
            return
        # 含关系而非完全相等：拦掉"普能达是一家车载冰箱生产厂家"这种本方品牌名
        # 紧跟描述文字、被 BRAND_SUFFIX_PATTERN 一起抓进候选词的情况——真实竞品名
        # 不可能包含本方品牌的字符串。这是深度诊断批量测试中新发现的问题，
        # 快速诊断走的是同一套 brand_parser，此前没被现有用例覆盖到。
        if any(alias.lower() in name.lower() for alias in BRAND_ALIASES):
            return
        key = name.lower()
        if key in seen:
            return
        seen.add(key)
        competitor_order.append(name)

    for item in list_items:
        cleaned = re.sub(r'^(推荐|品牌|厂家)[:：]?', '', item).strip()
        # 上限对齐 BRAND_SUFFIX_PATTERN / BRAND_LIST_PATTERN 的 {2,8}：
        # 列表项若不是"品牌名 —— 描述"而是整句描述性文字（无早期标点断句），
        # 12字符上限会把半句话截出来误当品牌名，8字符更接近真实品牌名长度。
        m = re.match(r'([A-Za-z一-龥]{2,8})', cleaned)
        if m:
            add_candidate(m.group(1))

    for m in BRAND_SUFFIX_PATTERN.finditer(text):
        add_candidate(m.group(1))

    for m in BRAND_LIST_PATTERN.finditer(text):
        for piece in re.split(r'、|和|及', m.group(1)):
            add_candidate(piece)

    competitors = competitor_order[:]  # 当前版本二者内容一致，均按首次出现顺序排列

    # 3：推荐状态。
    # 品牌落在识别到的排名列表中——出现在"推荐列表"里本身就是一种推荐信号；
    # 否则在品牌所在的整句话（而非固定字符窗口）内查找推荐类关键词，兼容关键词
    # 在品牌名前/后出现、中间夹杂描述文字、Markdown加粗、跨列表项等场景。
    # 关键词前若紧跟否定词（"不/没有/未推荐"等）不算数，保持保守判断——
    # 只有确实表达"推荐/建议/首选/值得考虑"语义时才为True，单纯提及不算。
    recommended = False
    if brand_mentioned:
        if rank is not None:
            recommended = True
        else:
            sentence_spans = _sentence_spans(text)
            for alias, m in brand_mention_matches:
                span = _sentence_containing(sentence_spans, m.start())
                if not span:
                    continue
                sentence = text[span[0]:span[1]]
                if _has_recommend_keyword(sentence):
                    recommended = True
                    break

    # 7：引用来源，只提取回答中真实出现的URL，不做任何猜测
    citations = list(dict.fromkeys(URL_PATTERN.findall(text)))  # 去重且保持首次出现顺序

    return {
        "brand_mentioned": brand_mentioned,
        "brand_alias_matched": brand_alias_matched,
        "mention_count": mention_count,
        "recommended": recommended,
        "rank": rank,
        "competitors": competitors,
        "competitor_order": competitor_order,
        "citations": citations,
        "citation_count": len(citations),
        "model": model,
        "tested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
