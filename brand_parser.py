"""
GEO 回答结构化解析模块

只用确定性规则（字符串匹配 / 正则 / 简单结构识别 / 维护的品牌词典），
不调用任何AI模型去"猜"排名或品牌。拿不准的字段一律返回 None / 空值，
由调用方（前端）显示为"无法判断"，绝不为了让结果看起来丰富而编造。

竞品识别原则：Precision First，高精度优先。
宁可漏掉真实竞品，也绝不能把普通词、形容词、短语、国家地区、产品技术
词汇识别成品牌。只有满足下列任一高置信度结构，且通过强制排除词库校验
的候选词，才会进入 competitors：
1. Markdown/纯文本里的"中文品牌名（英文名）"括号结构
2. 命中人工维护的 KNOWN_BRANDS 品牌词典（不要求任何特定文本结构）
3. "品牌：X" / "品牌有A、B、C" / "推荐品牌包括A、B、C" 这类明确的品牌
   枚举/提示句式（仍然要通过排除词库校验，结构本身不是充分条件）
没有任何高置信度候选时，competitors 就是空列表，不做兜底猜测。

已知局限（如实说明，不夸大准确率）：
- KNOWN_BRANDS 是人工维护的品牌词典，覆盖有限，新品牌需要手动补充才能
  被识别；不在词典里、又没有中英文括号结构或明确"品牌："提示的竞品会被
  漏掉——这是"宁可漏检也不能误判"这个设计取舍的直接代价。
- 排名判定只在能识别出"编号列表 / 中文序数词"结构、且本方品牌落在其中时才给出，
  没有这种结构就一律返回 None，不做任何基于常识的推测。
- 推荐语义判定以"句子"为单位（句子边界：。！？；;和换行），在同一句内查找推荐类关键词，
  不再依赖固定字符窗口。如果一句话里同时议论了多个品牌（例如"A是首选，B仅供参考"），
  规则解析无法区分关键词具体指向谁。
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

# 编号/项目符号列表项： "1. xxx" "1、xxx" "①xxx" "-xxx" "*xxx" 等。
# 仅用于"排名"判定（本方品牌落在第几个列表项里），不再用于竞品抽取——
# 详见下方竞品识别部分的说明。
LIST_MARKER_PATTERN = re.compile(
    r'(?:^|\n)\s*(?:[0-9]{1,2}[.、)．]|[①②③④⑤⑥⑦⑧⑨⑩]|[-•*])\s*([^\n]+)'
)

ORDINAL_WORDS = ["第一", "第二", "第三", "第四", "第五", "第六", "第七", "第八"]

URL_PATTERN = re.compile(r'https?://[^\s\)\]\>，。；、"\'》]+')


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


# ---------------------------------------------------------------------------
# 竞品识别 - Precision First
# ---------------------------------------------------------------------------

# 人工维护的已知品牌词典。命中词典的候选词直接视为高置信度品牌，不要求任何
# 特定文本结构；需要持续补充维护。普能达/PUNEDA 是本方品牌，不放进这里
# （避免被误判成"竞品"），本方品牌的识别用 BRAND_ALIASES 单独处理。
KNOWN_BRANDS = [
    {"name": "英得尔", "aliases": ["Indel B", "IndelB"]},
    {"name": "冰虎", "aliases": ["Alpicool"]},
    {"name": "科敏", "aliases": ["KEMIN"]},
    {"name": "美固", "aliases": ["MOBICOOL"]},
    {"name": "百事泰", "aliases": ["BESTTEN"]},
    {"name": "纽曼", "aliases": ["Newsmy"]},
    {"name": "ICECO", "aliases": []},
    {"name": "Dometic", "aliases": []},
    {"name": "Engel", "aliases": []},
    {"name": "ARB", "aliases": []},
]

# 强制排除词库：命中即拒绝，不管候选词是从哪种结构里抽出来的。
# 分类维护，方便后续继续补充；实际过滤时统一按"是否为子串"处理。
FORBIDDEN_WORDS = {
    # 普通功能词/连接词/代词
    "挑选", "选择", "推荐", "这个", "那个", "如果", "因为", "所以", "但是",
    "只看", "不能只看", "决定", "涉足", "适合", "这类", "那些", "有些",
    "也有", "还有", "并有", "又有", "更有", "没有", "关于", "其中", "包括",
    "各种", "不同", "多个", "一些", "众多", "部分", "某些", "目前", "常见",
    "市面上", "以下", "如下", "这些", "上述", "老牌", "知名", "正规",
    "靠谱", "主流", "全球", "源自",
    # 品牌描述词
    "国产", "进口", "国内", "国外", "国内高端", "国产头部", "国际品牌",
    "主流品牌", "高端品牌", "性价比品牌",
    # 产品技术词
    "压缩机", "半导体", "制冷", "制热", "电压保护", "APP", "12V", "24V",
    "双温区", "单温区", "车载冰箱", "冰箱",
    # 国家地区
    "中国", "美国", "德国", "意大利", "欧洲", "日本", "韩国", "法国", "英国",
    # 商业描述词
    "厂家", "制造商", "供应商", "OEM", "ODM", "经销商", "品牌", "产品",
    "市场", "售后", "优势", "行业", "消费者", "用户", "价格", "质量",
    "性能", "选购",
}

# "中文品牌名（英文名）"括号结构，允许两侧带 Markdown 加粗符号 **。
# 例如 "**英得尔（Indel B）**" "冰虎（Alpicool）" "美固(MOBICOOL)"。
# 中文名前面要求有边界（句首/空白/标点/顿号/连接词"和、及、与"），否则
# {2,8}贪婪匹配会把"包括""和"这类前面的虚词也吞进候选品牌名里，
# 比如把"推荐品牌包括英得尔（Indel B）"错误截成"荐品牌包括英得尔"。
CN_EN_BRAND_PATTERN = re.compile(
    r'(?:(?<=^)|(?<=[\s，,。;；:：、\(（\*])|(?<=和)|(?<=及)|(?<=与))'
    r'\*{0,2}([一-龥]{2,8})\*{0,2}[（(]([A-Za-z][A-Za-z0-9 .\-]{1,24})[）)]\*{0,2}'
)

# "品牌有A、B、C" "厂家推荐：A、B和C" "推荐以下品牌：A、B、C" 这种并列列表。
# 触发词支持"有/包括/如/推荐"这几个动词，也支持"品牌/厂家"后面直接跟冒号。
# 分隔符支持顿号"、"及中文连接词"和""及"。
BRAND_LIST_PATTERN = re.compile(
    r'(?:品牌|厂家)(?:(?:有|包括|如|推荐)[:：]?|[:：])\s*'
    r'((?:[A-Za-z一-龥]{2,8}(?:、|和|及))+[A-Za-z一-龥]{2,8}(?=[，,。;；:：\s、！？]|$))'
)

# "品牌：冰虎" 这种单个品牌的明确提示句式（没有并列结构，BRAND_LIST_PATTERN
# 覆盖不到）。
BRAND_HINT_PATTERN = re.compile(
    r'品牌[:：]\s*([A-Za-z一-龥]{2,8})(?=[，,。;；\s]|$)'
)


def _is_own_brand(name: str) -> bool:
    return any(alias.lower() in name.lower() for alias in BRAND_ALIASES)


def is_valid_brand_candidate(candidate: str, evidence: str = "") -> tuple:
    """判断一个候选词是否可能是真实品牌名。返回 (是否有效, 原因说明)。
    只做确定性规则判断：命中任意一条排除条件就判定无效，不做任何语义推测。
    """
    name = _clean_candidate(candidate)
    if not name:
        return False, "空字符串"
    if len(name) < 2:
        return False, "长度过短，不像品牌名"
    if len(name) > 8:
        return False, "长度过长，更像描述性短语而非品牌名"
    if "的" in name:
        return False, "含虚词'的'，像描述性短语/从句片段"
    if _is_own_brand(name):
        return False, "包含本方品牌名，不能算竞品"
    if any(term in name for term in FORBIDDEN_WORDS):
        return False, "命中强制排除词库（普通词/描述词/产品技术/国家地区/商业用语）"
    if not re.search(r'[A-Za-z一-龥]', name):
        return False, "不含有效的中英文字符"
    return True, "通过校验"


def _match_known_brand(name: str, aliases):
    """尝试把候选词（含已发现的别名）归一化到 KNOWN_BRANDS 里的标准条目。"""
    candidates = [name] + list(aliases)
    for brand in KNOWN_BRANDS:
        brand_names = [brand["name"]] + brand["aliases"]
        for c in candidates:
            for bn in brand_names:
                if c.lower() == bn.lower():
                    return brand
    return None


def _scan_known_brands(text: str):
    """在全文里直接扫描 KNOWN_BRANDS 词典命中的品牌，不要求任何特定文本结构。"""
    found = []
    for brand in KNOWN_BRANDS:
        for name_variant in [brand["name"]] + brand["aliases"]:
            flags = re.IGNORECASE if name_variant.isascii() else 0
            m = re.search(re.escape(name_variant), text, flags)
            if m:
                found.append((brand["name"], list(brand["aliases"]), m.group(0)))
                break  # 同一品牌命中一个别名就够了
    return found


def extract_competitors(text: str) -> list:
    """Precision First 竞品识别：只接受高置信度结构 + 通过排除词库校验的候选，
    每条都带 evidence（原文片段），按首次发现顺序返回结构化列表：
    [{"name": str, "aliases": [str], "confidence": "high", "evidence": str}, ...]
    没有任何高置信度候选时返回空列表，不做兜底猜测。
    """
    competitors = []
    seen_keys = {}  # normalized_key -> competitors 里对应条目的下标

    def register(name: str, aliases, evidence: str):
        name = _clean_candidate(name)
        ok, _reason = is_valid_brand_candidate(name, evidence)
        if not ok:
            return
        canonical = _match_known_brand(name, aliases)
        if canonical:
            final_name = canonical["name"]
            final_aliases = list(canonical["aliases"])
        else:
            final_name = name
            final_aliases = [a for a in aliases if a]

        key = final_name.lower()
        if key in seen_keys:
            existing = competitors[seen_keys[key]]
            for a in final_aliases:
                if a not in existing["aliases"]:
                    existing["aliases"].append(a)
            # 换成更具体的原文片段作为evidence（比如后来发现了完整的
            # "中文（英文）"结构，比词典命中时只截到裸中文名更有说服力）。
            if len(evidence.strip()) > len(existing["evidence"]):
                existing["evidence"] = evidence.strip()
            return
        seen_keys[key] = len(competitors)
        competitors.append({
            "name": final_name,
            "aliases": final_aliases,
            "confidence": "high",
            "evidence": evidence.strip(),
        })

    # 来源1：已知品牌词典，全文直接扫描，不依赖任何文本结构。
    for name, aliases, evidence in _scan_known_brands(text):
        register(name, aliases, evidence)

    # 来源2："中文品牌名（英文名）"括号结构，中英文配对本身就是强证据。
    for m in CN_EN_BRAND_PATTERN.finditer(text):
        register(m.group(1), [m.group(2).strip()], m.group(0))

    # 来源3：明确的"品牌有/包括/推荐/：A、B、C"并列列表。
    for m in BRAND_LIST_PATTERN.finditer(text):
        for piece in re.split(r'、|和|及', m.group(1)):
            register(piece, [], piece)

    # 来源4："品牌：X"单个品牌的明确提示句式。
    for m in BRAND_HINT_PATTERN.finditer(text):
        register(m.group(1), [], m.group(0))

    return competitors


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

    # 识别列表结构，仅供排名判定使用
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

    # 5 & 6：竞品识别（Precision First，只保留高置信度、带evidence的候选）
    competitors = extract_competitors(text)

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
        "citations": citations,
        "citation_count": len(citations),
        "model": model,
        "tested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
