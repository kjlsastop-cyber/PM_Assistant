# -*- coding: utf-8 -*-
"""本地知识库：分块、向量化、混合检索（向量+BM25 关键词，RRF 融合）、Rerank 重排、JSON 持久化（各助手独立存储）。

向量存储格式：磁盘上为 base64 编码的 struct float 数组；内存中保持为 Python list，便于余弦计算。
BM25 分词在首次检索时预计算并缓存，条目变更时自动失效。
"""
import base64
import json
import math
import os
import re
import struct
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from openai import OpenAI

BASE_DIR = Path(__file__).parent

CHUNK_SIZE = int(os.getenv("KB_CHUNK_SIZE", "700"))
CHUNK_OVERLAP = int(os.getenv("KB_CHUNK_OVERLAP", "100"))
TOP_K = int(os.getenv("KB_TOP_K", "4"))
MIN_SCORE = float(os.getenv("KB_MIN_SCORE", "0.25"))
EMBED_BATCH = int(os.getenv("EMBEDDING_BATCH_SIZE", "10"))
RERANK_MODEL = os.getenv("RERANK_MODEL", "").strip()
RERANK_CANDIDATES = int(os.getenv("RERANK_CANDIDATES", "20"))
RRF_K = 60


def get_embedding_client():
    api_key = os.getenv("EMBEDDING_API_KEY", "").strip()
    if not api_key:
        return None
    return OpenAI(
        api_key=api_key,
        base_url=os.getenv("EMBEDDING_BASE_URL", "https://api.openai.com/v1").strip(),
    )


def get_embedding_model() -> str:
    return os.getenv("EMBEDDING_MODEL", "text-embedding-3-small").strip()


# 标题行识别：Markdown #、整行加粗、第X章、一、二、（一）、1.1 / 1. 等短编号行。
# 编号行限制内容长度并排除句读标点，避免把"1. 性能需求：……500ms。"这类列表项误判为标题。
_HEADING_RE = re.compile(
    r"^(?:"
    r"#{1,6}\s+\S.{0,60}"                                     # Markdown 标题
    r"|\*\*[^*]{1,40}\*\*"                                    # 整行加粗（常作小标题）
    r"|第[一二三四五六七八九十百千0-9]{1,6}[章篇部节][^。]{0,40}"   # 第X章/节
    r"|[一二三四五六七八九十]{1,4}[、.．]\s*\S[^。；，,]{0,24}"    # 一、二、
    r"|[（(][一二三四五六七八九十0-9]{1,4}[)）]\s*\S[^。]{0,24}"   # （一）
    r"|\d{1,3}\.\d+(?:\.\d+)*[、.．)）]?\s*\S[^。]{0,24}"         # 1.1 多级编号
    r"|\d{1,3}[、.．)）]\s*\S[^。；，,]{0,18}"                    # 1./1、 单级编号（限短标题）
    r"|\d{1,3}\s+\S[^。；，,]{0,18}"                             # 1 范围（国标风格）
    r")$"
)


def _split_sentences(text: str) -> list[str]:
    """按句末标点切句（保留标点），兼容中英文。"""
    parts = re.split(r"(?<=[。！？；!?;])|(?<=[.])(?=\s)", text)
    return [p.strip() for p in parts if p.strip()]


def _split_units(section: str, size: int) -> list[str]:
    """把节内容打散为不超过 size 的最小单元：段落 → 行 → 句 → 硬切兜底。"""
    units = []
    for para in re.split(r"\n\s*\n", section):
        para = para.strip()
        if not para:
            continue
        for line in para.splitlines():
            line = line.strip()
            if not line:
                continue
            if len(line) <= size:
                units.append(line)
                continue
            for sent in _split_sentences(line):
                if len(sent) <= size:
                    units.append(sent)
                else:  # 无标点超长串（长表格行/URL 等）：硬切
                    units.extend(
                        sent[i : i + size].strip()
                        for i in range(0, len(sent), size)
                    )
    return units


def _chunk_section(section: str, size: int) -> list[str]:
    """节内分块：把最小单元聚合为不超过 size 的块，换行连接保留结构。"""
    if len(section) <= size:
        return [section]
    chunks, buf = [], ""
    for unit in _split_units(section, size):
        if buf and len(buf) + len(unit) + 1 > size:
            chunks.append(buf)
            buf = unit
        else:
            buf = f"{buf}\n{unit}" if buf else unit
    if buf:
        chunks.append(buf)
    return chunks


def _tail_context(prev: str, overlap: int) -> str:
    """从上一块尾部取不超过 overlap 字符的完整句子，作为下一块的上下文回看。"""
    tail = prev[-(overlap * 2) :] if len(prev) > overlap * 2 else prev
    picked, total = [], 0
    for s in _split_sentences(tail):
        if picked and total + len(s) > overlap:
            break
        if not picked and len(s) > overlap:  # 单句超长：取句尾
            return s[-overlap:]
        picked.append(s)
        total += len(s)
    return "".join(picked)


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP):
    """结构感知分块：标题分节 → 节内段落/行/句聚合 → 相邻块尾部重叠。

    规则：
    1. 优先按标题行（Markdown #、第X章、一、/1.1/（一）等）切节，标题随块保留；
    2. 节内按空行分段、段内按行聚合，超长行再按句末标点切分；
    3. 同一节被拆成多块时，后续块自动补挂节标题，保持检索时的语义定位；
    4. 相邻块之间回看携带不超过 overlap 字符的完整句子作为上下文。
    """
    text = text.strip()
    if not text:
        return []

    # 1) 按标题行切节
    sections, heading, body = [], "", []
    for raw in text.splitlines():
        line = raw.strip()
        if line and _HEADING_RE.match(line):
            if heading or any(l.strip() for l in body):
                sections.append((heading, "\n".join(body).strip()))
            heading, body = line, []
        else:
            body.append(raw)
    if heading or any(l.strip() for l in body):
        sections.append((heading, "\n".join(body).strip()))

    # 1.5) 合并无正文的标题节（如父标题下直接是子标题）：标题链并入下一节，
    #      避免产生只有标题的孤块；末尾孤立标题并回上一节。
    merged, pending_heads = [], []
    for head, body_text in sections:
        if not body_text:
            if head:
                pending_heads.append(head)
            continue
        if pending_heads:
            head = "\n".join(pending_heads + ([head] if head else []))
            pending_heads = []
        merged.append((head, body_text))
    if pending_heads:
        if merged:
            last_head, last_body = merged[-1]
            merged[-1] = (last_head, f"{last_body}\n" + "\n".join(pending_heads))
        else:
            merged.append(("\n".join(pending_heads), ""))
    sections = merged

    # 2) 节内分块；非首块补挂节标题
    chunks = []
    for head, body_text in sections:
        sec = f"{head}\n{body_text}".strip() if head else body_text
        if not sec:
            continue
        for i, c in enumerate(_chunk_section(sec, size)):
            if i > 0 and head and not c.startswith(head):
                c = f"{head}\n{c}"
            chunks.append(c)

    # 3) 相邻块尾部重叠（按完整句子对齐）
    if overlap > 0 and len(chunks) > 1:
        result = [chunks[0]]
        for prev, cur in zip(chunks, chunks[1:]):
            ctx = _tail_context(prev, overlap)
            result.append(f"{ctx}\n{cur}" if ctx else cur)
        chunks = result

    return [c.strip() for c in chunks if c.strip()]


def _embed(client, model, texts):
    vectors = []
    for i in range(0, len(texts), EMBED_BATCH):
        resp = client.embeddings.create(model=model, input=texts[i : i + EMBED_BATCH])
        vectors.extend(d.embedding for d in resp.data)
    return vectors


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def tokenize(text: str):
    """轻量分词：英文/数字整词，中文按字二元组切分，供 BM25 使用。"""
    toks = []
    for run in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", text.lower()):
        if run.isascii():
            toks.append(run)
        elif len(run) == 1:
            toks.append(run)
        else:
            toks.extend(run[i : i + 2] for i in range(len(run) - 1))
    return toks


def _bm25_scores(query_tokens, texts, cached_tokens=None, cached_df=None, cached_avgdl=None, k1=1.5, b=0.75):
    """标准 BM25，返回与 texts 同序的分数列表。
    传入 cached_tokens / cached_df / cached_avgdl 时可跳过预计算。
    """
    n = len(texts)
    if n == 0:
        return []

    if cached_tokens is not None:
        doc_tokens = cached_tokens
        avgdl = cached_avgdl
        df = cached_df
    else:
        doc_tokens = [tokenize(t) for t in texts]
        avgdl = sum(len(t) for t in doc_tokens) / n if n else 0.0
        df = {}
        for toks in doc_tokens:
            for tok in set(toks):
                df[tok] = df.get(tok, 0) + 1

    scores = []
    for toks in doc_tokens:
        if not toks or not avgdl:
            scores.append(0.0)
            continue
        tf = {}
        for tok in toks:
            tf[tok] = tf.get(tok, 0) + 1
        score = 0.0
        for tok in query_tokens:
            if tok not in tf:
                continue
            idf = math.log(1 + (n - df[tok] + 0.5) / (df[tok] + 0.5))
            f = tf[tok]
            score += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * len(toks) / avgdl))
        scores.append(score)
    return scores


def get_rerank_config():
    """Rerank 与 Embedding 共用 EMBEDDING_API_KEY，返回 (url, api_key, model)，未配置返回 None。"""
    api_key = os.getenv("EMBEDDING_API_KEY", "").strip()
    if not api_key or not RERANK_MODEL:
        return None
    base = os.getenv(
        "EMBEDDING_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
    ).strip()
    netloc = urlsplit(base).netloc or "dashscope.aliyuncs.com"
    url = f"https://{netloc}/api/v1/services/rerank/text-rerank/text-rerank"
    return url, api_key, RERANK_MODEL


def _rerank(query: str, documents, top_n: int):
    """调用 DashScope Rerank 接口，返回 [(原文档下标, 相关度)]；失败返回 None。"""
    config = get_rerank_config()
    if config is None or not documents:
        return None
    url, api_key, model = config
    payload = {
        "model": model,
        "input": {"query": query, "documents": list(documents)},
        "parameters": {"top_n": top_n},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    results = (data.get("output") or {}).get("results") or data.get("results") or []
    if not results:
        return None
    return [(r["index"], r.get("relevance_score", 0.0)) for r in results]


def _kb_path(kb_id: str) -> Path:
    """每个助手对应独立存储文件，文件名过滤非法字符。"""
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", kb_id).strip("_") or "default"
    return BASE_DIR / f"kb_store_{safe}.json"


class KnowledgeBase:
    """条目结构：{"doc": 文档名, "text": 分块文本, "vec": 向量（磁盘 base64，内存 list）}；按 kb_id 独立存储。

    向量格式自动迁移：加载时若 vec 为 list（旧格式），会自动转为 base64 struct 格式并保存。
    BM25 分词与 df 在首次 search 时预计算缓存，add_document / remove_document / clear 时自动失效。
    """

    def __init__(self, kb_id: str = "default"):
        self.kb_file = _kb_path(kb_id)
        self.entries = []
        self._bm25_cache = None
        self._migrated = False
        if self.kb_file.exists():
            try:
                raw = json.loads(self.kb_file.read_text(encoding="utf-8"))
                self.entries = []
                for e in raw:
                    if isinstance(e.get("vec"), list):
                        self.entries.append(e)
                    elif isinstance(e.get("vec"), str):
                        decoded = self._decode_vec(e["vec"])
                        self.entries.append({**e, "vec": decoded})
                        self._migrated = True
                    else:
                        self.entries.append(e)
            except (json.JSONDecodeError, OSError):
                self.entries = []
        if self._migrated:
            self.save()

    @staticmethod
    def _encode_vec(vec_list: list) -> str:
        """list[float] → base64 struct 字符串。"""
        if not vec_list:
            return ""
        packed = struct.pack(f"{len(vec_list)}f", *vec_list)
        return base64.b64encode(packed).decode("ascii")

    @staticmethod
    def _decode_vec(vec_str: str) -> list:
        """base64 struct 字符串 → list[float]。"""
        if not vec_str:
            return []
        packed = base64.b64decode(vec_str)
        n = len(packed) // 4
        return list(struct.unpack(f"{n}f", packed))

    def save(self):
        out = []
        for e in self.entries:
            item = {
                "doc": e["doc"],
                "text": e["text"],
                "vec": self._encode_vec(e["vec"]),
            }
            out.append(item)
        self.kb_file.write_text(
            json.dumps(out, ensure_ascii=False), encoding="utf-8"
        )

    def _invalidate_cache(self):
        self._bm25_cache = None

    def doc_names(self):
        return sorted({e["doc"] for e in self.entries})

    def add_document(self, doc_name: str, text: str) -> int:
        client = get_embedding_client()
        if client is None:
            raise RuntimeError("未配置 EMBEDDING_API_KEY，无法向量化")
        self.entries = [e for e in self.entries if e["doc"] != doc_name]
        self._invalidate_cache()
        chunks = chunk_text(text)
        if not chunks:
            return 0
        vectors = _embed(client, get_embedding_model(), chunks)
        for chunk, vec in zip(chunks, vectors):
            self.entries.append({"doc": doc_name, "text": chunk, "vec": vec})
        self.save()
        return len(chunks)

    def remove_document(self, doc_name: str):
        self.entries = [e for e in self.entries if e["doc"] != doc_name]
        self._invalidate_cache()
        self.save()

    def clear(self):
        self.entries = []
        self._invalidate_cache()
        self.save()

    def _ensure_bm25_cache(self):
        if self._bm25_cache is not None:
            return
        texts = [e["text"] for e in self.entries]
        doc_tokens = [tokenize(t) for t in texts]
        n = len(texts)
        avgdl = sum(len(t) for t in doc_tokens) / n if n else 0.0
        df = {}
        for toks in doc_tokens:
            for tok in set(toks):
                df[tok] = df.get(tok, 0) + 1
        self._bm25_cache = (doc_tokens, df, avgdl)

    def search(self, query: str, top_k: int = TOP_K):
        """混合检索：向量余弦 + BM25 关键词 → RRF 融合 → Rerank 重排。
        统一 MIN_SCORE 阈值过滤：Rerank 路径用 relevance_score，非 Rerank 路径用余弦相似度。

        返回 [{"doc", "text", "score"}]，按相关性降序。
        """
        client = get_embedding_client()
        if not self.entries or client is None:
            return []
        texts = [e["text"] for e in self.entries]
        qvec = _embed(client, get_embedding_model(), [query])[0]
        vec_scores = [_cosine(qvec, e["vec"]) for e in self.entries]

        self._ensure_bm25_cache()
        cached_tokens, cached_df, cached_avgdl = self._bm25_cache
        bm25_scores = _bm25_scores(
            tokenize(query), texts,
            cached_tokens=cached_tokens,
            cached_df=cached_df,
            cached_avgdl=cached_avgdl,
        )

        rrf = {}
        for scores in (vec_scores, bm25_scores):
            order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
            for rank, idx in enumerate(order):
                rrf[idx] = rrf.get(idx, 0.0) + 1.0 / (RRF_K + rank + 1)
        fused = sorted(rrf.items(), key=lambda kv: kv[1], reverse=True)
        cand_idx = [idx for idx, _ in fused[: max(top_k, RERANK_CANDIDATES)]]
        candidates = [self.entries[i] for i in cand_idx]

        reranked = _rerank(query, [e["text"] for e in candidates], top_k)
        if reranked is None:
            return [
                {"doc": e["doc"], "text": e["text"], "score": vec_scores[cand_idx[i]]}
                for i, e in enumerate(candidates[:top_k])
                if vec_scores[cand_idx[i]] >= MIN_SCORE
            ]
        return [
            {"doc": candidates[idx]["doc"], "text": candidates[idx]["text"], "score": rel}
            for idx, rel in reranked[:top_k]
            if rel >= MIN_SCORE
        ]
