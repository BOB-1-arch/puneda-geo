"""
GEO 深度诊断 - SQLite 持久化层

只做最基础的存取，不引入ORM。用标准库 sqlite3，不新增依赖。
浏览器刷新后历史数据不能丢：数据落在磁盘文件里，只要服务进程不重装数据库
文件就一直在，天然满足这一点。

数据库路径可以通过环境变量 GEO_DB_PATH 覆盖，方便测试用临时文件隔离，
不污染开发时的真实数据库。
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

DB_PATH = os.environ.get("GEO_DB_PATH", "geo_diagnosis.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS diagnosis_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market TEXT NOT NULL,
    platform TEXT NOT NULL,
    mode TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    status TEXT NOT NULL,
    total_questions INTEGER NOT NULL,
    success_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS diagnosis_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    diagnosis_id INTEGER NOT NULL REFERENCES diagnosis_runs(id),
    question TEXT NOT NULL,
    query_intent TEXT,
    commercial_value TEXT,
    platform TEXT NOT NULL,
    raw_answer TEXT,
    brand_mentioned INTEGER,
    mention_count INTEGER,
    recommended INTEGER,
    rank INTEGER,
    competitors TEXT,
    citations TEXT,
    answer_fit TEXT,
    industry_knowledge_quality TEXT,
    gaps TEXT,
    diagnosis_summary TEXT,
    observations TEXT,
    inferences TEXT,
    actions TEXT,
    model TEXT,
    tested_at TEXT,
    status TEXT NOT NULL,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_items_diagnosis_id ON diagnosis_items(diagnosis_id);

CREATE TABLE IF NOT EXISTS content_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content_cluster TEXT NOT NULL,
    content_type TEXT NOT NULL,
    priority TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planning',

    target_query TEXT,
    target_queries TEXT,
    query_intent TEXT,
    commercial_value TEXT,

    source_diagnosis_id INTEGER REFERENCES diagnosis_runs(id),
    source_diagnosis_item_ids TEXT,

    geo_gaps TEXT,
    action_type TEXT,
    reason TEXT,

    suggested_title TEXT,
    alt_titles TEXT,
    content_angle TEXT,
    outline TEXT,
    key_points TEXT,
    facts_required TEXT,
    entity_facts TEXT,

    target_page_type TEXT,
    target_url TEXT,
    published_url TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    published_at TEXT,

    index_status TEXT NOT NULL DEFAULT 'unchecked',
    index_checked_at TEXT,
    index_notes TEXT,

    retest_status TEXT NOT NULL DEFAULT 'not_scheduled',
    retest_due_at TEXT,
    retest_completed_at TEXT,

    baseline_brand_mentioned INTEGER,
    baseline_recommended INTEGER,
    baseline_rank INTEGER,
    baseline_snapshot TEXT,

    retest_brand_mentioned INTEGER,
    retest_recommended INTEGER,
    retest_rank INTEGER,
    retest_snapshot TEXT,

    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_content_tasks_status ON content_tasks(status);
CREATE INDEX IF NOT EXISTS idx_content_tasks_priority ON content_tasks(priority);
CREATE INDEX IF NOT EXISTS idx_content_tasks_source_diagnosis_id ON content_tasks(source_diagnosis_id);
"""


def init_db(db_path=None):
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def _connect(db_path=None):
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def now():
    """公开版本，供 main.py 需要记时间戳（如发布时间/收录检查时间）时复用，
    不用各自再实现一遍格式。"""
    return _now()


def create_run(market, platform, mode, total_questions, db_path=None):
    with _connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO diagnosis_runs
               (market, platform, mode, created_at, started_at, status, total_questions,
                success_count, failed_count)
               VALUES (?, ?, ?, ?, ?, 'running', ?, 0, 0)""",
            (market, platform, mode, _now(), _now(), total_questions),
        )
        conn.commit()
        return cur.lastrowid


def _json_or_none(value):
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _json_load(value, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def add_item(run_id, item, db_path=None):
    """item 是一个 dict，success 情况下至少包含 question/query_intent/
    commercial_value/platform/status='success' 以及 parsed+diagnosis 展开的
    各字段；failed 情况下 status='failed' + error_message，其余字段可以缺省。
    保存后同步累加对应 run 的 success_count / failed_count。
    """
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT INTO diagnosis_items
               (diagnosis_id, question, query_intent, commercial_value, platform,
                raw_answer, brand_mentioned, mention_count, recommended, rank,
                competitors, citations, answer_fit, industry_knowledge_quality,
                gaps, diagnosis_summary, observations, inferences, actions,
                model, tested_at, status, error_message)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                item.get("question"),
                item.get("query_intent"),
                item.get("commercial_value"),
                item.get("platform"),
                item.get("raw_answer"),
                None if item.get("brand_mentioned") is None else int(bool(item.get("brand_mentioned"))),
                item.get("mention_count"),
                None if item.get("recommended") is None else int(bool(item.get("recommended"))),
                item.get("rank"),
                _json_or_none(item.get("competitors")),
                _json_or_none(item.get("citations")),
                item.get("answer_fit"),
                item.get("industry_knowledge_quality"),
                _json_or_none(item.get("gaps")),
                item.get("diagnosis_summary"),
                _json_or_none(item.get("observations")),
                _json_or_none(item.get("inferences")),
                _json_or_none(item.get("actions")),
                item.get("model"),
                item.get("tested_at"),
                item.get("status", "success"),
                item.get("error_message"),
            ),
        )
        if item.get("status") == "failed":
            conn.execute(
                "UPDATE diagnosis_runs SET failed_count = failed_count + 1 WHERE id = ?",
                (run_id,),
            )
        else:
            conn.execute(
                "UPDATE diagnosis_runs SET success_count = success_count + 1 WHERE id = ?",
                (run_id,),
            )
        conn.commit()


def complete_run(run_id, db_path=None):
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE diagnosis_runs SET status = 'completed', completed_at = ? WHERE id = ?",
            (_now(), run_id),
        )
        conn.commit()


def _row_to_run(row):
    return dict(row)


def _row_to_item(row):
    d = dict(row)
    d["brand_mentioned"] = None if d["brand_mentioned"] is None else bool(d["brand_mentioned"])
    d["recommended"] = None if d["recommended"] is None else bool(d["recommended"])
    d["competitors"] = _json_load(d["competitors"], [])
    d["citations"] = _json_load(d["citations"], [])
    d["gaps"] = _json_load(d["gaps"], [])
    d["observations"] = _json_load(d["observations"], [])
    d["inferences"] = _json_load(d["inferences"], [])
    d["actions"] = _json_load(d["actions"], [])
    return d


def get_run(run_id, db_path=None):
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM diagnosis_runs WHERE id = ?", (run_id,)).fetchone()
        return _row_to_run(row) if row else None


def get_run_items(run_id, db_path=None):
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM diagnosis_items WHERE diagnosis_id = ? ORDER BY id ASC",
            (run_id,),
        ).fetchall()
        return [_row_to_item(r) for r in rows]


def list_runs(market=None, platform=None, limit=50, db_path=None):
    query = "SELECT * FROM diagnosis_runs WHERE 1=1"
    params = []
    if market:
        query += " AND market = ?"
        params.append(market)
    if platform:
        query += " AND platform = ?"
        params.append(platform)
    query += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)
    with _connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
        return [_row_to_run(r) for r in rows]


def get_latest_successful_run(market=None, platform=None, db_path=None):
    query = "SELECT * FROM diagnosis_runs WHERE status = 'completed'"
    params = []
    if market:
        query += " AND market = ?"
        params.append(market)
    if platform:
        query += " AND platform = ?"
        params.append(platform)
    query += " ORDER BY completed_at DESC, id DESC LIMIT 1"
    with _connect(db_path) as conn:
        row = conn.execute(query, params).fetchone()
        return _row_to_run(row) if row else None


# ---------------------------------------------------------------------------
# GEO 内容矩阵 - content_tasks
# ---------------------------------------------------------------------------

_CONTENT_TASK_JSON_FIELDS = (
    "target_queries", "source_diagnosis_item_ids", "geo_gaps", "alt_titles",
    "outline", "key_points", "facts_required", "entity_facts",
    "baseline_snapshot", "retest_snapshot",
)

_CONTENT_TASK_BOOL_FIELDS = (
    "baseline_brand_mentioned", "baseline_recommended",
    "retest_brand_mentioned", "retest_recommended",
)

_CONTENT_TASK_COLUMNS = (
    "title", "content_cluster", "content_type", "priority", "status",
    "target_query", "target_queries", "query_intent", "commercial_value",
    "source_diagnosis_id", "source_diagnosis_item_ids",
    "geo_gaps", "action_type", "reason",
    "suggested_title", "alt_titles", "content_angle", "outline", "key_points",
    "facts_required", "entity_facts",
    "target_page_type", "target_url", "published_url",
    "created_at", "updated_at", "completed_at", "published_at",
    "index_status", "index_checked_at", "index_notes",
    "retest_status", "retest_due_at", "retest_completed_at",
    "baseline_brand_mentioned", "baseline_recommended", "baseline_rank", "baseline_snapshot",
    "retest_brand_mentioned", "retest_recommended", "retest_rank", "retest_snapshot",
    "notes",
)


def _content_task_row_to_dict(row):
    d = dict(row)
    for f in _CONTENT_TASK_JSON_FIELDS:
        d[f] = _json_load(d.get(f), [] if f != "outline" else [])
    for f in _CONTENT_TASK_BOOL_FIELDS:
        d[f] = None if d.get(f) is None else bool(d[f])
    return d


def _prepare_content_task_value(field, value):
    if field in _CONTENT_TASK_JSON_FIELDS:
        return _json_or_none(value if value is not None else [])
    if field in _CONTENT_TASK_BOOL_FIELDS:
        return None if value is None else int(bool(value))
    return value


def create_content_task(fields, db_path=None):
    """fields 是一个 dict，键为 _CONTENT_TASK_COLUMNS 的子集。
    title/content_cluster/content_type/priority 是必填项，其余缺省用 None/空列表。
    created_at/updated_at 自动填充，不需要调用方传。
    """
    now = _now()
    data = dict(fields)
    data["created_at"] = now
    data["updated_at"] = now
    data.setdefault("status", "planning")
    data.setdefault("index_status", "unchecked")
    data.setdefault("retest_status", "not_scheduled")

    columns = [c for c in _CONTENT_TASK_COLUMNS if c in data]
    values = [_prepare_content_task_value(c, data.get(c)) for c in columns]
    placeholders = ", ".join(["?"] * len(columns))
    with _connect(db_path) as conn:
        cur = conn.execute(
            f"INSERT INTO content_tasks ({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )
        conn.commit()
        return cur.lastrowid


def get_content_task(task_id, db_path=None):
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM content_tasks WHERE id = ?", (task_id,)).fetchone()
        return _content_task_row_to_dict(row) if row else None


def update_content_task(task_id, updates, db_path=None):
    """updates 是一个 dict，只更新传入的字段，updated_at 自动刷新。
    返回 True 表示确实更新到了一行，False 表示 task_id 不存在。
    """
    data = dict(updates)
    data.pop("id", None)
    data.pop("created_at", None)
    data["updated_at"] = _now()

    columns = [c for c in _CONTENT_TASK_COLUMNS + ("updated_at",) if c in data]
    if not columns:
        return get_content_task(task_id, db_path=db_path) is not None
    set_clause = ", ".join(f"{c} = ?" for c in columns)
    values = [_prepare_content_task_value(c, data.get(c)) for c in columns]
    with _connect(db_path) as conn:
        cur = conn.execute(
            f"UPDATE content_tasks SET {set_clause} WHERE id = ?",
            values + [task_id],
        )
        conn.commit()
        return cur.rowcount > 0


def delete_content_task(task_id, db_path=None):
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM content_tasks WHERE id = ?", (task_id,))
        conn.commit()
        return cur.rowcount > 0


def list_content_tasks(
    priority=None, status=None, content_cluster=None, query_intent=None,
    commercial_value=None, geo_gap=None, source_diagnosis_id=None,
    retest_status=None, search=None, limit=500, db_path=None,
):
    query = "SELECT * FROM content_tasks WHERE 1=1"
    params = []
    if priority:
        query += " AND priority = ?"
        params.append(priority)
    if status:
        query += " AND status = ?"
        params.append(status)
    if content_cluster:
        query += " AND content_cluster = ?"
        params.append(content_cluster)
    if query_intent:
        query += " AND query_intent = ?"
        params.append(query_intent)
    if commercial_value:
        query += " AND commercial_value = ?"
        params.append(commercial_value)
    if geo_gap:
        query += " AND geo_gaps LIKE ?"
        params.append(f'%"{geo_gap}"%')
    if source_diagnosis_id:
        query += " AND source_diagnosis_id = ?"
        params.append(source_diagnosis_id)
    if retest_status:
        query += " AND retest_status = ?"
        params.append(retest_status)
    if search:
        query += " AND (title LIKE ? OR target_query LIKE ?)"
        like = f"%{search}%"
        params.extend([like, like])
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with _connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
        return [_content_task_row_to_dict(r) for r in rows]


def append_query_to_task(task_id, query_text, diagnosis_item_id=None, db_path=None):
    """把一个新Query（及其来源诊断item）合并进一个已有的内容任务，而不是新建一条任务。
    用于"发现相似内容任务 -> 合并到现有任务"这个场景。
    """
    task = get_content_task(task_id, db_path=db_path)
    if not task:
        return False
    target_queries = list(task.get("target_queries") or [])
    if query_text and query_text not in target_queries:
        target_queries.append(query_text)
    item_ids = list(task.get("source_diagnosis_item_ids") or [])
    if diagnosis_item_id is not None and diagnosis_item_id not in item_ids:
        item_ids.append(diagnosis_item_id)
    return update_content_task(
        task_id,
        {"target_queries": target_queries, "source_diagnosis_item_ids": item_ids},
        db_path=db_path,
    )
