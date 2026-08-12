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


# 把 static/ 目录里的前端网页一并托管出去，浏览器访问 http://127.0.0.1:8000/ 即可打开。
# 这行必须放在所有 /api/... 路由定义之后。
app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
