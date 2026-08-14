"""
GEO 智能体后端 - MVP
当前阶段：只真实接入 DeepSeek 官方 Chat Completions API。
后续按 DeepSeek -> 豆包 -> 通义千问 -> Kimi -> 其他平台 的顺序逐个平台扩展。

安全原则：
- API Key 只从服务器端环境变量读取，绝不出现在前端代码、请求体或响应里。
- 前端只发送“问题文本”，不发送、不保存任何密钥。
"""

import os
import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from brand_parser import parse_geo_answer
from diagnosis_analyzer import diagnose as run_geo_diagnosis
from question_bank import select_questions
import storage
from aggregate import build_full_report, compute_run_stats
from geo_tutorial import build_tutorial_report
import content_matrix as cm

try:
    from dotenv import load_dotenv
    load_dotenv()  # 本地开发时从 .env 读取，生产环境请用真实环境变量注入
except ImportError:
    pass

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# deepseek-chat / deepseek-reasoner 已于 2026-07-24 15:59 UTC 弃用停用。
# 二者原本分别路由到 deepseek-v4-flash 的 non-thinking / thinking 模式，
# 官方要求显式改用 deepseek-v4-flash 或 deepseek-v4-pro。
# 默认模型可通过环境变量覆盖，方便后续整体切换到 v4-pro，无需改代码。
DEEPSEEK_DEFAULT_MODEL = os.environ.get("DEEPSEEK_DEFAULT_MODEL", "deepseek-v4-flash")
VALID_DEEPSEEK_MODELS = {"deepseek-v4-flash", "deepseek-v4-pro"}

storage.init_db()

app = FastAPI(title="GEO 智能体后端", version="0.1.0")

# 开发阶段先放开跨域，接入真实前端域名后请收紧 allow_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AskRequest(BaseModel):
    question: str
    model: str = DEEPSEEK_DEFAULT_MODEL  # deepseek-v4-flash（默认）或 deepseek-v4-pro
    thinking: bool = False    # V4系列默认开启思维链，这里默认关闭以对齐原deepseek-chat的非思考行为、控制延迟和成本


class AskResponse(BaseModel):
    platform: str
    model: str
    raw_answer: str                 # 模型返回的原始文本回答
    reasoning_content: str | None = None  # 开启thinking时的思维链内容，未开启则为None
    raw_response: dict              # DeepSeek API 返回的完整原始JSON，便于溯源和后续解析引用来源


@app.get("/api/platforms/deepseek/status")
def deepseek_status():
    """
    仅返回“服务器是否已配置Key”，不返回Key本身，也不做一次消耗额度的真实调用。
    """
    return {
        "platform": "DeepSeek",
        "configured": bool(DEEPSEEK_API_KEY),
        "default_model": DEEPSEEK_DEFAULT_MODEL,
        "available_models": sorted(VALID_DEEPSEEK_MODELS),
    }


@app.post("/api/ask/deepseek", response_model=AskResponse)
def ask_deepseek(req: AskRequest):
    if not req.question or not req.question.strip():
        raise HTTPException(status_code=400, detail="问题不能为空")

    if not DEEPSEEK_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="DEEPSEEK_API_KEY 未在服务器环境变量中配置，请参考 README 完成配置",
        )

    try:
        DEEPSEEK_API_KEY.encode("ascii")
    except UnicodeEncodeError:
        raise HTTPException(
            status_code=500,
            detail=(
                "DEEPSEEK_API_KEY 里混入了非ASCII字符（常见原因：手机端复制粘贴时被输入法"
                "自动转成了全角字符，肉眼看起来一样但实际编码不同）。请去DeepSeek开放平台"
                "重新复制一份Key，在服务器上用 `sudo nano .env` 直接编辑替换后，"
                "执行 `sudo systemctl restart puneda-geo` 重启服务。"
            ),
        )

    if req.model not in VALID_DEEPSEEK_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的model：{req.model}，仅支持 {sorted(VALID_DEEPSEEK_MODELS)}"
            f"（deepseek-chat / deepseek-reasoner 已于2026-07-24弃用）",
        )

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": req.model,
        "messages": [{"role": "user", "content": req.question}],
        "stream": False,
        "thinking": {"type": "enabled" if req.thinking else "disabled"},
    }

    try:
        resp = requests.post(
            f"{DEEPSEEK_BASE_URL}/chat/completions",
            json=payload,
            headers=headers,
            timeout=60,
        )
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"请求 DeepSeek API 失败：{e}")
    except UnicodeEncodeError as e:
        # 兜底：理论上上面对Key的ASCII校验已经能提前拦住最常见的情况，这里防止
        # 其他未预料到的字段（如请求头相关配置）里混入非ASCII字符时，用户看到的
        # 还是一堆看不懂的Python报错（对应500），而不是明确的错误提示。
        raise HTTPException(status_code=500, detail=f"请求头包含无法识别的字符：{e}")

    if resp.status_code != 200:
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"DeepSeek API 返回错误：{resp.text}",
        )

    data = resp.json()
    try:
        message = data["choices"][0]["message"]
        content = message["content"]
    except (KeyError, IndexError):
        raise HTTPException(status_code=502, detail="DeepSeek API 返回结构异常，无法解析回答")

    return AskResponse(
        platform="DeepSeek",
        model=req.model,
        raw_answer=content,
        reasoning_content=message.get("reasoning_content"),
        raw_response=data,
    )


class ParseRequest(BaseModel):
    raw_answer: str
    model: str = ""


@app.post("/api/parse/geo")
def parse_geo(req: ParseRequest):
    """
    GEO回答结构化解析 - 第一版。
    只做确定性规则解析（字符串/正则/列表结构识别），不调用任何AI去猜排名或品牌。
    完全不修改、不依赖 raw_answer 的原始内容，只读取。
    """
    return parse_geo_answer(req.raw_answer, req.model)


class DiagnoseRequest(BaseModel):
    question: str
    raw_answer: str
    model: str = ""


@app.post("/api/diagnose/geo")
def diagnose_geo(req: DiagnoseRequest):
    """
    GEO诊断分析：在 parse_geo 的结构化解析结果之上，进一步给出
    Query Intent 分类 / 回答匹配度 / 行业认知质量 / GEO Gap / 诊断结论 / 改善建议。
    深度诊断（20题批量）未来接入时也应复用同一条 brand_parser -> diagnosis_analyzer 链路，
    不另起一套判断逻辑。
    """
    parsed = parse_geo_answer(req.raw_answer, req.model)
    diagnosis = run_geo_diagnosis(req.question, req.raw_answer, parsed)
    return {"parsed": parsed, "diagnosis": diagnosis}


# ---------------------------------------------------------------------------
# 深度诊断（20题真实批量品牌体检）
#
# 批量本身不直接调用DeepSeek：前端逐题复用已有的 /api/ask/deepseek +
# /api/diagnose/geo（和快速诊断完全同一条真实链路），后端这里只负责
# 出题（question_bank）+ 落库（storage）+ 聚合统计（aggregate）。
# 单题失败不影响其它题，由前端捕获错误后调用 /item 接口记录
# status=failed + error_message，run 整体照常推进到 complete。
# ---------------------------------------------------------------------------

class DeepStartRequest(BaseModel):
    market: str = "cn"
    platform: str = "DeepSeek"
    intents: list[str] | None = None
    count: int = 20


@app.post("/api/diagnose/deep/start")
def start_deep_diagnosis(req: DeepStartRequest):
    if req.platform != "DeepSeek":
        raise HTTPException(status_code=400, detail="本轮深度诊断仅支持 DeepSeek 平台")
    questions = select_questions(req.intents, req.count)
    run_id = storage.create_run(req.market, req.platform, "quick_20", len(questions))
    return {"run_id": run_id, "questions": questions}


class DeepItemIn(BaseModel):
    question: str
    query_intent: str | None = None
    commercial_value: str | None = None
    platform: str = "DeepSeek"
    status: str  # "success" | "failed"
    raw_answer: str | None = None
    model: str | None = None
    tested_at: str | None = None
    error_message: str | None = None
    brand_mentioned: bool | None = None
    mention_count: int | None = None
    recommended: bool | None = None
    rank: int | None = None
    competitors: list[dict] | None = None  # brand_parser 的结构化竞品 {name,aliases,confidence,evidence}
    citations: list[str] | None = None
    answer_fit: str | None = None
    industry_knowledge_quality: str | None = None
    gaps: list[dict] | None = None
    diagnosis_summary: str | None = None
    observations: list[str] | None = None
    inferences: list[str] | None = None
    actions: list[dict] | None = None


@app.post("/api/diagnose/deep/{run_id}/item")
def save_deep_diagnosis_item(run_id: int, item: DeepItemIn):
    run = storage.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="诊断任务不存在")
    storage.add_item(run_id, item.model_dump())
    return {"ok": True}


@app.post("/api/diagnose/deep/{run_id}/complete")
def complete_deep_diagnosis(run_id: int):
    run = storage.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="诊断任务不存在")
    storage.complete_run(run_id)
    items = storage.get_run_items(run_id)
    report = build_full_report(items)
    return {"run": storage.get_run(run_id), "report": report, "items": items}


@app.get("/api/diagnose/deep/runs")
def list_deep_diagnosis_runs(market: str | None = None, platform: str | None = None, limit: int = 50):
    runs = storage.list_runs(market=market, platform=platform, limit=limit)
    result = []
    for run in runs:
        items = storage.get_run_items(run["id"])
        result.append({**run, "stats": compute_run_stats(items)})
    return {"runs": result}


@app.get("/api/diagnose/deep/latest")
def get_latest_deep_diagnosis(market: str | None = None, platform: str | None = None):
    run = storage.get_latest_successful_run(market=market, platform=platform)
    if not run:
        return {"run": None, "report": None, "items": []}
    items = storage.get_run_items(run["id"])
    return {"run": run, "report": build_full_report(items), "items": items}


@app.get("/api/diagnose/deep/runs/{run_id}")
def get_deep_diagnosis_run(run_id: int):
    run = storage.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="诊断任务不存在")
    items = storage.get_run_items(run_id)
    return {"run": run, "report": build_full_report(items), "items": items}


@app.get("/api/tutorial/geo")
def get_geo_tutorial(market: str | None = None, platform: str | None = None):
    """GEO教程：基础知识始终返回；如果存在最近一次已完成的深度诊断，
    额外按诊断结果里实际命中的Gap频次排序返回针对性教程。
    """
    run = storage.get_latest_successful_run(market=market, platform=platform)
    if not run:
        return {"run": None, "tutorial": build_tutorial_report(None)}
    items = storage.get_run_items(run["id"])
    return {"run": run, "tutorial": build_tutorial_report(items)}


# ---------------------------------------------------------------------------
# GEO 内容矩阵
#
# 诊断 -> 发现未覆盖Query -> 加入内容矩阵 -> 聚合相似Query -> P1/P2/P3内容任务
# -> 标题/大纲 -> 人工管理状态 -> 发布/收录记录 -> 设置复测 -> 重新诊断
# -> baseline vs retest 这条完整链路。
#
# 复测同样不在后端直接调用DeepSeek：前端复用已有的 /api/ask/deepseek +
# /api/diagnose/geo 逐个Query重新跑一遍（和深度诊断同一条真实链路），
# 后端只负责把结果落库、和baseline做对比。
# ---------------------------------------------------------------------------

_VALID_CONTENT_CLUSTERS = {c for c, _ in cm.CONTENT_CLUSTERS}
_VALID_CONTENT_TYPES = {c for c, _ in cm.CONTENT_TYPES}


def _validate_enum(value, valid_set, field_name):
    if value is not None and value not in valid_set:
        raise HTTPException(status_code=400, detail=f"{field_name} 不合法：{value}，可选值：{sorted(valid_set)}")


class EntityFactIn(BaseModel):
    name: str
    content: str = ""
    source: str = ""
    verified: bool = False


class ContentTaskIn(BaseModel):
    title: str
    content_cluster: str
    content_type: str
    priority: str = "P3"
    status: str = "planning"

    target_query: str | None = None
    target_queries: list[str] | None = None
    query_intent: str | None = None
    commercial_value: str | None = None

    source_diagnosis_id: int | None = None
    source_diagnosis_item_ids: list[int] | None = None

    geo_gaps: list[str] | None = None
    action_type: str | None = None
    reason: str | None = None

    suggested_title: str | None = None
    alt_titles: list[str] | None = None
    content_angle: str | None = None
    outline: list[dict] | None = None
    key_points: list[str] | None = None
    facts_required: list[str] | None = None
    entity_facts: list[EntityFactIn] | None = None

    target_page_type: str | None = None

    baseline_brand_mentioned: bool | None = None
    baseline_recommended: bool | None = None
    baseline_rank: int | None = None
    baseline_snapshot: dict | None = None

    notes: str | None = None

    # 当命中"发现相似内容任务"时，前端可以传这个字段，改成"合并到现有任务"
    # 而不是新建一条任务。
    merge_into_task_id: int | None = None


class ContentTaskUpdate(BaseModel):
    title: str | None = None
    content_cluster: str | None = None
    content_type: str | None = None
    priority: str | None = None
    status: str | None = None
    target_query: str | None = None
    target_queries: list[str] | None = None
    suggested_title: str | None = None
    alt_titles: list[str] | None = None
    content_angle: str | None = None
    outline: list[dict] | None = None
    key_points: list[str] | None = None
    facts_required: list[str] | None = None
    entity_facts: list[EntityFactIn] | None = None
    target_page_type: str | None = None
    target_url: str | None = None
    published_url: str | None = None
    published_at: str | None = None
    completed_at: str | None = None
    index_status: str | None = None
    index_checked_at: str | None = None
    index_notes: str | None = None
    retest_status: str | None = None
    retest_due_at: str | None = None
    notes: str | None = None


def _content_task_dict_for_create(payload: ContentTaskIn) -> dict:
    d = payload.model_dump(exclude={"merge_into_task_id"}, exclude_none=True)
    if payload.entity_facts is not None:
        d["entity_facts"] = [f.model_dump() for f in payload.entity_facts]
    if not d.get("target_queries") and d.get("target_query"):
        d["target_queries"] = [d["target_query"]]
    if not d.get("target_query") and d.get("target_queries"):
        d["target_query"] = d["target_queries"][0]
    return d


@app.get("/api/content/meta")
def get_content_meta():
    """内容矩阵用到的固定枚举，前端筛选/表单下拉框统一从这里取，
    避免前后端各自维护一份、后续改动漏改。
    """
    return {
        "content_clusters": [{"code": c, "label": l} for c, l in cm.CONTENT_CLUSTERS],
        "content_types": [{"code": c, "label": l} for c, l in cm.CONTENT_TYPES],
        "statuses": [{"code": s, "label": cm.STATUS_LABELS_CN[s]} for s in cm.STATUS_VALUES],
        "priorities": cm.PRIORITY_VALUES,
        "index_statuses": [{"code": s, "label": cm.INDEX_STATUS_LABELS_CN[s]} for s in cm.INDEX_STATUS_VALUES],
        "retest_statuses": [{"code": s, "label": cm.RETEST_STATUS_LABELS_CN[s]} for s in cm.RETEST_STATUS_VALUES],
    }


@app.get("/api/content/dashboard")
def get_content_dashboard():
    tasks = storage.list_content_tasks(limit=100000)
    return cm.compute_content_dashboard(tasks)


@app.get("/api/content/tasks")
def list_content_tasks(
    priority: str | None = None,
    status: str | None = None,
    content_cluster: str | None = None,
    query_intent: str | None = None,
    commercial_value: str | None = None,
    geo_gap: str | None = None,
    source_diagnosis_id: int | None = None,
    retest_status: str | None = None,
    search: str | None = None,
):
    tasks = storage.list_content_tasks(
        priority=priority, status=status, content_cluster=content_cluster,
        query_intent=query_intent, commercial_value=commercial_value,
        geo_gap=geo_gap, source_diagnosis_id=source_diagnosis_id,
        retest_status=retest_status, search=search,
    )
    return {"tasks": tasks}


@app.post("/api/content/tasks")
def create_content_task_endpoint(payload: ContentTaskIn):
    _validate_enum(payload.content_cluster, _VALID_CONTENT_CLUSTERS, "content_cluster")
    _validate_enum(payload.content_type, _VALID_CONTENT_TYPES, "content_type")
    _validate_enum(payload.priority, set(cm.PRIORITY_VALUES), "priority")
    _validate_enum(payload.status, set(cm.STATUS_VALUES), "status")

    if payload.merge_into_task_id is not None:
        existing = storage.get_content_task(payload.merge_into_task_id)
        if not existing:
            raise HTTPException(status_code=404, detail="要合并进去的内容任务不存在")
        query_text = payload.target_query or (payload.target_queries[0] if payload.target_queries else None)
        item_id = payload.source_diagnosis_item_ids[0] if payload.source_diagnosis_item_ids else None
        storage.append_query_to_task(payload.merge_into_task_id, query_text, item_id)
        return {"task": storage.get_content_task(payload.merge_into_task_id), "merged": True}

    task_id = storage.create_content_task(_content_task_dict_for_create(payload))
    return {"task": storage.get_content_task(task_id), "merged": False}


@app.get("/api/content/tasks/{task_id}")
def get_content_task_endpoint(task_id: int):
    task = storage.get_content_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="内容任务不存在")
    return {"task": task}


@app.put("/api/content/tasks/{task_id}")
def update_content_task_endpoint(task_id: int, payload: ContentTaskUpdate):
    existing = storage.get_content_task(task_id)
    if not existing:
        raise HTTPException(status_code=404, detail="内容任务不存在")

    _validate_enum(payload.content_cluster, _VALID_CONTENT_CLUSTERS, "content_cluster")
    _validate_enum(payload.content_type, _VALID_CONTENT_TYPES, "content_type")
    _validate_enum(payload.priority, set(cm.PRIORITY_VALUES), "priority")
    _validate_enum(payload.status, set(cm.STATUS_VALUES), "status")
    _validate_enum(payload.index_status, set(cm.INDEX_STATUS_VALUES), "index_status")
    _validate_enum(payload.retest_status, set(cm.RETEST_STATUS_VALUES), "retest_status")

    updates = payload.model_dump(exclude_none=True)
    if payload.entity_facts is not None:
        updates["entity_facts"] = [f.model_dump() for f in payload.entity_facts]
    # 标记"已发布"时如果没传发布时间，自动记一下，避免用户忘填。
    if updates.get("status") == "published" and not updates.get("published_at"):
        updates["published_at"] = storage.now()
    if updates.get("status") == "completed" and not updates.get("completed_at"):
        updates["completed_at"] = storage.now()
    if updates.get("index_status") == "indexed" and not updates.get("index_checked_at"):
        updates["index_checked_at"] = storage.now()

    storage.update_content_task(task_id, updates)
    return {"task": storage.get_content_task(task_id)}


@app.delete("/api/content/tasks/{task_id}")
def delete_content_task_endpoint(task_id: int):
    """只删内容任务本身，绝不触碰 diagnosis_runs/diagnosis_items —— 两张表之间
    只是用 source_diagnosis_id/source_diagnosis_item_ids 记了个引用关系，
    没有任何级联删除。"""
    existing = storage.get_content_task(task_id)
    if not existing:
        raise HTTPException(status_code=404, detail="内容任务不存在")
    storage.delete_content_task(task_id)
    return {"ok": True}


class AdHocDiagnosisItemIn(BaseModel):
    """快速诊断不落库到 diagnosis_items 表，没有 diagnosis_id/diagnosis_item_id
    可以引用——这里允许直接把一次快速诊断的 parsed+diagnosis 结果内联传进来，
    复用同一套 build_task_suggestion 逻辑，不需要另起一套判断规则。
    """
    question: str
    query_intent: str | None = None
    commercial_value: str | None = None
    brand_mentioned: bool | None = None
    recommended: bool | None = None
    rank: int | None = None
    competitors: list[dict] | None = None
    citations: list[str] | None = None
    gaps: list[dict] | None = None
    actions: list[dict] | None = None


class SuggestFromDiagnosisRequest(BaseModel):
    diagnosis_id: int | None = None
    diagnosis_item_id: int | None = None
    item: AdHocDiagnosisItemIn | None = None


@app.post("/api/content/tasks/suggest-from-diagnosis")
def suggest_from_diagnosis(req: SuggestFromDiagnosisRequest):
    """"加入内容矩阵"预览接口：不落库，只返回建议内容 + 是否命中相似的已有任务，
    供前端弹预览窗口，用户确认后再调用 POST /api/content/tasks（带上
    source_diagnosis_id/source_diagnosis_item_ids）或者带 merge_into_task_id 合并。

    支持两种输入：
    1. diagnosis_id + diagnosis_item_id —— 来自深度诊断，已经落库的问题。
    2. item —— 来自快速诊断的临时结果（快速诊断本身不落库），直接内联传结构化数据。
    """
    if req.item is not None:
        item = req.item.model_dump()
        item["id"] = None
        run_id = None
    elif req.diagnosis_id is not None and req.diagnosis_item_id is not None:
        items = storage.get_run_items(req.diagnosis_id)
        item = next((it for it in items if it["id"] == req.diagnosis_item_id), None)
        if not item:
            raise HTTPException(status_code=404, detail="诊断问题不存在")
        run_id = req.diagnosis_id
    else:
        raise HTTPException(status_code=400, detail="必须提供 diagnosis_id+diagnosis_item_id，或者 item")

    suggestion = cm.build_task_suggestion(item, run_id=run_id)
    existing_tasks = storage.list_content_tasks(limit=100000)
    similar = cm.find_similar_tasks(
        suggestion["target_query"], suggestion["content_cluster"],
        suggestion["query_intent"], existing_tasks,
    )
    return {"suggestion": suggestion, "similar_tasks": similar}


class BatchSuggestRequest(BaseModel):
    diagnosis_id: int


@app.post("/api/content/tasks/batch-suggest-from-diagnosis")
def batch_suggest_from_diagnosis(req: BatchSuggestRequest):
    """"生成内容矩阵建议"：把一次深度诊断里相似的Query聚合成少量建议
    （不是20题生成20篇），同样不落库，供前端勾选后调用 batch-from-diagnosis。
    """
    run = storage.get_run(req.diagnosis_id)
    if not run:
        raise HTTPException(status_code=404, detail="诊断任务不存在")
    items = storage.get_run_items(req.diagnosis_id)
    suggestions = cm.cluster_diagnosis_items_into_suggestions(items, run_id=req.diagnosis_id)
    return {"suggestions": suggestions}


class BatchCreateRequest(BaseModel):
    suggestions: list[dict]


@app.post("/api/content/tasks/batch-from-diagnosis")
def batch_create_from_diagnosis(req: BatchCreateRequest):
    """用户在 batch-suggest 预览里勾选之后，批量真正创建内容任务。"""
    created = []
    for s in req.suggestions:
        fields = {k: v for k, v in s.items() if k != "covered_query_count"}
        _validate_enum(fields.get("content_cluster"), _VALID_CONTENT_CLUSTERS, "content_cluster")
        _validate_enum(fields.get("content_type"), _VALID_CONTENT_TYPES, "content_type")
        task_id = storage.create_content_task(fields)
        created.append(storage.get_content_task(task_id))
    return {"tasks": created, "created_count": len(created)}


class RetestResultIn(BaseModel):
    query: str
    brand_mentioned: bool | None = None
    recommended: bool | None = None
    rank: int | None = None
    competitors: list[dict] | None = None
    citations: list[str] | None = None


class RetestRequest(BaseModel):
    results: list[RetestResultIn]


@app.post("/api/content/tasks/{task_id}/retest")
def save_content_task_retest(task_id: int, req: RetestRequest):
    """复测结果落库：真正的DeepSeek调用 + brand_parser + diagnosis_analyzer
    由前端逐个Query复用现有 /api/ask/deepseek + /api/diagnose/geo 完成，
    这个接口只负责把汇总结果存下来、和baseline做对比。
    """
    task = storage.get_content_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="内容任务不存在")
    if not req.results:
        raise HTTPException(status_code=400, detail="复测结果不能为空")

    retest_snapshot = {
        "per_query": [r.model_dump() for r in req.results],
    }
    first = req.results[0]
    storage.update_content_task(task_id, {
        "retest_snapshot": retest_snapshot,
        "retest_brand_mentioned": first.brand_mentioned,
        "retest_recommended": first.recommended,
        "retest_rank": first.rank,
        "retest_completed_at": storage.now(),
        "retest_status": "completed",
        "status": "retested",
    })
    updated = storage.get_content_task(task_id)
    comparison = cm.compare_baseline_retest(updated.get("baseline_snapshot"), retest_snapshot)
    return {"task": updated, "comparison": comparison}


@app.get("/api/content/tasks/{task_id}/comparison")
def get_content_task_comparison(task_id: int):
    task = storage.get_content_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="内容任务不存在")
    comparison = cm.compare_baseline_retest(task.get("baseline_snapshot"), task.get("retest_snapshot"))
    return {"comparison": comparison}


# 把 static/ 目录里的前端网页一并托管出去，浏览器访问 http://127.0.0.1:8000/ 即可打开。
# 这行必须放在所有 /api/... 路由定义之后。
app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
