# -*- coding: utf-8 -*-
"""项目事实抽取器：从会议纪要/周报文本中用 LLM 抽取 decisions / tasks / risks / events，并写入 Supabase。

Project Memory MVP 闭环：
    会议纪要/周报文本 → LLM 严格 JSON 输出 → 规范化校验 → 写入 project_memory（Supabase）

说明：
- 仅复用 .env 中现有的 OpenAI 兼容接口配置（OPENAI_*，或 EMBEDDING_*/QWEN_MODEL 兜底），不新增配置项
- 不依赖 Streamlit，不触碰 kb.py / app.py
- 数据库不可用或写入失败时抛出带清晰信息的 ExtractionError，不静默降级
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from openai import OpenAI

import project_memory as pm

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

# ---------- 常量 ----------
VALID_TASK_STATUS = {"todo", "in_progress", "blocked", "done"}
VALID_RISK_STATUS = {"open", "closed"}
VALID_PRIORITY = {"high", "medium", "low"}
VALID_SEVERITY = {"high", "medium", "low"}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

SYSTEM_PROMPT = """你是项目管理助手，负责从会议纪要/周报文本中抽取结构化项目事实。
只输出一个合法 JSON 对象，不要输出任何解释、Markdown 代码块标记或其他文字。

JSON 结构：
{
  "decisions": [{"content": "决策内容", "decision_maker": "决策人", "decision_date": "YYYY-MM-DD"}],
  "tasks": [{"title": "任务标题", "description": "任务描述", "owner": "负责人", "status": "todo|in_progress|blocked|done", "priority": "high|medium|low", "dependency": "依赖的其他任务", "due_date": "YYYY-MM-DD"}],
  "risks": [{"content": "风险内容", "severity": "high|medium|low", "status": "open|closed", "owner": "负责人", "impact": "影响说明"}],
  "events": [{"event_date": "YYYY-MM-DD", "content": "事件内容"}]
}

规则：
1. 只抽取文本中明确提到的事实，不臆测、不编造。
2. decisions：已做出的决定/结论；文本未提及的字段填 null。
3. tasks：待办、进行中或被阻塞的工作项；状态不明确时默认 "todo"。
4. risks：风险项；已解决的风险 status 填 "closed"，未解决填 "open"。
5. events：已发生且有明确日期的事件；日期不明确的事件不要输出。
6. 所有日期使用 YYYY-MM-DD 格式，无法确定时填 null。
7. 四个数组必须都存在，没有对应内容时给空数组 []。"""


class ExtractionError(RuntimeError):
    """LLM 抽取或数据库写入失败时的统一异常。"""


# ---------- LLM ----------
def get_llm_config():
    """复用现有配置：优先 OPENAI_*，其次 EMBEDDING_*/QWEN_MODEL。返回 (api_key, base_url, model)。"""
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if api_key:
        return (
            api_key,
            os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip(),
            os.getenv("MODEL_NAME", "gpt-4o-mini").strip(),
        )
    api_key = os.getenv("EMBEDDING_API_KEY", "").strip()
    if api_key:
        return (
            api_key,
            os.getenv(
                "EMBEDDING_BASE_URL",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ).strip(),
            os.getenv("QWEN_MODEL", "qwen3.8-max").strip(),
        )
    raise ExtractionError("未配置 LLM：请在 .env 中设置 OPENAI_API_KEY 或 EMBEDDING_API_KEY")


def _extract_json(text: str) -> dict:
    """从模型输出中解析 JSON：剥离代码块标记，截取首个 {...} 片段。"""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ExtractionError(f"LLM 输出中未找到 JSON 对象：{text[:200]}")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"LLM 输出的 JSON 无法解析：{exc}") from exc


def _clean_str(value) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _clean_date(value) -> Optional[dt.date]:
    s = _clean_str(value)
    if not s or not _DATE_RE.match(s):
        return None
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        return None


def extract_facts(minutes_text: str, client=None, model_name: Optional[str] = None) -> dict:
    """调用 LLM 从会议纪要/周报文本中抽取结构化事实，返回规范化后的 dict。

    返回结构：{"decisions": [...], "tasks": [...], "risks": [...], "events": [...]}
    可选传入 client / model_name 复用调用方已创建的 OpenAI 客户端（如 Streamlit 侧选中的模型），
    未传时按 .env 配置自行创建。
    """
    if not minutes_text or not minutes_text.strip():
        raise ExtractionError("输入文本为空，无法抽取")

    if client is not None and model_name:
        model = model_name
    else:
        api_key, base_url, model = get_llm_config()
        client = OpenAI(api_key=api_key, base_url=base_url)
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": minutes_text},
            ],
            temperature=0.1,
        )
    except Exception as exc:
        raise ExtractionError(f"LLM 调用失败（{model}）：{exc}") from exc

    raw = resp.choices[0].message.content or ""
    data = _extract_json(raw)

    # ---------- 规范化 ----------
    decisions, tasks, risks, events = [], [], [], []
    for item in data.get("decisions") or []:
        content = _clean_str(item.get("content"))
        if not content:
            continue
        decisions.append(
            {
                "content": content,
                "decision_maker": _clean_str(item.get("decision_maker")),
                "decision_date": _clean_date(item.get("decision_date")),
            }
        )
    for item in data.get("tasks") or []:
        title = _clean_str(item.get("title"))
        if not title:
            continue
        status = _clean_str(item.get("status"))
        tasks.append(
            {
                "title": title,
                "description": _clean_str(item.get("description")),
                "owner": _clean_str(item.get("owner")),
                "status": status if status in VALID_TASK_STATUS else pm.TASK_STATUS_TODO,
                "priority": _clean_str(item.get("priority"))
                if _clean_str(item.get("priority")) in VALID_PRIORITY
                else None,
                "dependency": _clean_str(item.get("dependency")),
                "due_date": _clean_date(item.get("due_date")),
            }
        )
    for item in data.get("risks") or []:
        content = _clean_str(item.get("content"))
        if not content:
            continue
        status = _clean_str(item.get("status"))
        risks.append(
            {
                "content": content,
                "severity": _clean_str(item.get("severity"))
                if _clean_str(item.get("severity")) in VALID_SEVERITY
                else None,
                "status": status if status in VALID_RISK_STATUS else "open",
                "owner": _clean_str(item.get("owner")),
                "impact": _clean_str(item.get("impact")),
            }
        )
    for item in data.get("events") or []:
        content = _clean_str(item.get("content"))
        event_date = _clean_date(item.get("event_date"))
        if not content or not event_date:
            continue
        events.append({"event_date": event_date, "content": content})

    return {"decisions": decisions, "tasks": tasks, "risks": risks, "events": events}


# ---------- 写入数据库 ----------
def save_facts(project_id, facts: dict, source: str = "会议纪要") -> dict:
    """把抽取结果写入 Supabase，返回各类写入的 id 列表。

    数据库不可用或任一写入失败时抛出 ExtractionError。
    """
    pid = pm._to_uuid(project_id)
    if pid is None:
        raise ExtractionError(f"project_id 非法：{project_id}")
    if pm.get_project(pid) is None:
        raise ExtractionError("数据库不可用或项目不存在，无法写入（请检查 .env 中 DB_* / DATABASE_URL 配置）")

    result = {"decisions": [], "tasks": [], "risks": [], "events": []}

    for d in facts.get("decisions", []):
        rid = pm.add_decision(
            pid,
            d["content"],
            decision_maker=d.get("decision_maker"),
            decision_date=d.get("decision_date"),
            status="confirmed",
            source=source,
        )
        if rid is None:
            raise ExtractionError(f"决策写入失败：{d['content'][:50]}（数据库异常）")
        result["decisions"].append(rid)

    for t in facts.get("tasks", []):
        tid = pm.add_task(
            pid,
            t["title"],
            description=t.get("description"),
            owner=t.get("owner"),
            status=t.get("status", pm.TASK_STATUS_TODO),
            priority=t.get("priority"),
            dependency=t.get("dependency"),
            due_date=t.get("due_date"),
            source=source,
        )
        if tid is None:
            raise ExtractionError(f"任务写入失败：{t['title'][:50]}（数据库异常）")
        result["tasks"].append(tid)

    for r in facts.get("risks", []):
        rid = pm.add_risk(
            pid,
            r["content"],
            severity=r.get("severity"),
            status=r.get("status", "open"),
            owner=r.get("owner"),
            impact=r.get("impact"),
            source=source,
        )
        if rid is None:
            raise ExtractionError(f"风险写入失败：{r['content'][:50]}（数据库异常）")
        result["risks"].append(rid)

    for e in facts.get("events", []):
        eid = pm.add_event(
            pid,
            e["content"],
            event_date=e.get("event_date"),
            source=source,
        )
        if eid is None:
            raise ExtractionError(f"事件写入失败：{e['content'][:50]}（数据库异常）")
        result["events"].append(eid)

    return result


def extract_and_save(project_id, minutes_text: str, source: str = "会议纪要") -> tuple[dict, dict]:
    """一步完成：LLM 抽取 → 写入数据库。返回 (抽取结果, 写入的 id 集合)。"""
    facts = extract_facts(minutes_text)
    ids = save_facts(project_id, facts, source=source)
    return facts, ids
