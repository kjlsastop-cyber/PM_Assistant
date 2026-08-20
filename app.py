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
PENGUIN_IMG = BASE_DIR / "assets" / "penguin.jpg"

st.set_page_config(page_title="助手工作台", page_icon="🐧", layout="wide")

# ---------- 初始化 ----------
load_dotenv(BASE_DIR / ".env")

UPLOAD_TYPES = ["txt", "md", "pdf", "docx", "pptx", "xlsx"]
MAX_UPLOAD_CHARS = 8000  # 本次附件注入上下文的最大字符数
HISTORY_FILE = BASE_DIR / "history_topics.json"  # 历史话题本地持久化


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

        return "\n".join(p.text for p in docx.Document(uploaded_file).paragraphs)
    if ext == "pptx":
        from pptx import Presentation

        texts = []
        for slide in Presentation(uploaded_file).slides:
            for shape in slide.shapes:
                if shape.has_text_frame:
                    texts.append(
                        "\n".join(p.text for p in shape.text_frame.paragraphs)
                    )
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


CUSTOM_CSS = """
<style>
  .stApp, body {
    background: linear-gradient(rgba(253,246,236,.38), rgba(253,246,236,.6)),
      url("data:image/jpeg;base64,__PENGUIN_B64__") center / cover fixed no-repeat !important;
  }
  header[data-testid="stHeader"] { background: transparent !important; }
  footer { visibility: hidden; }

  /* 侧边栏：奶油毛玻璃 */
  section[data-testid="stSidebar"] {
    background: rgba(255,252,246,.88) !important;
    backdrop-filter: blur(12px);
    border-right: 2px solid #fff;
  }

  /* 聊天气泡：奶油毛玻璃圆角卡片 */
  div[data-testid="stChatMessage"] {
    background: rgba(255,252,246,.88) !important;
    border: 2px solid #fff;
    border-radius: 20px;
    box-shadow: 0 4px 14px rgba(120,100,60,.18);
    backdrop-filter: blur(8px);
  }
  div[data-testid="stChatMessageAvatar"] img {
    border-radius: 50%;
    border: 2px solid #fff;
    box-shadow: 0 3px 8px rgba(120,100,60,.28);
  }

  /* 输入框：胶囊形 */
  div[data-testid="stChatInput"] {
    border-radius: 999px !important;
    border: 2px solid #fff !important;
    background: rgba(255,252,246,.9) !important;
    box-shadow: 0 8px 24px rgba(120,100,60,.25) !important;
  }
  div[data-testid="stChatInput"] textarea { background: transparent !important; }
  div[data-testid="stBottom"] > div { background: transparent !important; }
  /* 去掉内层嵌套容器的白底/边框，避免输入框看起来两层重叠 */
  div[data-testid="stChatInput"] > div {
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
  }
  /* 附件按钮：默认“+”号换成曲别针图标 */
  div[data-testid="stChatInputFileUploadButton"] button svg { display: none !important; }
  div[data-testid="stChatInputFileUploadButton"] button::before {
    content: "📎";
    font-size: 18px;
    line-height: 1;
  }

  /* 按钮：企鹅黑胶囊 */
  div.stButton > button {
    border-radius: 999px !important;
    background: #33383f !important;
    color: #fff !important;
    border: none !important;
    font-weight: 700;
  }

  /* 上传区：发卡蓝虚线圆角 */
  div[data-testid="stFileUploadDropzone"] {
    border: 2px dashed #7fb8dd !important;
    border-radius: 16px !important;
    background: rgba(127,184,221,.14) !important;
  }

  /* 下拉框：圆角白底 */
  div[data-testid="stSelectbox"] > div {
    border-radius: 14px !important;
    border: 2px solid #f3e7d3 !important;
    background: #fff !important;
  }

  /* 欢迎卡片 */
  .penguin-welcome {
    display: flex; gap: 18px; align-items: center;
    background: rgba(255,252,246,.88); border: 2px solid #fff; border-radius: 26px;
    padding: 18px 24px; margin: 4px 0 12px;
    box-shadow: 0 8px 28px rgba(120,100,60,.22); backdrop-filter: blur(12px);
  }
  .penguin-welcome img {
    width: 92px; height: 92px; border-radius: 22px; object-fit: cover;
    border: 3px solid #fff; box-shadow: 0 4px 12px rgba(242,185,92,.4);
  }
  .penguin-welcome h1 { font-size: 20px; margin: 0 0 6px; color: #33383f; }
  .penguin-welcome p { font-size: 13px; color: #8a7a5f; margin: 0; }
</style>
"""


def inject_cute_theme():
    """注入咕咕嘎嘎可爱风：背景图 + 欢迎卡片。"""
    b64 = _penguin_b64()
    st.markdown(CUSTOM_CSS.replace("__PENGUIN_B64__", b64), unsafe_allow_html=True)
    st.markdown(
        '<div class="penguin-welcome">'
        f'<img src="data:image/jpeg;base64,{b64}" alt="咕咕嘎嘎">'
        "<div><h1>咕咕嘎嘎来帮你啦～ 🐧</h1>"
        "<p>选择左侧助手与大模型，上传知识库文档后即可提问。可爱模式已开启，工作效率也要萌萌哒！</p></div></div>",
        unsafe_allow_html=True,
    )


if "topic_store" not in st.session_state:
    st.session_state.topic_store = _load_topics()
if "cur_topic" not in st.session_state:
    st.session_state.cur_topic = {}

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
    knowledge_base = kb.KnowledgeBase(chosen)  # 各助手知识库独立存储

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
        c1, c2 = st.columns([6, 1])
        with c1:
            label = f"🐧 {t['title']}" if t["id"] == cur["id"] else f"💬 {t['title']}"
            if st.button(label, key=f"topic_{t['id']}", use_container_width=True):
                st.session_state.cur_topic[chosen] = t["id"]
                st.rerun()
        with c2:
            if st.button("✕", key=f"topic_del_{t['id']}"):
                topics[:] = [x for x in topics if x["id"] != t["id"]]
                if st.session_state.cur_topic.get(chosen) == t["id"]:
                    st.session_state.cur_topic[chosen] = (
                        topics[0]["id"] if topics else None
                    )
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
                st.rerun()
        if st.button("清空知识库"):
            knowledge_base.clear()
            st.rerun()

# ---------- 历史消息 ----------
for msg in messages:
    with st.chat_message(
        msg["role"],
        avatar=str(PENGUIN_IMG) if msg["role"] == "assistant" else None,
    ):
        st.markdown(msg["display"])

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
    _save_topics(store)  # 话题内容本地持久化
    if title_changed:  # 侧边栏先于提问渲染，标题变化后刷新一次以同步显示
        st.rerun()
