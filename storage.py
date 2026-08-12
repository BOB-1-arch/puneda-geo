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
