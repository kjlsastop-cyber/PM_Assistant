# -*- coding: utf-8 -*-
"""助手工作台（本地 Web 界面版，支持多助手切换 / 大模型选择 / 对话框附件 / 历史话题 / 各助手独立知识库 RAG）

启动：python -m streamlit run app.py --server.headless true --browser.gatherUsageStats false
"""
import base64
import json
import os
import time
import uuid
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv
from openai import OpenAI

import kb

BASE_DIR = Path(__file__).parent
PROMPTS_DIR = BASE_DIR / "prompts"
STYLES_DIR = BASE_DIR / "styles"
PENGUIN_IMG = BASE_DIR / "assets" / "penguin.jpg"

st.set_page_config(page_title="助手工作台", page_icon="🐧", layout="wide")

# ---------- 初始化 ----------
load_dotenv(BASE_DIR / ".env")

UPLOAD_TYPES = ["txt", "md", "pdf", "docx", "pptx", "xlsx"]
MAX_UPLOAD_CHARS = 8000  # 本次附件注入上下文的最大字符数
MAX_HISTORY_ROUNDS = int(os.getenv("MAX_HISTORY_ROUNDS", "10"))  # 历史对话最大轮数
MAX_HISTORY_CHARS = int(os.getenv("MAX_HISTORY_CHARS", "8000"))  # 历史对话最大字符数
HISTORY_FILE = BASE_DIR / "history_topics.json"  # 历史话题本地持久化
REVIEW_ENABLED = os.getenv("REVIEW_ENABLED", "true").strip().lower() == "true"
REVIEW_MODEL = os.getenv("REVIEWER_MODEL", "").strip()  # 留空则复用当前对话模型
REVIEW_TEMP = float(os.getenv("REVIEW_TEMPERATURE", "0.3"))  # 审查温度，低温度确保审查稳定


def _model_options():
    """可选大模型：DeepSeek 使用 OPENAI_* 配置；通义千问复用 EMBEDDING_* 密钥与地址。"""
    options = {}
    ds_key = os.getenv("OPENAI_API_KEY", "").strip()
    if ds_key:
        ds_model = os.getenv("MODEL_NAME", "gpt-4o-mini").strip()
        options[f"DeepSeek（{ds_model}）"] = (
            ds_key,
            os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip(),
            ds_model,
        )
    qw_key = os.getenv("EMBEDDING_API_KEY", "").strip()
    if qw_key:
        qw_model = os.getenv("QWEN_MODEL", "qwen3.8-max").strip()
        options[f"通义千问（{qw_model}）"] = (
            qw_key,
            os.getenv(
                "EMBEDDING_BASE_URL",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ).strip(),
            qw_model,
        )
    return options


@st.cache_resource
def get_client(api_key: str, base_url: str):
    return OpenAI(api_key=api_key, base_url=base_url)


@st.cache_resource
def _get_kb(kb_id: str) -> kb.KnowledgeBase:
    """缓存 KnowledgeBase 实例，避免每次交互重复加载。"""
    return kb.KnowledgeBase(kb_id)


def extract_text(uploaded_file) -> str:
    """从上传文件提取纯文本，支持 txt/md/pdf/docx/pptx/xlsx。"""
    ext = uploaded_file.name.rsplit(".", 1)[-1].lower()
    if ext in ("txt", "md", "markdown", "csv", "log"):
        return uploaded_file.read().decode("utf-8", errors="ignore")
    if ext == "pdf":
        import pypdf

        reader = pypdf.PdfReader(uploaded_file)
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    if ext == "docx":
        import docx

        doc = docx.Document(uploaded_file)
        parts = []
        for p in doc.paragraphs:
            if p.text.strip():
                parts.append(p.text)
        for table in doc.tables:
            table_rows = []
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    table_rows.append(" | ".join(cells))
            if table_rows:
                parts.append("[表格]\n" + "\n".join(table_rows))
        return "\n".join(parts)
    if ext == "pptx":
        from pptx import Presentation

        texts = []
        for slide in Presentation(uploaded_file).slides:
            for shape in slide.shapes:
                if shape.has_text_frame:
                    texts.append(
                        "\n".join(p.text for p in shape.text_frame.paragraphs)
                    )
                if shape.has_table:
                    table_rows = []
                    for row in shape.table.rows:
                        cells = [c.text.strip() for c in row.cells if c.text.strip()]
                        if cells:
                            table_rows.append(" | ".join(cells))
                    if table_rows:
                        texts.append("[表格]\n" + "\n".join(table_rows))
        return "\n".join(t for t in texts if t.strip())
    if ext == "xlsx":
        import openpyxl

        wb = openpyxl.load_workbook(uploaded_file, read_only=True, data_only=True)
        lines = []
        for ws in wb.worksheets:
            lines.append(f"[工作表：{ws.title}]")
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None]
                if cells:
                    lines.append("\t".join(cells))
        return "\n".join(lines)
    raise ValueError(f"不支持的文件类型：{ext}")


def _truncate_history(messages: list, max_rounds: int = MAX_HISTORY_ROUNDS,
                      max_chars: int = MAX_HISTORY_CHARS) -> list:
    """截断历史对话：保留最近 N 轮对话，同时限制总字符数。
    始终保留 system 消息（通过外部传入），仅截断 user/assistant 轮次。
    """
    if not messages:
        return messages

    non_system = [m for m in messages if m["role"] != "system"]
    system_msgs = [m for m in messages if m["role"] == "system"]

    # 按轮次倒序保留（一轮 = 一个 user + 一个 assistant）
    rounds = []
    i = len(non_system) - 1
    while i >= 0:
        if non_system[i]["role"] == "assistant" and i > 0 and non_system[i - 1]["role"] == "user":
            rounds.insert(0, [non_system[i - 1], non_system[i]])
            i -= 2
        else:
            rounds.insert(0, [non_system[i]])
            i -= 1
        if len(rounds) >= max_rounds:
            break

    # 限制总字符数
    result_msgs = []
    total_chars = 0
    for round_pair in reversed(rounds):
        round_chars = sum(len(m.get("content", "")) for m in round_pair)
        if total_chars + round_chars > max_chars and result_msgs:
            break
        result_msgs = round_pair + result_msgs
        total_chars += round_chars

    return system_msgs + result_msgs


def _load_topics() -> dict:
    """读取历史话题（按助手名分组）。"""
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_topics(store: dict):
    HISTORY_FILE.write_text(
        json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _new_topic() -> dict:
    return {
        "id": uuid.uuid4().hex[:8],
        "title": "新话题",
        "created": time.time(),
        "messages": [],
    }


@st.cache_resource
def _penguin_b64() -> str:
    """企鹅形象图转 base64，供 CSS 背景与欢迎卡片内联使用。"""
    return base64.b64encode(PENGUIN_IMG.read_bytes()).decode("ascii")


def _load_css() -> str:
    """从 styles/app.css 加载主题样式，替换企鹅 base64 占位符。"""
    css_file = STYLES_DIR / "app.css"
    if css_file.exists():
        css = css_file.read_text(encoding="utf-8")
    else:
        css = "<style></style>"
    return css


def inject_cute_theme():
    """注入咕咕嘎嘎可爱风：背景图 + 欢迎卡片。"""
    b64 = _penguin_b64()
    css = _load_css().replace("__PENGUIN_B64__", b64)
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)
    st.markdown(
        '<div class="penguin-welcome">'
        f'<img src="data:image/jpeg;base64,{b64}" alt="咕咕嘎嘎">'
        "<div><h1>咕咕嘎嘎来帮你啦～ 🐧</h1>"
        "<p>选择左侧助手与大模型，上传知识库文档后即可提问。可爱模式已开启，工作效率也要萌萌哒！</p></div></div>",
        unsafe_allow_html=True,
    )


@st.cache_resource
def _reviewer_prompt() -> str:
    """加载审查员系统提示词。"""
    f = PROMPTS_DIR / "reviewer.md"
    if f.exists():
        return f.read_text(encoding="utf-8").strip()
    return "你是一名严格的 AI 输出审查员，请对回复进行结构化质量审查并按指定格式输出。"


def _run_self_review(client, model_name: str, query: str,
                     kb_context: str, upload_context: str,
                     assistant_reply: str) -> str | None:
    """执行自我审查，返回审查结果文本；失败返回 None。"""
    reviewer_system = _reviewer_prompt()
    review_user = (
        f"【用户问题】\n{query}\n\n"
        f"【知识库检索资料】\n{kb_context or '（无）'}\n\n"
        f"【本次上传文件】\n{upload_context or '（无）'}\n\n"
        f"【助手回复】\n{assistant_reply}\n\n"
        "请按审查维度逐项评估并输出结果。"
    )
    try:
        resp = client.chat.completions.create(
            model=REVIEW_MODEL or model_name,
            temperature=REVIEW_TEMP,
            messages=[
                {"role": "system", "content": reviewer_system},
                {"role": "user", "content": review_user},
            ],
        )
        return resp.choices[0].message.content
    except Exception:
        return None


if "topic_store" not in st.session_state:
    st.session_state.topic_store = _load_topics()
if "cur_topic" not in st.session_state:
    st.session_state.cur_topic = {}
if "rename_topic_id" not in st.session_state:
    st.session_state.rename_topic_id = None

inject_cute_theme()

# ---------- 侧边栏 ----------
with st.sidebar:
    assistants = {}
    if PROMPTS_DIR.exists():
        assistants = {p.stem: p for p in sorted(PROMPTS_DIR.glob("*.md"))}
    if not assistants:
        st.error("prompts/ 目录下没有助手提示词文件（.md），请先添加。")
        st.stop()
    names = list(assistants.keys())
    if st.session_state.get("assistant_sel") not in names:
        st.session_state.pop("assistant_sel", None)
    chosen = st.selectbox("当前助手", names, key="assistant_sel")
    SYSTEM_PROMPT = assistants[chosen].read_text(encoding="utf-8").strip()
    knowledge_base = _get_kb(chosen)  # 使用缓存的知识库实例

    st.title(f"🐧 {chosen}")

    # ---------- 历史话题（各助手独立，上下文互不串扰） ----------
    store = st.session_state.topic_store
    topics = store.setdefault(chosen, [])
    cur = next(
        (t for t in topics if t["id"] == st.session_state.cur_topic.get(chosen)),
        None,
    )
    if cur is None:  # 首次进入或当前话题已被删除
        cur = _new_topic()
        topics.insert(0, cur)
        st.session_state.cur_topic[chosen] = cur["id"]
        _save_topics(store)
    messages = cur["messages"]

    st.subheader("历史对话")
    if st.button("＋ 新建话题", use_container_width=True, key="new_topic"):
        cur = _new_topic()
        topics.insert(0, cur)
        st.session_state.cur_topic[chosen] = cur["id"]
        _save_topics(store)
        st.rerun()

    for t in topics:
        c1, c2, c3 = st.columns([5, 1, 1])
        with c1:
            is_renaming = st.session_state.rename_topic_id == t["id"]
            if is_renaming:
                new_title = st.text_input(
                    "重命名话题",
                    value=t["title"],
                    key=f"rename_input_{t['id']}",
                    label_visibility="collapsed",
                )
                if st.button("✓", key=f"rename_ok_{t['id']}"):
                    t["title"] = new_title[:30] if new_title.strip() else t["title"]
                    st.session_state.rename_topic_id = None
                    _save_topics(store)
                    st.rerun()
                if st.button("✕", key=f"rename_cancel_{t['id']}"):
                    st.session_state.rename_topic_id = None
                    st.rerun()
            else:
                label = f"🐧 {t['title']}" if t["id"] == cur["id"] else f"💬 {t['title']}"
                if st.button(label, key=f"topic_{t['id']}", use_container_width=True):
                    st.session_state.cur_topic[chosen] = t["id"]
                    st.rerun()
        with c2:
            if st.button("✎", key=f"topic_rename_{t['id']}", help="重命名"):
                st.session_state.rename_topic_id = t["id"]
                st.rerun()
        with c3:
            if st.button("🗑", key=f"topic_del_{t['id']}", help="删除"):
                topics[:] = [x for x in topics if x["id"] != t["id"]]
                if st.session_state.cur_topic.get(chosen) == t["id"]:
                    st.session_state.cur_topic[chosen] = (
                        topics[0]["id"] if topics else None
                    )
                if st.session_state.rename_topic_id == t["id"]:
                    st.session_state.rename_topic_id = None
                _save_topics(store)
                st.rerun()

    options = _model_options()
    labels = list(options.keys())
    if st.session_state.get("model_sel") not in labels:
        st.session_state.pop("model_sel", None)
    if labels:
        label = st.selectbox("当前大模型", labels, key="model_sel")
        api_key, base_url, model_name = options[label]
        client = get_client(api_key, base_url)
        st.caption(f"当前模型：{model_name}")
    else:
        client = None
        st.error("未配置任何大模型密钥，请编辑 .env 文件后刷新页面。")

    st.subheader("Agent 自我审查")
    review_enabled = st.toggle(
        "启用回复审查",
        value=REVIEW_ENABLED,
        help="每次回复后自动进行质量审查（准确性/完整性/合规性等），审查结果可展开查看",
    )
    if review_enabled:
        review_model_label = REVIEW_MODEL or "复用当前对话模型"
        st.caption(f"审查模型：{review_model_label} | 温度：{REVIEW_TEMP}")

    st.subheader("知识库（智能检索）")
    embedding_ready = kb.get_embedding_client() is not None
    if not embedding_ready:
        st.warning("未配置向量化服务（.env 中的 EMBEDDING_* 配置项），知识库暂不可用。")

    kb_files = st.file_uploader(
        "拖入或点击上传文档入库（txt/md/pdf/docx/pptx/xlsx，可多选）",
        type=UPLOAD_TYPES,
        key="kb_uploader",
        accept_multiple_files=True,
        disabled=not embedding_ready,
    )
    for kb_file in kb_files or []:
        done_key = f"kb_done_{kb_file.name}_{kb_file.size}"
        if done_key not in st.session_state:
            with st.spinner(f"正在将 {kb_file.name} 加入知识库…"):
                try:
                    text = extract_text(kb_file)
                    n = knowledge_base.add_document(kb_file.name, text)
                    st.session_state[done_key] = (
                        "success",
                        f"{kb_file.name} 已入库（{n} 个分块）",
                    )
                except Exception as e:
                    st.session_state[done_key] = ("error", f"{kb_file.name} 入库失败：{e}")
        status, msg = st.session_state[done_key]
        (st.success if status == "success" else st.error)(msg)

    doc_names = knowledge_base.doc_names()
    st.caption(f"知识库：{len(doc_names)} 个文档 / {len(knowledge_base.entries)} 个分块")
    if doc_names:
        for name in doc_names:
            if st.button(f"删除：{name}", key=f"del_{name}"):
                knowledge_base.remove_document(name)
                _get_kb.clear()  # 清除缓存以便下次重新加载
                st.rerun()
        if st.button("清空知识库"):
            knowledge_base.clear()
            _get_kb.clear()
            st.rerun()

# ---------- 历史消息 ----------
for msg in messages:
    with st.chat_message(
        msg["role"],
        avatar=str(PENGUIN_IMG) if msg["role"] == "assistant" else None,
    ):
        st.markdown(msg["display"])
        if msg["role"] == "assistant" and msg.get("review"):
            with st.expander("🔍 自我审查结果", expanded=False):
                st.markdown(msg["review"])

# ---------- 输入与回复 ----------
submission = st.chat_input(
    "输入你的问题，📎可添加附件（悬浮📎查看支持格式）",
    accept_file="multiple",
    file_type=UPLOAD_TYPES,
)
if submission and ((submission.text or "").strip() or submission.files):
    user_input = (submission.text or "").strip()
    chat_files = list(submission.files or [])
    if not user_input and chat_files:
        user_input = "请结合我上传的附件内容进行回答。"

    if client is None:
        st.error("请先在 .env 中配置大模型密钥（OPENAI_API_KEY 或 EMBEDDING_API_KEY）。")
        st.stop()

    # 1) 本次附件文本（多文件拼接）
    upload_parts = []
    for f in chat_files:
        try:
            full = extract_text(f)
            part = full[:MAX_UPLOAD_CHARS]
            if len(full) > MAX_UPLOAD_CHARS:
                part += "\n（文件过长，已截断）"
            upload_parts.append(f"文件：{f.name}\n{part}")
        except Exception as e:
            upload_parts.append(f"文件：{f.name}（解析失败：{e}）")
    upload_text = "\n\n".join(upload_parts)

    # 2) 知识库检索
    kb_section = ""
    retrieval_hits = []
    if knowledge_base.entries and embedding_ready:
        try:
            hits = knowledge_base.search(user_input)
            retrieval_hits = hits
            if hits:
                kb_section = "【知识库检索资料】\n" + "\n\n".join(
                    f"[来源：{h['doc']}]\n{h['text']}" for h in hits
                )
        except Exception as e:
            st.warning(f"知识库检索失败：{e}")

    # 3) 组装发送给模型的用户消息
    parts = [
        p
        for p in [
            kb_section,
            f"【本次上传文件内容】\n{upload_text}" if upload_text else "",
            f"【用户问题】\n{user_input}",
        ]
        if p
    ]
    api_content = "\n\n".join(parts)

    # 4) 上下文截断：发送给模型的历史对话
    messages_for_api = _truncate_history(
        [{"role": "system", "content": SYSTEM_PROMPT}] + messages
    )

    display = user_input
    if chat_files:
        display += "\n\n📎 " + "、".join(f.name for f in chat_files)
    messages.append({"role": "user", "display": display, "content": api_content})
    title_changed = False
    if len(messages) == 1:  # 话题首条消息自动作为话题标题
        cur["title"] = user_input.replace("\n", " ")[:18]
        title_changed = True
    with st.chat_message("user"):
        st.markdown(display)

    with st.chat_message("assistant", avatar=str(PENGUIN_IMG)):
        try:
            stream = client.chat.completions.create(
                model=model_name,
                messages=messages_for_api,
                stream=True,
            )

            def _content_stream():
                for chunk in stream:
                    if not chunk.choices:  # 跳过空 choices 的统计块，避免越界
                        continue
                    content = chunk.choices[0].delta.content
                    if content:  # 跳过空内容块，避免界面显示 None
                        yield content

            reply = st.write_stream(_content_stream())
            assistant_msg = {"role": "assistant", "display": reply, "content": reply}
            messages.append(assistant_msg)

            # 5) 检索来源展示（可展开）
            if retrieval_hits:
                with st.expander(f"📚 检索来源（{len(retrieval_hits)} 条）", expanded=False):
                    for i, hit in enumerate(retrieval_hits, 1):
                        st.markdown(
                            f"**{i}. {hit['doc']}**  "
                            f"<small>相关度: {hit.get('score', 0):.2f}</small>",
                            unsafe_allow_html=True,
                        )
                        st.markdown(f"> {hit['text'][:200]}{'...' if len(hit['text']) > 200 else ''}")

            # 6) Agent 自我审查（可展开）
            if review_enabled and client is not None:
                with st.spinner("🔍 Agent 自我审查中…"):
                    review_result = _run_self_review(
                        client=client,
                        model_name=model_name,
                        query=user_input,
                        kb_context=kb_section,
                        upload_context=upload_text,
                        assistant_reply=reply,
                    )
                if review_result:
                    assistant_msg["review"] = review_result
                    verdict = "warn"
                    for line in review_result.split("\n"):
                        if "总体评级" in line:
                            if "pass" in line.lower():
                                verdict = "pass"
                            elif "fail" in line.lower():
                                verdict = "fail"
                            break
                    verdict_icon = {"pass": "✅", "warn": "⚠️", "fail": "❌"}.get(verdict, "⚠️")
                    verdict_label = {"pass": "通过", "warn": "注意", "fail": "不通过"}.get(verdict, "审查")
                    with st.expander(
                        f"{verdict_icon} 自我审查：{verdict_label}",
                        expanded=(verdict == "fail"),
                    ):
                        st.markdown(review_result)
                else:
                    st.caption("🔍 自我审查：审查服务暂不可用，已跳过。")
        except Exception as e:  # 网络/鉴权/模型错误，保留会话可重试
            messages.pop()
            st.error(f"调用失败：{e}")
    _save_topics(store)  # 话题内容本地持久化
    if title_changed:  # 侧边栏先于提问渲染，标题变化后刷新一次以同步显示
        st.rerun()