# -*- coding: utf-8 -*-
"""Project Memory UI 模块：把项目状态抽取 / 确认写入 / 快照展示全部封装在此，
避免 app.py 继续膨胀。app.py 只需 4 个触点接入（见各 render_* 函数注释）。

设计约定（第一阶段）：
- 只支持创建和选择一个项目，不做复杂项目管理
- 抽取结果先展示给用户检查，确认后才写入 Supabase
- 数据库不可用 / 抽取失败时只提示错误，不影响聊天与 RAG
"""
from __future__ import annotations

from typing import Optional

import streamlit as st

import project_memory as pm
from fact_extractor import ExtractionError, extract_facts, save_facts

# ---------- 会话状态 ----------
# pm_project_id   当前项目 id（str）
# pm_doc_texts    {文档名: 完整文本} 本次会话上传时记住的原文
# pm_pending      待确认的抽取结果 {"facts": {...}, "source": 文档名, "project_id": ...}


def _doc_texts() -> dict:
    return st.session_state.setdefault("pm_doc_texts", {})


def remember_doc_text(name: str, text: str):
    """记住本次上传文档的完整文本（供项目状态抽取使用，避免从分块拼接）。"""
    if text and text.strip():
        _doc_texts()[name] = text


def get_doc_text(name: str, knowledge_base=None) -> Optional[str]:
    """取文档全文：优先本次会话记忆，其次从知识库分块按序拼接。"""
    text = _doc_texts().get(name)
    if text:
        return text
    if knowledge_base is not None:
        chunks = [e["text"] for e in knowledge_base.entries if e.get("doc") == name]
        if chunks:
            return "\n".join(chunks)
    return None


def current_project_id() -> Optional[str]:
    """当前选中的项目 id。"""
    return st.session_state.get("pm_project_id")


def _show_db_diagnose():
    """在数据库不可用时展示详细诊断面板，帮助用户在 Cloud 上快速定位问题。"""
    diag = pm.database_diagnose()
    st.markdown("**🔧 诊断面板**")
    rows = [
        ("os.environ.DATABASE_URL", "✅" if diag["env_DATABASE_URL"] else "❌"),
        ("os.environ.DB_HOST", "✅" if diag["env_DB_HOST"] else "❌"),
        ("st.secrets.DATABASE_URL", "✅" if diag["st_secrets_DATABASE_URL"] else "❌"),
        ("st.secrets.DB_HOST", "✅" if diag["st_secrets_DB_HOST"] else "❌"),
        ("构建连接 URL", "✅" if diag["url_built"] else "❌"),
        ("SQLAlchemy Engine", "✅" if diag["engine_ok"] else "❌"),
        ("SELECT 1 连通", "✅" if diag["connect_ok"] else "❌"),
    ]
    for name, status in rows:
        st.caption(f"{status} `{name}`")
    if diag.get("url_preview"):
        st.caption(f"URL 预览：`{diag['url_preview']}`")
    if diag.get("last_error"):
        st.error(f"连接错误：`{diag['last_error']}`")
    if diag.get("st_secrets_error"):
        st.caption(f"st.secrets 异常：`{diag['st_secrets_error']}`")

    with st.expander("📖 修复指引", expanded=False):
        st.markdown("""
        **在 Streamlit Cloud 上配置 Supabase 数据库**：
        1. 打开你的 App → `⋮` → **Settings** → **Secrets**
        2. 粘贴以下 TOML（密码已替换）：
        ```toml
        DATABASE_URL = "postgresql://postgres:<你的密码>@db.kbelbsnnxwawfrrwjczq.supabase.co:5432/postgres?sslmode=require"
        ```
        3. 保存后**必须 Reboot app**（不是 Rerun）才能让 Secrets 注入生效
        4. 如果用 Supabase Transaction Pooler，把 host 换成 `aws-0-xx.pooler.supabase.com`，端口 `6543`
        """)


# ---------- 侧边栏：项目记忆 ----------
def render_sidebar_section(knowledge_base, client=None, model_name: Optional[str] = None):
    """侧边栏「项目记忆」区：数据库状态 + 项目创建/选择 + 文档选择 + 提取按钮。

    app.py 在侧边栏（知识库 expander 之前）调用一次即可。
    """
    with st.expander("🗂️ 项目记忆（Supabase）", expanded=False):
        if not pm.database_available():
            st.caption("🗄️ 数据库未连接，项目记忆暂不可用（不影响聊天与知识库）。")
            # ---------- 诊断信息 ----------
            _show_db_diagnose()
            return

        # ---- 项目创建 / 选择（第一版：单项目） ----
        projects = pm.list_projects()
        if not projects:
            st.caption("还没有项目，先创建一个：")
        else:
            labels = [p["name"] for p in projects]
            cur = st.session_state.get("pm_project_id")
            idx = next(
                (i for i, p in enumerate(projects) if str(p["id"]) == cur), 0
            )
            sel = st.selectbox(
                "当前项目",
                range(len(projects)),
                format_func=lambda i: labels[i],
                index=idx,
                key="pm_project_sel",
            )
            st.session_state.pm_project_id = str(projects[sel]["id"])

        with st.popover("➕ 新建项目", use_container_width=True):
            new_name = st.text_input(
                "项目名称", key="pm_new_name", placeholder="例如：药监局申报助手"
            )
            new_desc = st.text_input(
                "项目描述（可选）", key="pm_new_desc", placeholder="一句话说明"
            )
            if st.button("创建", key="pm_create_btn", type="primary", use_container_width=True):
                if not new_name.strip():
                    st.error("请填写项目名称")
                else:
                    pid = pm.create_project(new_name.strip(), new_desc.strip() or None)
                    if pid:
                        st.session_state.pm_project_id = str(pid)
                        st.toast(f"✅ 项目「{new_name.strip()}」已创建")
                        st.rerun()
                    else:
                        st.error("项目创建失败（数据库写入异常）")

        pid = current_project_id()
        if pid is None:
            st.caption("请先创建或选择项目，再提取项目状态。")
            return

        st.divider()

        # ---- 文档选择 + 提取按钮 ----
        doc_names = sorted(set(list(_doc_texts().keys()) + (
            knowledge_base.doc_names() if knowledge_base is not None else []
        )))
        if not doc_names:
            st.caption("暂无可提取的文档：请先在「知识库」中上传项目资料。")
            return

        doc = st.selectbox("选择要提取的文档", doc_names, key="pm_doc_sel")
        if st.button(
            "🔍 提取项目状态",
            key="pm_extract_btn",
            type="primary",
            use_container_width=True,
            help="用当前大模型从文档中抽取 决策/任务/风险/事件，先预览再确认写入",
        ):
            text = get_doc_text(doc, knowledge_base)
            if not text or not text.strip():
                st.error(f"无法获取「{doc}」的文本内容")
                return
            with st.spinner(f"🤖 正在从「{doc}」中抽取项目状态（约 1~3 分钟）…"):
                try:
                    facts = extract_facts(text, client=client, model_name=model_name)
                except ExtractionError as e:
                    st.error(f"❌ 抽取失败：{e}")
                    return
            total = sum(len(facts[k]) for k in ("decisions", "tasks", "risks", "events"))
            if total == 0:
                st.warning("未从该文档中抽取到任何决策/任务/风险/事件。")
                return
            st.session_state.pm_pending = {
                "facts": facts,
                "source": doc,
                "project_id": pid,
            }
            st.toast(f"已抽取 {total} 条事实，请在主页面检查后确认写入")
            st.rerun()


# ---------- 主区域：抽取结果预览 + 确认写入 ----------
def _fmt(v) -> str:
    return str(v) if v else "—"


def _render_facts_review(facts: dict, source: str):
    """以可读格式展示抽取结果，供用户检查。"""
    st.markdown(f"来源文档：**{source}**")

    d, t, r, e = facts["decisions"], facts["tasks"], facts["risks"], facts["events"]
    st.markdown(
        f"共抽取 **{len(d)}** 条决策、**{len(t)}** 条任务、**{len(r)}** 条风险、**{len(e)}** 条事件。"
        " 请检查以下内容，确认无误后写入项目记忆。"
    )

    if d:
        with st.expander(f"🏛️ 决策（{len(d)}）", expanded=True):
            for i, x in enumerate(d, 1):
                st.markdown(
                    f"{i}. {x['content']}  \n"
                    f"<small>决策人：{_fmt(x.get('decision_maker'))} | 日期：{_fmt(x.get('decision_date'))}</small>",
                    unsafe_allow_html=True,
                )
    if t:
        with st.expander(f"✅ 任务（{len(t)}）", expanded=True):
            for i, x in enumerate(t, 1):
                st.markdown(
                    f"{i}. **{x['title']}**  \n"
                    f"<small>状态：{x.get('status') or 'todo'} | 优先级：{x.get('priority') or '—'} | "
                    f"负责人：{_fmt(x.get('owner'))} | 截止：{_fmt(x.get('due_date'))}</small>",
                    unsafe_allow_html=True,
                )
                if x.get("description"):
                    st.caption(x["description"])
    if r:
        with st.expander(f"⚠️ 风险（{len(r)}）", expanded=True):
            for i, x in enumerate(r, 1):
                st.markdown(
                    f"{i}. {x['content']}  \n"
                    f"<small>等级：{x.get('severity') or '—'} | 状态：{x.get('status') or 'open'} | "
                    f"负责人：{_fmt(x.get('owner'))}</small>",
                    unsafe_allow_html=True,
                )
                if x.get("impact"):
                    st.caption(f"影响：{x['impact']}")
    if e:
        with st.expander(f"📅 事件（{len(e)}）", expanded=True):
            for i, x in enumerate(e, 1):
                st.markdown(f"{i}. {_fmt(x.get('event_date'))} — {x['content']}")


def render_pending_review():
    """主区域：待确认的抽取结果 + 「确认写入项目记忆」按钮。

    app.py 在欢迎卡片之后调用一次即可；无待确认数据时不渲染任何内容。
    """
    pending = st.session_state.get("pm_pending")
    if not pending:
        return

    facts, source, pid = pending["facts"], pending["source"], pending["project_id"]
    project = pm.get_project(pid)
    pname = (project or {}).get("name", "未知项目")

    with st.container(border=True):
        st.subheader("🧐 项目状态抽取结果（待确认）")
        st.caption(f"目标项目：{pname}")
        _render_facts_review(facts, source)

        c1, c2 = st.columns([1, 1])
        with c1:
            if st.button("✅ 确认写入项目记忆", key="pm_confirm_btn", type="primary", use_container_width=True):
                with st.spinner("正在写入 Supabase…"):
                    try:
                        ids = save_facts(pid, facts, source=source)
                    except ExtractionError as exc:
                        st.error(f"❌ 写入失败：{exc}")
                        return
                n = sum(len(v) for v in ids.values())
                st.session_state.pop("pm_pending", None)
                st.session_state.pm_snapshot_dirty = True  # 写入后刷新快照
                st.toast(f"✅ 已写入 {n} 条项目记忆")
                st.rerun()
        with c2:
            if st.button("🗑️ 丢弃本次抽取", key="pm_discard_btn", use_container_width=True):
                st.session_state.pop("pm_pending", None)
                st.rerun()


# ---------- 主区域：项目状态快照 ----------
_STATUS_CN = {
    pm.TASK_STATUS_TODO: "待办",
    pm.TASK_STATUS_IN_PROGRESS: "进行中",
    pm.TASK_STATUS_BLOCKED: "阻塞",
    pm.TASK_STATUS_DONE: "已完成",
}


def _task_line(t: dict) -> str:
    return (
        f"- **{t['title']}**（{_STATUS_CN.get(t.get('status'), t.get('status'))}"
        f"{' | ' + t['owner'] if t.get('owner') else ''}"
        f"{' | 截止 ' + str(t['due_date']) if t.get('due_date') else ''}）"
    )


def _get_snapshot(pid: str) -> dict:
    """带会话级缓存的快照读取：项目切换或写入后自动刷新，避免每次交互都查库。"""
    cache = st.session_state.get("pm_snapshot")
    if (
        cache
        and cache.get("pid") == pid
        and not st.session_state.get("pm_snapshot_dirty")
    ):
        return cache["data"]
    snap = pm.get_project_snapshot(pid)
    st.session_state.pm_snapshot = {"pid": pid, "data": snap}
    st.session_state.pop("pm_snapshot_dirty", None)
    return snap


def render_project_status():
    """主区域：「项目状态」快照区（当前/阻塞/已完成任务、最新决策、未关闭风险、最近事件）。

    app.py 在欢迎卡片之后调用一次即可；数据库不可用或未选项目时不渲染。
    """
    pid = current_project_id()
    if pid is None:
        return
    if not pm.database_available():
        return

    try:
        snap = _get_snapshot(pid)
    except Exception as e:
        st.caption(f"📋 项目状态读取失败：{e}")
        return

    project = snap.get("project")
    if not project:
        return

    cur_t, blk_t, done_t = snap["current_tasks"], snap["blocked_tasks"], snap["completed_tasks"]
    dec, risks, events = snap["latest_decisions"], snap["open_risks"], snap["recent_events"]
    total = len(cur_t) + len(blk_t) + len(done_t) + len(dec) + len(risks) + len(events)

    with st.expander(
        f"📋 项目状态：{project['name']}"
        f"（任务 {len(cur_t) + len(blk_t) + len(done_t)} · 决策 {len(dec)} · 风险 {len(risks)} · 事件 {len(events)}）",
        expanded=total > 0,
    ):
        if st.button("🔄 刷新", key="pm_refresh_btn", type="tertiary"):
            st.session_state.pm_snapshot_dirty = True
            st.rerun()
        if total == 0:
            st.caption("暂无项目记忆数据。上传会议纪要/周报到知识库后，在侧边栏「项目记忆」中提取。")
            return

        left, right = st.columns(2)
        with left:
            st.markdown(f"**🚧 当前任务（{len(cur_t)}）**")
            st.markdown("\n".join(_task_line(t) for t in cur_t) or "—")
            st.markdown(f"**⛔ 阻塞任务（{len(blk_t)}）**")
            st.markdown("\n".join(_task_line(t) for t in blk_t) or "—")
            st.markdown(f"**✅ 已完成任务（{len(done_t)}）**")
            st.markdown("\n".join(_task_line(t) for t in done_t) or "—")
        with right:
            st.markdown(f"**🏛️ 最新决策（{len(dec)}）**")
            st.markdown(
                "\n".join(
                    f"- {d['content']}（{_fmt(d.get('decision_maker'))}，{_fmt(d.get('decision_date'))}）"
                    for d in dec
                )
                or "—"
            )
            st.markdown(f"**⚠️ 未关闭风险（{len(risks)}）**")
            st.markdown(
                "\n".join(
                    f"- {'🔴' if r.get('severity') == 'high' else '🟡' if r.get('severity') == 'medium' else '🔵'}"
                    f" {r['content']}（{_fmt(r.get('owner'))}）"
                    for r in risks
                )
                or "—"
            )
            st.markdown(f"**📅 最近事件（{len(events)}）**")
            st.markdown(
                "\n".join(f"- {_fmt(e.get('event_date'))} {e['content']}" for e in events)
                or "—"
            )
