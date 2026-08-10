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
"""

import re
from datetime import datetime

BRAND_ALIASES = ["普能达", "PUNEDA"]

RECOMMEND_KEYWORDS = [
    "推荐", "值得推荐", "值得考虑", "值得选择", "优选", "首选",
    "口碑较好", "口碑不错", "性价比高", "值得入手", "不错的选择", "值得关注",
]

# 编号/项目符号列表项： "1. xxx" "1、xxx" "①xxx" "-xxx" "*xxx" 等
LIST_MARKER_PATTERN = re.compile(
    r'(?:^|\n)\s*(?:[0-9]{1,2}[.、)．]|[①②③④⑤⑥⑦⑧⑨⑩]|[-•*])\s*([^\n]+)'
)

ORDINAL_WORDS = ["第一", "第二", "第三", "第四", "第五", "第六", "第七", "第八"]

URL_PATTERN = re.compile(r'https?://[^\s\)\]\>，。；、"\'》]+')

# "XX品牌" "XX车载冰箱" "XX厂家" 这种紧邻关键词的命名模式。
# 要求候选词前面是句子边界（开头/空白/标点），避免把一整段长句误当成一个"品牌名"截取出来。
BRAND_SUFFIX_PATTERN = re.compile(
    r'(?:(?<=^)|(?<=[，,。;；:：\s、\(（]))([A-Za-z\u4e00-\u9fa5]{2,8})(?:品牌|车载冰箱|厂家)'
)

# "品牌有A、B、C" "厂家推荐：A、B、C" 这种顿号并列列表。
# 只用顿号"、"作分隔符（不含逗号），因为中文里顿号才是同类项枚举的标准分隔符，
# 逗号/句号往往表示另起一个小句，用它们切分会把不相关的后半句也错误地并进候选列表。
BRAND_LIST_PATTERN = re.compile(
    r'(?:品牌|厂家)(?:有|包括|如|推荐)[:：]?\s*'
    r'((?:[A-Za-z\u4e00-\u9fa5]{2,8}、)+[A-Za-z\u4e00-\u9fa5]{2,8}(?=[，,。;；:：\s、！？]|$))'
)

# 车载冰箱行业/GEO场景下常见的通用词，不是具体品牌名，抽取到要过滤掉
# 含关系一律过滤（而不仅是完全相等），避免"车载冰箱推荐以下""选择车载冰箱"
# 这类夹带通用词的整句被 BRAND_SUFFIX_PATTERN 误当成品牌名抽出来。
GENERIC_TERMS = {
    "车载冰箱", "冰箱", "压缩机", "品牌", "厂家", "产品", "市场", "行业",
    "消费者", "用户", "价格", "质量", "性能", "选购", "推荐",
    "老牌", "知名", "正规", "靠谱", "以下", "如下", "这些", "上述",
}


def _find_alias_matches(text: str, alias: str):
    flags = re.IGNORECASE if alias.isascii() else 0
    return re.findall(re.escape(alias), text, flags)


def _clean_candidate(name: str) -> str:
    name = name.strip(" \u3000、,，。.;；:：()（）\"'“”‘’")
    name = re.sub(r'(等等|等)$', '', name)
    return name.strip()


def parse_geo_answer(raw_answer: str, model: str) -> dict:
    text = raw_answer or ""

    # 1 & 2：品牌是否出现 + 出现次数（原始回答本身不做任何修改，只读）
    brand_alias_matched = []
    mention_count = 0
    for alias in BRAND_ALIASES:
        matches = _find_alias_matches(text, alias)
        if matches:
            brand_alias_matched.append(alias)
            mention_count += len(matches)
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
            m = re.search(re.escape(word) + r'[^\n。；]{0,15}', text)
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
        if any(alias.lower() == name.lower() for alias in BRAND_ALIASES):
            return
        key = name.lower()
        if key in seen:
            return
        seen.add(key)
        competitor_order.append(name)

    for item in list_items:
        cleaned = re.sub(r'^(推荐|品牌|厂家)[:：]?', '', item).strip()
        # 上限对齐 BRAND_SUFFIX_PATTERN / BRAND_LIST_PATTERN 的 {2,8}：
        # 列表项若不是“品牌名 —— 描述”而是整句描述性文字（无早期标点断句），
        # 12字符上限会把半句话截出来误当品牌名，8字符更接近真实品牌名长度。
        m = re.match(r'([A-Za-z\u4e00-\u9fa5]{2,8})', cleaned)
        if m:
            add_candidate(m.group(1))

    for m in BRAND_SUFFIX_PATTERN.finditer(text):
        add_candidate(m.group(1))

    for m in BRAND_LIST_PATTERN.finditer(text):
        for piece in m.group(1).split('、'):
            add_candidate(piece)

    competitors = competitor_order[:]  # 当前版本二者内容一致，均按首次出现顺序排列

    # 3：推荐状态。品牌落在识别到的排名列表中，或紧邻"推荐类"关键词，才判定为True
    recommended = False
    if brand_mentioned:
        if rank is not None:
            recommended = True
        else:
            for alias in BRAND_ALIASES:
                flags = re.IGNORECASE if alias.isascii() else 0
                for m in re.finditer(re.escape(alias), text, flags):
                    window = text[max(0, m.start() - 15): m.end() + 15]
                    if any(kw in window for kw in RECOMMEND_KEYWORDS):
                        recommended = True
                        break
                if recommended:
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
