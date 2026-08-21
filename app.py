# -*- coding: utf-8 -*-
"""助手工作台（本地 Web 界面版，支持多助手切换 / 大模型选择 / 对话框附件 / 历史话题 / 各助手独立知识库 RAG）

启动：python -m streamlit run app.py --server.headless true --browser.gatherUsageStats false
"""
import base64
import datetime
import io
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv
from openai import OpenAI

import kb
from skill_router import detect_route, should_generate, should_edit, is_copy_only

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


def _extract_greeting(prompt_text: str) -> tuple[str, str]:
    """提取提示词文件内嵌的开场白（<!--GREETING ... GREETING-->），返回 (系统提示词, 开场白)。"""
    m = re.search(r"<!--GREETING\s*\n(.*?)\n\s*GREETING-->", prompt_text, re.DOTALL)
    if m:
        return prompt_text[: m.start()].strip(), m.group(1).strip()
    return prompt_text.strip(), ""


def _ppt_outline(client, model_name: str, content: str) -> dict | None:
    """调用模型把内容提炼为 PPT 页面大纲（JSON，含 layout 类型），失败返回 None。"""
    system = (
        "你是资深演示设计顾问。将用户内容改写为适合演示的 PPT 大纲，只输出纯 JSON，"
        "不要 markdown 代码块或其他任何文字。格式：\n"
        '{"title":"演示标题","slides":[{"type":"content","title":"页标题",'
        '"bullets":["要点1","要点2"],"notes":"演讲备注"}]}\n'
        "type 可选值：cover(封面), section(章节分隔), content(内容页), closing(结尾)。"
        "第一页自动视为 cover，最后一页自动视为 closing。"
        "中间页：章节大标题用 section，其余用 content。"
        "要求：总页数控制在 5~10 页；每页要点 3~5 条，每条不超过 30 字，"
        "提炼为观点式短句而非原文照搬；notes 为该页演讲提示，不超过 60 字。"
    )
    try:
        resp = client.chat.completions.create(
            model=model_name,
            temperature=0.4,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": content[:12000]},
            ],
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)  # 剥离可能的代码块包裹
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("slides"):
            return data
    except Exception:
        return None
    return None


def _fallback_outline(content: str) -> dict:
    """模型不可用/失败时的兜底：按 Markdown 标题与段落规则切分大纲。"""
    title, slides, cur_slide = "", [], None
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            t = line.lstrip("#").strip()
            if not title:
                title = t
                continue
            cur_slide = {"title": t, "bullets": [], "notes": ""}
            slides.append(cur_slide)
        else:
            if cur_slide is None:
                cur_slide = {"title": "内容要点", "bullets": [], "notes": ""}
                slides.append(cur_slide)
            cur_slide["bullets"].append(line.lstrip("-*• ").strip())
    for s in slides:
        s["bullets"] = [b for b in s["bullets"] if b][:6]
    if not slides:
        slides = [{"title": "内容要点", "bullets": [content[:120]], "notes": ""}]
    return {"title": title or "演示文稿", "slides": slides[:10]}


_TEMPLATE_CACHE: dict[str, bytes] = {}


def _get_template_bytes() -> bytes | None:
    """加载 PPT 模板文件，支持用户上传（st.session_state.ppt_template）或默认模板。"""
    key = st.session_state.get("_ppt_template_key", "default")
    if key in _TEMPLATE_CACHE:
        return _TEMPLATE_CACHE[key]
    # 优先使用用户上传的 me.pptx，其次是默认模板
    for name in ("me.pptx", "ppt_template.pptx"):
        tmpl_path = BASE_DIR / "assets" / name
        if tmpl_path.exists():
            data = tmpl_path.read_bytes()
            _TEMPLATE_CACHE[key] = data
            return data
    return None


def _fill_placeholder(ph, text: str):
    """向占位符写入文本，保留模板原有样式（字体/字号/颜色）。"""
    tf = ph.text_frame
    tf.word_wrap = True
    # 清空现有文本再写入
    tf.clear()
    p = tf.paragraphs[0]
    p.text = text
    for run in p.runs:
        run.font.name = "微软雅黑"


def _fill_body_placeholder(ph, bullets: list[str]):
    """向 body 占位符写入多条要点，保留模板样式并添加项目符号。"""
    from pptx.oxml.ns import qn
    from lxml import etree

    tf = ph.text_frame
    tf.word_wrap = True
    tf.clear()
    for i, bullet in enumerate(bullets):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.text = bullet
        # 添加项目符号（buFont + buChar）
        pPr = p._pPr
        if pPr is None:
            pPr = etree.SubElement(p._p, qn('a:pPr'))
        buChar = etree.SubElement(pPr, qn('a:buChar'), char='•')
        for run in p.runs:
            run.font.name = "微软雅黑"


def _build_pptx(outline: dict, source_title: str = "") -> bytes:
    """按大纲生成 PPT。优先使用模板（带占位符），失败时回退到空白画布。"""
    template_bytes = _get_template_bytes()
    if template_bytes is not None:
        return _build_pptx_from_template(outline, source_title, template_bytes)
    return _build_pptx_fallback(outline, source_title)


def _remove_slide(prs, slide_idx: int):
    """从演示文稿中删除指定索引的幻灯片。"""
    xml_slides = prs.slides._sldIdLst
    rels = prs.part.rels
    sldId = xml_slides[slide_idx]
    rId = sldId.rId
    xml_slides.remove(sldId)
    try:
        prs.part.drop_rel(rId)
    except Exception:
        pass


def _build_pptx_from_template(outline: dict, source_title: str, template_bytes: bytes) -> bytes:
    """基于模板生成 PPT：清空模板示例页 → 按版式选择布局 → 填充占位符。"""
    from pptx import Presentation

    prs = Presentation(io.BytesIO(template_bytes))

    # 清空模板中的示例幻灯片（倒序删除，避免索引偏移）
    for i in range(len(prs.slides) - 1, -1, -1):
        _remove_slide(prs, i)

    layouts = prs.slide_layouts
    LAYOUT_COVER = layouts[0] if len(layouts) > 0 else layouts[6]
    LAYOUT_CONTENT = layouts[1] if len(layouts) > 1 else layouts[6]
    LAYOUT_SECTION = layouts[2] if len(layouts) > 2 else layouts[1]
    LAYOUT_CLOSING = layouts[0] if len(layouts) > 0 else layouts[6]

    slides_data = outline.get("slides", [])
    title = str(outline.get("title") or source_title or "演示文稿").strip()

    # 1) 封面
    cover = prs.slides.add_slide(LAYOUT_COVER)
    for ph in cover.placeholders:
        idx = ph.placeholder_format.idx
        if idx == 0:
            _fill_placeholder(ph, title)
        elif idx == 1:
            _fill_placeholder(ph, "🐧 由咕咕嘎嘎助手生成")

    # 2) 章节页 + 内容页
    for i, s in enumerate(slides_data):
        s_type = str(s.get("type") or "content").strip().lower()
        s_title = str(s.get("title") or "").strip()
        bullets = [str(b).strip() for b in (s.get("bullets") or []) if str(b).strip()][:6]

        if s_type in ("section", "divider", "chapter"):
            slide = prs.slides.add_slide(LAYOUT_SECTION)
            for ph in slide.placeholders:
                idx = ph.placeholder_format.idx
                if idx == 0:
                    _fill_placeholder(ph, s_title)
                elif idx == 1:
                    desc = str(s.get("description") or s.get("notes") or "").strip()
                    _fill_placeholder(ph, desc)
        else:
            slide = prs.slides.add_slide(LAYOUT_CONTENT)
            for ph in slide.placeholders:
                idx = ph.placeholder_format.idx
                if idx == 0:
                    _fill_placeholder(ph, s_title)
                elif idx == 1:
                    _fill_body_placeholder(ph, bullets)
            notes = str(s.get("notes") or "").strip()
            if notes:
                try:
                    slide.notes_slide.notes_text_frame.text = notes
                except Exception:
                    pass

    # 3) 结尾页
    closing = prs.slides.add_slide(LAYOUT_CLOSING)
    for ph in closing.placeholders:
        idx = ph.placeholder_format.idx
        if idx == 0:
            _fill_placeholder(ph, "感谢聆听")
        elif idx == 1:
            _fill_placeholder(ph, "THANK YOU")

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _build_pptx_fallback(outline: dict, source_title: str = "") -> bytes:
    """兜底：在空白画布上硬画（模板不可用时）。"""
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches, Pt

    BG = RGBColor(0xFD, 0xF6, 0xEC)
    ACCENT = RGBColor(0xF2, 0xB9, 0x5C)
    DARK = RGBColor(0x33, 0x38, 0x3F)
    GRAY = RGBColor(0x8A, 0x7A, 0x5F)

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    blank = prs.slide_layouts[6]

    def _new_slide():
        s = prs.slides.add_slide(blank)
        s.background.fill.solid()
        s.background.fill.fore_color.rgb = BG
        return s

    title = str(outline.get("title") or source_title or "演示文稿").strip()
    cover = _new_slide()
    tb = cover.shapes.add_textbox(Inches(1.2), Inches(2.7), Inches(10.9), Inches(1.6))
    tf = tb.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = title
    p.font.size, p.font.bold, p.font.color.rgb = Pt(40), True, DARK
    sub = cover.shapes.add_textbox(Inches(1.25), Inches(4.4), Inches(10.9), Inches(0.6))
    sp = sub.text_frame.paragraphs[0]
    sp.text = "🐧 由咕咕嘎嘎助手生成"
    sp.font.size, sp.font.color.rgb = Pt(16), GRAY

    for i, s in enumerate(outline.get("slides", []), 1):
        slide = _new_slide()
        title_tb = slide.shapes.add_textbox(Inches(0.8), Inches(0.5), Inches(11.7), Inches(0.9))
        tf = title_tb.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.text = f"{i}. {str(s.get('title') or '').strip()}"
        p.font.size, p.font.bold, p.font.color.rgb = Pt(28), True, DARK
        bar = slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE, Inches(0.85), Inches(1.45), Inches(1.6), Inches(0.07)
        )
        bar.fill.solid()
        bar.fill.fore_color.rgb = ACCENT
        bar.line.fill.background()
        bullets = [str(b).strip() for b in (s.get("bullets") or []) if str(b).strip()][:6]
        body = slide.shapes.add_textbox(Inches(1.1), Inches(1.9), Inches(11.1), Inches(4.9))
        tf = body.text_frame
        tf.word_wrap = True
        for j, b in enumerate(bullets):
            para = tf.paragraphs[0] if j == 0 else tf.add_paragraph()
            para.text = f"• {b}"
            para.font.size, para.font.color.rgb = Pt(20), DARK
            para.space_after = Pt(12)
        notes = str(s.get("notes") or "").strip()
        if notes:
            slide.notes_slide.notes_text_frame.text = notes

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _build_docx(topic_title: str, messages: list) -> bytes:
    """把当前话题导出为 Word 文档。

    遵循 anthropics/skills docx 最佳实践：
    - 使用内置 Heading 样式（非自定义），确保目录自动生成
    - 列表使用 numbering/bullet 而非手动 • 字符
    - 表格使用 dual widths（列宽 + 单元格宽）
    - 页边距、页码、页眉页脚完整
    - 不使用 \\n，用独立 Paragraph
    """
    from docx import Document
    from docx.shared import Inches, Pt, Emu, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    doc = Document()

    # --- 页面设置：A4，标准边距 ---
    section = doc.sections[0]
    section.page_width = Emu(12240)
    section.page_height = Emu(15840)
    section.top_margin = Emu(1440)
    section.bottom_margin = Emu(1440)
    section.left_margin = Emu(1440)
    section.right_margin = Emu(1440)

    # --- 页眉 ---
    header = section.header
    hp = header.paragraphs[0]
    hp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = hp.add_run(topic_title or "对话记录")
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(0x8A, 0x7A, 0x5F)

    # --- 页脚 + 页码 ---
    footer = section.footer
    fp = footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run1 = fp.add_run("第 ")
    run1.font.size = Pt(9)
    run1.font.color.rgb = RGBColor(0x8A, 0x7A, 0x5F)
    fld_begin = OxmlElement('w:fldChar')
    fld_begin.set(qn('w:fldCharType'), 'begin')
    instr = OxmlElement('w:instrText')
    instr.text = 'PAGE'
    fld_end = OxmlElement('w:fldChar')
    fld_end.set(qn('w:fldCharType'), 'end')
    run2 = fp.add_run()
    run2._r.append(fld_begin)
    run2._r.append(instr)
    run2._r.append(fld_end)
    run2.font.size = Pt(9)
    run2.font.color.rgb = RGBColor(0x8A, 0x7A, 0x5F)
    run3 = fp.add_run(" 页 / 共 ")
    run3.font.size = Pt(9)
    run3.font.color.rgb = RGBColor(0x8A, 0x7A, 0x5F)
    run4 = fp.add_run()
    fld_begin2 = OxmlElement('w:fldChar')
    fld_begin2.set(qn('w:fldCharType'), 'begin')
    instr2 = OxmlElement('w:instrText')
    instr2.text = 'NUMPAGES'
    fld_end2 = OxmlElement('w:fldChar')
    fld_end2.set(qn('w:fldCharType'), 'end')
    run4._r.append(fld_begin2)
    run4._r.append(instr2)
    run4._r.append(fld_end2)
    run4.font.size = Pt(9)
    run4.font.color.rgb = RGBColor(0x8A, 0x7A, 0x5F)
    run5 = fp.add_run(" 页")
    run5.font.size = Pt(9)
    run5.font.color.rgb = RGBColor(0x8A, 0x7A, 0x5F)

    # --- 文档标题（Heading 0 = Title）---
    title_h = doc.add_heading(topic_title or "对话记录", level=0)
    for run in title_h.runs:
        run.font.color.rgb = RGBColor(0x1E, 0x3A, 0x5F)

    # --- 生成日期 ---
    date_p = doc.add_paragraph()
    date_run = date_p.add_run(f"生成日期：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    date_run.font.size = Pt(10)
    date_run.font.color.rgb = RGBColor(0x8A, 0x7A, 0x5F)

    # --- 分隔线 ---
    sep = doc.add_paragraph()
    sep_run = sep.add_run("─" * 60)
    sep_run.font.size = Pt(6)
    sep_run.font.color.rgb = RGBColor(0xF2, 0xB9, 0x5C)

    # --- 对话内容 ---
    for m in messages:
        if m.get("greeting"):
            continue
        role = m["role"]
        display = m.get("display", "").strip()
        if not display:
            continue

        # 角色标题
        if role == "user":
            h = doc.add_heading("🧑 用户提问", level=2)
            for run in h.runs:
                run.font.color.rgb = RGBColor(0x1E, 0x3A, 0x5F)
        else:
            h = doc.add_heading("🐧 助手回答", level=2)
            for run in h.runs:
                run.font.color.rgb = RGBColor(0x1E, 0x3A, 0x5F)

        # 正文段落（按 \n 拆分，每段独立 Paragraph）
        for line in display.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            # 检测列表项（以 -/•/数字. 开头）
            if _is_list_item(stripped):
                p = doc.add_paragraph(stripped, style="List Bullet")
            else:
                p = doc.add_paragraph(stripped)
            for run in p.runs:
                run.font.size = Pt(11)

    # --- 结尾 ---
    doc.add_paragraph()
    end_p = doc.add_paragraph()
    end_run = end_p.add_run("—— 由咕咕嘎嘎 PM Assistant 生成 ——")
    end_run.font.size = Pt(9)
    end_run.font.color.rgb = RGBColor(0x8A, 0x7A, 0x5F)
    end_p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _is_list_item(text: str) -> bool:
    """判断是否为列表项（以 -, *, •, 数字., 数字) 开头）。"""
    import re
    return bool(re.match(r'^[-*•]|\d+[.)、]', text))


def _modify_pptx(pptx_bytes: bytes, patch_ops: list[dict]) -> bytes | None:
    """使用 hands-on-deck (deck.py) 修改 PPT。

    patch_ops 格式：[{"op": "replace-text", "scope": "deck", "from": "旧", "to": "新"}, ...]
    支持的 op：replace-text, replace-color, set-text, delete, duplicate, move, resize,
              set-style, add-shape, add-picture, add-table, add-slide, set-notes, set-props 等
    """
    hod_dir = BASE_DIR / "hands_on_deck"
    deck_py = hod_dir / "scripts_deck.py"
    if not deck_py.exists():
        return None

    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = Path(tmpdir) / "input.pptx"
        output_path = Path(tmpdir) / "output.pptx"
        patch_path = Path(tmpdir) / "patch.json"

        input_path.write_bytes(pptx_bytes)
        patch_path.write_text(json.dumps(patch_ops, ensure_ascii=False), encoding="utf-8")

        result = subprocess.run(
            [sys.executable, str(deck_py), str(input_path), "apply",
             str(patch_path), "-o", str(output_path), "--fix"],
            capture_output=True, text=True, cwd=str(hod_dir), timeout=120
        )
        if result.returncode == 0 and output_path.exists():
            return output_path.read_bytes()
        return None


def _autodownload(ext: str):
    """注入 JS 自动触发一次下载点击，实现“生成完直接下载”。"""
    import streamlit.components.v1 as components

    js = (
        "const links = window.parent.document.querySelectorAll('a[download$=\"" + ext + "\"]');"
        "if (links.length) links[links.length - 1].click();"
    )
    components.html(f"<script>{js}</script>", height=0)


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
        assistants = {
            p.stem: p for p in sorted(PROMPTS_DIR.glob("*.md"))
            if p.stem != "reviewer"
        }
    if not assistants:
        st.error("prompts/ 目录下没有助手提示词文件（.md），请先添加。")
        st.stop()
    names = list(assistants.keys())
    if st.session_state.get("assistant_sel") not in names:
        st.session_state.pop("assistant_sel", None)
    chosen = st.selectbox("当前助手", names, key="assistant_sel")
    SYSTEM_PROMPT, GREETING = _extract_greeting(
        assistants[chosen].read_text(encoding="utf-8")
    )
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
    if not messages and GREETING:  # 新话题由助手主动发起开场白（仅展示，不进模型上下文）
        messages.append(
            {"role": "assistant", "display": GREETING, "content": "", "greeting": True}
        )
        _save_topics(store)

    st.subheader("历史对话")
    if st.button("+ 新建话题", key="new_topic", type="tertiary", use_container_width=True):
        cur = _new_topic()
        topics.insert(0, cur)
        st.session_state.cur_topic[chosen] = cur["id"]
        _save_topics(store)
        st.rerun()

    for t in topics:
        is_renaming = st.session_state.rename_topic_id == t["id"]
        if is_renaming:
            new_title = st.text_input(
                "重命名话题",
                value=t["title"],
                key=f"rename_input_{t['id']}",
                label_visibility="collapsed",
            )
            c1, c2 = st.columns([1, 1])
            with c1:
                if st.button("✓", key=f"rename_ok_{t['id']}", type="tertiary"):
                    t["title"] = new_title[:30] if new_title.strip() else t["title"]
                    st.session_state.rename_topic_id = None
                    _save_topics(store)
                    st.rerun()
            with c2:
                if st.button("✕", key=f"rename_cancel_{t['id']}", type="tertiary"):
                    st.session_state.rename_topic_id = None
                    st.rerun()
        else:
            c1, c2, c3 = st.columns([6, 1, 1])
            with c1:
                is_active = t["id"] == cur["id"]
                label = f"{'�' if is_active else '��'} {t['title']}"
                if st.button(
                    label,
                    key=f"topic_{t['id']}",
                    type="primary" if is_active else "secondary",
                    use_container_width=True,
                ):
                    st.session_state.cur_topic[chosen] = t["id"]
                    st.rerun()
            with c2:
                if st.button(
                    "✎", key=f"topic_rename_{t['id']}", type="tertiary", help="重命名"
                ):
                    st.session_state.rename_topic_id = t["id"]
                    st.rerun()
            with c3:
                if st.button(
                    "🗑", key=f"topic_del_{t['id']}", type="tertiary", help="删除"
                ):
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
        st.error("未配置任何大模型密钥：本地请编辑 .env；Streamlit Cloud 请在 Settings → Secrets 中配置后重启应用。")

    st.subheader("Agent 自我审查")
    review_enabled = st.toggle(
        "启用回复审查",
        value=REVIEW_ENABLED,
        help="每次回复后自动进行质量审查（准确性/完整性/合规性等），审查结果可展开查看",
    )
    if review_enabled:
        review_model_label = REVIEW_MODEL or "复用当前对话模型"
        st.caption(f"审查模型：{review_model_label} | 温度：{REVIEW_TEMP}")

    st.subheader("PPT 模板")
    tmpl = st.file_uploader(
        "上传 .pptx 模板（可选，用于生成 PPT 时套用样式）",
        type=["pptx"],
        key="ppt_template_uploader",
        help="模板需含标准占位符（标题/正文）。不上传则使用默认企鹅奶油风模板。",
    )
    if tmpl:
        st.session_state["_ppt_template_key"] = f"user_{tmpl.size}_{tmpl.name}"
        _TEMPLATE_CACHE[f"user_{tmpl.size}_{tmpl.name}"] = tmpl.read()
        st.success(f"已加载模板：{tmpl.name}")
    elif "_ppt_template_key" in st.session_state:
        if st.button("清除自定义模板", key="clear_tmpl"):
            del st.session_state["_ppt_template_key"]
            st.rerun()

    st.subheader("知识库（智能检索）")
    embedding_ready = kb.get_embedding_client() is not None
    if not embedding_ready:
        st.warning("未配置向量化服务（EMBEDDING_* 配置项：本地在 .env，线上在 Settings → Secrets），知识库暂不可用。")

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

# ---------- 产出工具栏：PPT 生成 / 对话导出 ----------
last_reply = next(
    (m for m in reversed(messages) if m["role"] == "assistant" and not m.get("greeting")),
    None,
)
has_content = any(not m.get("greeting") for m in messages)
_t1, _t2, _t3 = st.columns([3, 1, 1])
with _t2:
    ppt_btn = st.button(
        "📊 生成 PPT",
        disabled=last_reply is None or client is None,
        help="将助手最新回复提炼为演示结构，排版生成 .pptx（含演讲备注）",
    )
with _t3:
    doc_btn = st.button(
        "📤 导出对话",
        disabled=not has_content,
        help="把当前话题完整导出为 Word 文档",
    )

if ppt_btn and last_reply is not None and client is not None:
    with st.spinner("📊 正在分析内容并排版 PPT（约 10~30 秒）…"):
        src = last_reply.get("content") or last_reply.get("display") or ""
        outline = _ppt_outline(client, model_name, src) or _fallback_outline(src)
        st.session_state.ppt_bytes = _build_pptx(outline, cur["title"])
        st.session_state.ppt_autodl = True

if doc_btn and has_content:
    with st.spinner("📤 正在生成 Word 文档…"):
        st.session_state.docx_bytes = _build_docx(cur["title"], messages)
        st.session_state.docx_autodl = True

if st.session_state.get("ppt_bytes"):
    st.download_button(
        "⬇️ 下载 PPT",
        data=st.session_state["ppt_bytes"],
        file_name=f"{(cur['title'] or '演示文稿')[:20]}.pptx",
        mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        key="ppt_dl",
    )
    if st.session_state.pop("ppt_autodl", False):
        _autodownload(".pptx")

if st.session_state.get("docx_bytes"):
    st.download_button(
        "⬇️ 下载 Word",
        data=st.session_state["docx_bytes"],
        file_name=f"{(cur['title'] or '对话记录')[:20]}.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        key="docx_dl",
    )
    if st.session_state.pop("docx_autodl", False):
        _autodownload(".docx")

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
        st.error("请先配置大模型密钥（OPENAI_API_KEY 或 EMBEDDING_API_KEY）：本地编辑 .env，线上在 Settings → Secrets。")
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

    # 4) 先把本次用户消息写入会话，再组装发送给模型的消息（开场白仅展示，不进上下文）
    display = user_input
    if chat_files:
        display += "\n\n📎 " + "、".join(f.name for f in chat_files)
    messages.append({"role": "user", "display": display, "content": api_content})

    messages_for_api = _truncate_history(
        [{"role": "system", "content": SYSTEM_PROMPT}]
        + [m for m in messages if not m.get("greeting")]
    )
    title_changed = False
    if sum(1 for m in messages if m["role"] == "user") == 1:  # 首条提问自动作为话题标题
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