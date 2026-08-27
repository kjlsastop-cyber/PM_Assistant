# -*- coding: utf-8 -*-
"""统一前置判断层：路由决策 + 边界检测 + 安全约束。

设计原则：
1. 纯 Python，零 Streamlit 依赖，返回结构化决策对象
2. 编辑链路永远输出副本，绝不覆盖源文件
3. 检测 SmartArt/动画/修订标记/复杂域等不可脚本修改的元素，提前警告
4. 路由规则：
   - 用户给 md/大纲/文本 → 生成链路（generate_ppt / generate_docx）
   - 用户上传已有 docx/pptx → 编辑链路（edit_ppt / edit_docx），禁止重建
"""
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class RouteDecision:
    route: str  # generate_ppt | generate_docx | edit_ppt | edit_docx
    warnings: list[str] = field(default_factory=list)
    safety: dict = field(default_factory=lambda: {"output_mode": "new"})
    boundary_warnings: list[str] = field(default_factory=list)


def detect_route(
    input_type: str,
    filename: str = "",
    content_bytes: Optional[bytes] = None,
) -> RouteDecision:
    """根据输入类型和文件内容决定路由。

    Args:
        input_type: "ppt_outline" | "docx_outline" | "edit_ppt" | "edit_docx" | "upload_ppt" | "upload_docx" | "upload_file" | "text"
        filename: 原始文件名（用于 MIME 判断）
        content_bytes: 文件二进制内容（用于边界检测）

    Returns:
        RouteDecision 对象，包含路由、警告、安全约束
    """
    decision = RouteDecision(route="generate_docx")

    ext = Path(filename).suffix.lower() if filename else ""

    # --- 路由判定 ---
    if input_type in ("ppt_outline",):
        decision.route = "generate_ppt"
    elif input_type in ("docx_outline", "text"):
        decision.route = "generate_docx"
    elif input_type in ("upload_ppt", "edit_ppt"):
        decision.route = "edit_ppt"
        decision.safety["output_mode"] = "copy"
    elif input_type in ("upload_docx", "edit_docx"):
        decision.route = "edit_docx"
        decision.safety["output_mode"] = "copy"
    elif ext in (".pptx", ".potx"):
        decision.route = "edit_ppt"
        decision.safety["output_mode"] = "copy"
        decision.warnings.append("检测到 PPT 文件，已切换到编辑链路（不会重建文档）")
    elif ext in (".docx", ".dotx"):
        decision.route = "edit_docx"
        decision.safety["output_mode"] = "copy"
        decision.warnings.append("检测到 DOCX 文件，已切换到编辑链路（不会重建文档）")
    elif ext in (".md", ".txt") or input_type == "text":
        decision.route = "generate_docx"
    else:
        decision.route = "generate_docx"

    # --- 边界检测 ---
    if content_bytes and ext in (".pptx", ".potx"):
        decision.boundary_warnings.extend(_detect_ppt_boundaries(content_bytes))
    elif content_bytes and ext in (".docx", ".dotx"):
        decision.boundary_warnings.extend(_detect_docx_boundaries(content_bytes))

    return decision


def _detect_ppt_boundaries(data: bytes) -> list[str]:
    """检测 PPT 中不可脚本修改的元素。"""
    warnings = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        slide_names = [n for n in zf.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")]
        for sn in slide_names:
            xml = zf.read(sn).decode("utf-8", errors="ignore")
            # SmartArt
            if "graphicFrame" in xml and "smartArt" in xml.lower():
                warnings.append("⚠️ 检测到 SmartArt 图形，hands-on-deck 无法修改 SmartArt，请手动调整")
                break
            # 动画
            if "<p:timing>" in xml:
                warnings.append("⚠️ 检测到幻灯片动画，修改后动画可能丢失或异常")
                break
    except Exception:
        pass
    return warnings


def _detect_docx_boundaries(data: bytes) -> list[str]:
    """检测 DOCX 中不可脚本修改的元素。"""
    warnings = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        if "word/document.xml" not in zf.namelist():
            return warnings
        xml = zf.read("word/document.xml").decode("utf-8", errors="ignore")
        # 修订标记
        if "<w:ins" in xml or "<w:del" in xml:
            warnings.append("⚠️ 检测到修订标记（Track Changes），脚本修改可能破坏修订历史")
        # 复杂域代码（排除 PAGE/NUMPAGES/TOC）
        fld_matches = re.findall(r"<w:fldChar[^>]*fldCharType=\"begin\"[^>]*/>", xml)
        instr_matches = re.findall(r"<w:instrText[^>]*>([^<]+)</w:instrText>", xml)
        for instr in instr_matches:
            instr_stripped = instr.strip()
            if instr_stripped.upper() not in ("PAGE", "NUMPAGES", "TOC", "TOCPAGE"):
                if len(instr_stripped) > 2:
                    warnings.append(f"⚠️ 检测到复杂域代码：{instr_stripped[:30]}...，脚本修改可能影响域更新")
                    break
        # 密码保护
        if "<w:documentProtection" in xml:
            warnings.append("⚠️ 文档可能受密码保护，修改可能失败")
    except Exception:
        pass
    return warnings


def should_generate(decision: RouteDecision) -> bool:
    """是否走生成链路。"""
    return decision.route.startswith("generate")


def should_edit(decision: RouteDecision) -> bool:
    """是否走编辑链路。"""
    return decision.route.startswith("edit")


def is_copy_only(decision: RouteDecision) -> bool:
    """是否只能输出副本（编辑链路强制 True）。"""
    return decision.safety.get("output_mode") == "copy"
