# -*- coding: utf-8 -*-
"""项目状态持久化模块（Supabase PostgreSQL）。

独立数据库模块，与 RAG / Reviewer / PPT / DOCX / LLM 主流程解耦。
数据库不可用时所有方法安全降级（返回 None / [] / 空字典），不会抛出异常导致应用崩溃。

连接信息从环境变量读取，二选一：
  1. DATABASE_URL（推荐，直接使用 Supabase 后台提供的连接串）
  2. DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASSWORD 分项配置

技术选型：SQLAlchemy 2.x + psycopg3（方言 postgresql+psycopg）。

使用方式：
  - 初始化表结构：init_db()
  - 检查连接：database_available()
  - 命令行自检：python -m project_memory
"""
from __future__ import annotations

import datetime as dt
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from dotenv import load_dotenv
from sqlalchemy import (
    Column,
    Date,
    DateTime,
    ForeignKey,
    String,
    Text,
    URL,
    create_engine,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

Base = declarative_base()

# ---------- 环境变量 ----------
SNAPSHOT_LIMIT = int(os.getenv("PM_SNAPSHOT_LIMIT", "5"))

# 任务/风险状态取值约定（与 SQL 默认值保持一致）
TASK_STATUS_TODO = "todo"
TASK_STATUS_IN_PROGRESS = "in_progress"
TASK_STATUS_BLOCKED = "blocked"
TASK_STATUS_DONE = "done"
RISK_STATUS_CLOSED = "closed"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _build_database_url() -> Optional[str]:
    """优先读取 DATABASE_URL，否则用 DB_HOST 等分项拼装。未配置返回 None。"""
    raw = _env("DATABASE_URL")
    if raw:
        # 兼容 postgres:// 与 postgresql://，统一到 psycopg3 方言
        url = raw.replace("postgresql://", "postgresql+psycopg://", 1)
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
        return url

    host = _env("DB_HOST")
    if not host:
        return None
    try:
        return URL.create(
            "postgresql+psycopg",
            username=_env("DB_USER") or None,
            password=_env("DB_PASSWORD") or None,
            host=host,
            port=int(_env("DB_PORT", "5432")),
            database=_env("DB_NAME") or None,
        ).render_as_string(hide_password=False)
    except Exception:
        return None


# ---------- 引擎与会话（惰性初始化） ----------
_engine: Optional[Engine] = None
_SessionLocal: Optional[sessionmaker] = None
_available_cache: Optional[bool] = None


def get_engine() -> Optional[Engine]:
    """惰性创建并缓存引擎；未配置或创建失败返回 None。"""
    global _engine
    if _engine is not None:
        return _engine
    url = _build_database_url()
    if not url:
        return None
    try:
        _engine = create_engine(
            url,
            pool_pre_ping=True,          # 取连接前先 ping，避免拿到失效连接
            pool_recycle=1800,           # 定期回收，规避 Supabase 连接空闲超时
            connect_args={"connect_timeout": 10},
        )
    except Exception:
        _engine = None
        return None
    return _engine


def _get_sessionmaker() -> Optional[sessionmaker]:
    global _SessionLocal
    if _SessionLocal is not None:
        return _SessionLocal
    engine = get_engine()
    if engine is None:
        return None
    _SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    return _SessionLocal


@contextmanager
def _session() -> Iterator[Optional[Session]]:
    """会话上下文：正常退出自动提交，异常回滚并重抛；数据库不可用时 yield None。"""
    sm = _get_sessionmaker()
    if sm is None:
        yield None
        return
    session = sm()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def database_available(force: bool = False) -> bool:
    """探测数据库是否可连接（结果缓存，force=True 重新探测并重建引擎）。"""
    global _available_cache, _engine
    if force:
        _available_cache = None
        _engine = None
    elif _available_cache is not None:
        return _available_cache

    engine = get_engine()
    if engine is None:
        _available_cache = False
        return False
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        _available_cache = True
    except Exception:
        _available_cache = False
    return _available_cache


def init_db() -> bool:
    """按模型创建缺失的表（幂等）。成功 True，未配置/失败 False。"""
    engine = get_engine()
    if engine is None:
        return False
    try:
        Base.metadata.create_all(engine)
        return True
    except Exception:
        return False


# ---------- ORM 模型 ----------
class Project(Base):
    __tablename__ = "projects"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    name = Column(String(255), nullable=False)
    description = Column(Text)
    status = Column(String(50), nullable=False, server_default=text("'active'"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class Task(Base):
    __tablename__ = "tasks"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title = Column(String(500), nullable=False)
    description = Column(Text)
    owner = Column(String(255))
    status = Column(String(50), nullable=False, server_default=text("'todo'"), index=True)
    priority = Column(String(50))
    dependency = Column(Text)
    due_date = Column(Date)
    source = Column(String(255))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class Decision(Base):
    __tablename__ = "decisions"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    content = Column(Text, nullable=False)
    decision_maker = Column(String(255))
    decision_date = Column(Date)
    status = Column(String(50), index=True)
    source = Column(String(255))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)


class Risk(Base):
    __tablename__ = "risks"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    content = Column(Text, nullable=False)
    severity = Column(String(50))
    status = Column(String(50), nullable=False, server_default=text("'open'"), index=True)
    owner = Column(String(255))
    impact = Column(Text)
    source = Column(String(255))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class Event(Base):
    __tablename__ = "events"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_date = Column(Date)
    content = Column(Text, nullable=False)
    source = Column(String(255))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)


# ---------- 工具函数 ----------
def _to_uuid(value) -> Optional[uuid.UUID]:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def _to_date(value):
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value.strip())
        except ValueError:
            return None
    return value


def _serialize(value):
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _row_to_dict(obj) -> dict:
    return {c.name: _serialize(getattr(obj, c.name)) for c in obj.__table__.columns}


# ---------- CRUD ----------
def create_project(name: str, description: Optional[str] = None, status: str = "active"):
    """创建项目，返回项目 id（字符串），失败返回 None。"""
    if not name:
        return None
    try:
        with _session() as session:
            if session is None:
                return None
            project = Project(name=name, description=description, status=status)
            session.add(project)
            session.flush()
            return str(project.id)
    except Exception:
        return None


def get_project(project_id):
    """按 id 查询项目，返回 dict，不存在或失败返回 None。"""
    pid = _to_uuid(project_id)
    if pid is None:
        return None
    try:
        with _session() as session:
            if session is None:
                return None
            project = session.get(Project, pid)
            return _row_to_dict(project) if project else None
    except Exception:
        return None


def list_projects() -> list:
    """返回全部项目（按创建时间倒序）。数据库不可用时返回空列表。"""
    try:
        with _session() as session:
            if session is None:
                return []
            rows = session.query(Project).order_by(Project.created_at.desc()).all()
            return [_row_to_dict(r) for r in rows]
    except Exception:
        return []


def add_task(
    project_id,
    title: str,
    description: Optional[str] = None,
    owner: Optional[str] = None,
    status: str = TASK_STATUS_TODO,
    priority: Optional[str] = None,
    dependency: Optional[str] = None,
    due_date=None,
    source: Optional[str] = None,
):
    """新增任务，返回任务 id（字符串），失败返回 None。"""
    pid = _to_uuid(project_id)
    if pid is None or not title:
        return None
    try:
        with _session() as session:
            if session is None:
                return None
            task = Task(
                project_id=pid,
                title=title,
                description=description,
                owner=owner,
                status=status or TASK_STATUS_TODO,
                priority=priority,
                dependency=dependency,
                due_date=_to_date(due_date),
                source=source,
            )
            session.add(task)
            session.flush()
            return str(task.id)
    except Exception:
        return None


_TASK_UPDATE_FIELDS = {
    "title", "description", "owner", "status", "priority",
    "dependency", "due_date", "source",
}


def update_task(task_id, **fields) -> bool:
    """更新任务字段（白名单），返回是否成功。"""
    tid = _to_uuid(task_id)
    if tid is None:
        return False
    updates = {k: v for k, v in fields.items() if k in _TASK_UPDATE_FIELDS and v is not None}
    if not updates:
        return False
    if "due_date" in updates:
        updates["due_date"] = _to_date(updates["due_date"])
    try:
        with _session() as session:
            if session is None:
                return False
            task = session.get(Task, tid)
            if task is None:
                return False
            for k, v in updates.items():
                setattr(task, k, v)
        return True
    except Exception:
        return False


def get_tasks(project_id, status: Optional[str] = None) -> list:
    """查询项目任务（可按状态过滤），返回 dict 列表。"""
    pid = _to_uuid(project_id)
    if pid is None:
        return []
    try:
        with _session() as session:
            if session is None:
                return []
            query = session.query(Task).filter(Task.project_id == pid)
            if status:
                query = query.filter(Task.status == status)
            return [_row_to_dict(t) for t in query.order_by(Task.created_at.desc()).all()]
    except Exception:
        return []


def add_decision(
    project_id,
    content: str,
    decision_maker: Optional[str] = None,
    decision_date=None,
    status: Optional[str] = None,
    source: Optional[str] = None,
):
    """新增决策，返回决策 id（字符串），失败返回 None。"""
    pid = _to_uuid(project_id)
    if pid is None or not content:
        return None
    try:
        with _session() as session:
            if session is None:
                return None
            decision = Decision(
                project_id=pid,
                content=content,
                decision_maker=decision_maker,
                decision_date=_to_date(decision_date),
                status=status,
                source=source,
            )
            session.add(decision)
            session.flush()
            return str(decision.id)
    except Exception:
        return None


def get_decisions(project_id, limit: Optional[int] = None) -> list:
    """查询项目决策（最新在前），返回 dict 列表。"""
    pid = _to_uuid(project_id)
    if pid is None:
        return []
    try:
        with _session() as session:
            if session is None:
                return []
            query = (
                session.query(Decision)
                .filter(Decision.project_id == pid)
                .order_by(Decision.decision_date.desc().nullslast(), Decision.created_at.desc())
            )
            if limit:
                query = query.limit(limit)
            return [_row_to_dict(d) for d in query.all()]
    except Exception:
        return []


def add_risk(
    project_id,
    content: str,
    severity: Optional[str] = None,
    status: str = "open",
    owner: Optional[str] = None,
    impact: Optional[str] = None,
    source: Optional[str] = None,
):
    """新增风险，返回风险 id（字符串），失败返回 None。"""
    pid = _to_uuid(project_id)
    if pid is None or not content:
        return None
    try:
        with _session() as session:
            if session is None:
                return None
            risk = Risk(
                project_id=pid,
                content=content,
                severity=severity,
                status=status or "open",
                owner=owner,
                impact=impact,
                source=source,
            )
            session.add(risk)
            session.flush()
            return str(risk.id)
    except Exception:
        return None


_RISK_UPDATE_FIELDS = {"content", "severity", "status", "owner", "impact", "source"}


def update_risk(risk_id, **fields) -> bool:
    """更新风险字段（白名单），返回是否成功。"""
    rid = _to_uuid(risk_id)
    if rid is None:
        return False
    updates = {k: v for k, v in fields.items() if k in _RISK_UPDATE_FIELDS and v is not None}
    if not updates:
        return False
    try:
        with _session() as session:
            if session is None:
                return False
            risk = session.get(Risk, rid)
            if risk is None:
                return False
            for k, v in updates.items():
                setattr(risk, k, v)
        return True
    except Exception:
        return False


def get_risks(project_id, status: Optional[str] = None) -> list:
    """查询项目风险（可按状态过滤），返回 dict 列表。"""
    pid = _to_uuid(project_id)
    if pid is None:
        return []
    try:
        with _session() as session:
            if session is None:
                return []
            query = session.query(Risk).filter(Risk.project_id == pid)
            if status:
                query = query.filter(Risk.status == status)
            return [_row_to_dict(r) for r in query.order_by(Risk.created_at.desc()).all()]
    except Exception:
        return []


def add_event(
    project_id,
    content: str,
    event_date=None,
    source: Optional[str] = None,
):
    """新增事件，返回事件 id（字符串），失败返回 None。"""
    pid = _to_uuid(project_id)
    if pid is None or not content:
        return None
    try:
        with _session() as session:
            if session is None:
                return None
            event = Event(
                project_id=pid,
                content=content,
                event_date=_to_date(event_date),
                source=source,
            )
            session.add(event)
            session.flush()
            return str(event.id)
    except Exception:
        return None


def get_events(project_id, limit: Optional[int] = None) -> list:
    """查询项目事件（最新在前），返回 dict 列表。"""
    pid = _to_uuid(project_id)
    if pid is None:
        return []
    try:
        with _session() as session:
            if session is None:
                return []
            query = (
                session.query(Event)
                .filter(Event.project_id == pid)
                .order_by(Event.event_date.desc().nullslast(), Event.created_at.desc())
            )
            if limit:
                query = query.limit(limit)
            return [_row_to_dict(e) for e in query.all()]
    except Exception:
        return []


def _empty_snapshot() -> dict:
    return {
        "project": None,
        "current_tasks": [],
        "completed_tasks": [],
        "blocked_tasks": [],
        "latest_decisions": [],
        "open_risks": [],
        "recent_events": [],
    }


def get_project_snapshot(project_id) -> dict:
    """返回项目整体状态快照：
    - current_tasks：进行中的任务（todo / in_progress）
    - completed_tasks：已完成任务（done）
    - blocked_tasks：阻塞任务（blocked）
    - latest_decisions：最新决策
    - open_risks：未关闭风险
    - recent_events：最近事件
    数据库不可用时返回空结构，不会抛异常。
    """
    pid = _to_uuid(project_id)
    if pid is None:
        return _empty_snapshot()
    try:
        with _session() as session:
            if session is None:
                return _empty_snapshot()

            project = session.get(Project, pid)

            current_tasks = (
                session.query(Task)
                .filter(
                    Task.project_id == pid,
                    Task.status.in_([TASK_STATUS_TODO, TASK_STATUS_IN_PROGRESS]),
                )
                .order_by(Task.created_at.desc())
                .all()
            )
            completed_tasks = (
                session.query(Task)
                .filter(Task.project_id == pid, Task.status == TASK_STATUS_DONE)
                .order_by(Task.created_at.desc())
                .all()
            )
            blocked_tasks = (
                session.query(Task)
                .filter(Task.project_id == pid, Task.status == TASK_STATUS_BLOCKED)
                .order_by(Task.created_at.desc())
                .all()
            )
            latest_decisions = (
                session.query(Decision)
                .filter(Decision.project_id == pid)
                .order_by(Decision.decision_date.desc().nullslast(), Decision.created_at.desc())
                .limit(SNAPSHOT_LIMIT)
                .all()
            )
            open_risks = (
                session.query(Risk)
                .filter(Risk.project_id == pid, Risk.status != RISK_STATUS_CLOSED)
                .order_by(Risk.created_at.desc())
                .all()
            )
            recent_events = (
                session.query(Event)
                .filter(Event.project_id == pid)
                .order_by(Event.event_date.desc().nullslast(), Event.created_at.desc())
                .limit(SNAPSHOT_LIMIT)
                .all()
            )

        return {
            "project": _row_to_dict(project) if project else None,
            "current_tasks": [_row_to_dict(t) for t in current_tasks],
            "completed_tasks": [_row_to_dict(t) for t in completed_tasks],
            "blocked_tasks": [_row_to_dict(t) for t in blocked_tasks],
            "latest_decisions": [_row_to_dict(d) for d in latest_decisions],
            "open_risks": [_row_to_dict(r) for r in open_risks],
            "recent_events": [_row_to_dict(e) for e in recent_events],
        }
    except Exception:
        return _empty_snapshot()


# ---------- 命令行自检 ----------
if __name__ == "__main__":
    url = _build_database_url()
    if not url:
        print("未配置数据库连接：请设置 DATABASE_URL，或 DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASSWORD")
    else:
        # 不打印完整连接串，避免泄露密码
        host = _env("DB_HOST")
        if not host and _env("DATABASE_URL"):
            print("已读取 DATABASE_URL（Supabase 连接串）")
        else:
            print(f"连接目标：{_env('DB_USER')}@{host}:{_env('DB_PORT', '5432')}/{_env('DB_NAME')}")

        ok = database_available()
        print("数据库连接：", "成功 ✅" if ok else "失败 ❌")
        if ok:
            print("初始化表结构：", "成功 ✅" if init_db() else "失败 ❌")
