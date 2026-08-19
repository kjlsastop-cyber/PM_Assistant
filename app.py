# -*- coding: utf-8 -*-
"""助手工作台（本地 Web 界面版，支持多助手切换 / 大模型选择 / 文件上传 / 各助手独立知识库 RAG）

启动：python -m streamlit run app.py --server.headless true --browser.gatherUsageStats false
"""
import os
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv
from openai import OpenAI

import kb

BASE_DIR = Path(__file__).parent
PROMPTS_DIR = BASE_DIR / "prompts"

st.set_page_config(page_title="助手工作台", page_icon="📋", layout="wide")

# ---------- 初始化 ----------
load_dotenv(BASE_DIR / ".env")

UPLOAD_TYPES = ["txt", "md", "pdf", "docx"]
MAX_UPLOAD_CHARS = 8000  # 本次附件注入上下文的最大字符数


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


def extract_text(uploaded_file) -> str:
    """从上传文件提取纯文本，支持 txt/md/pdf/docx。"""
    ext = uploaded_file.name.rsplit(".", 1)[-1].lower()
    if ext in ("txt", "md", "markdown", "csv", "log"):
        return uploaded_file.read().decode("utf-8", errors="ignore")
    if ext == "pdf":
        import pypdf

        reader = pypdf.PdfReader(uploaded_file)
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    if ext == "docx":
        import docx

        return "\n".join(p.text for p in docx.Document(uploaded_file).paragraphs)
    raise ValueError(f"不支持的文件类型：{ext}")


if "histories" not in st.session_state:
    st.session_state.histories = {}

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
    messages = st.session_state.histories.setdefault(chosen, [])
    knowledge_base = kb.KnowledgeBase(chosen)  # 各助手知识库独立存储

    st.title(f"📋 {chosen}")

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

    st.subheader("知识库（智能检索）")
    embedding_ready = kb.get_embedding_client() is not None
    if not embedding_ready:
        st.warning("未配置向量化服务（.env 中的 EMBEDDING_* 配置项），知识库暂不可用。")

    kb_files = st.file_uploader(
        "拖入或点击上传文档入库（txt/md/pdf/docx，可多选）",
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
                st.rerun()
        if st.button("清空知识库"):
            knowledge_base.clear()
            st.rerun()

    st.subheader("本次提问附件")
    chat_files = st.file_uploader(
        "拖入或点击上传附件（仅本次问题参考，可多选）",
        type=UPLOAD_TYPES,
        key="chat_uploader",
        accept_multiple_files=True,
    )

    if st.button("🗑️ 清空当前对话", use_container_width=True):
        messages.clear()
        st.rerun()

# ---------- 历史消息 ----------
for msg in messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["display"])

# ---------- 输入与回复 ----------
user_input = st.chat_input("输入你的问题，可配合左侧附件与知识库使用")
if user_input:
    if client is None:
        st.error("请先在 .env 中配置大模型密钥（OPENAI_API_KEY 或 EMBEDDING_API_KEY）。")
        st.stop()

    # 1) 本次附件文本（多文件拼接）
    upload_parts = []
    for f in chat_files or []:
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
    if knowledge_base.entries and embedding_ready:
        try:
            hits = knowledge_base.search(user_input)
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

    messages.append(
        {"role": "user", "display": user_input, "content": api_content}
    )
    with st.chat_message("user"):
        st.markdown(user_input)

    with st.chat_message("assistant"):
        try:
            stream = client.chat.completions.create(
                model=model_name,
                messages=[{"role": "system", "content": SYSTEM_PROMPT}]
                + [
                    {"role": m["role"], "content": m["content"]}
                    for m in messages
                ],
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
            messages.append(
                {"role": "assistant", "display": reply, "content": reply}
            )
        except Exception as e:  # 网络/鉴权/模型错误，保留会话可重试
            messages.pop()
            st.error(f"调用失败：{e}")
