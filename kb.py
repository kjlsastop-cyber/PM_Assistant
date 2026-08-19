# -*- coding: utf-8 -*-
"""本地知识库：分块、向量化、混合检索（向量+BM25 关键词，RRF 融合）、Rerank 重排、JSON 持久化（各助手独立存储）。"""
import json
import math
import os
import re
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from openai import OpenAI

BASE_DIR = Path(__file__).parent

CHUNK_SIZE = int(os.getenv("KB_CHUNK_SIZE", "500"))
CHUNK_OVERLAP = int(os.getenv("KB_CHUNK_OVERLAP", "50"))
TOP_K = int(os.getenv("KB_TOP_K", "4"))
MIN_SCORE = float(os.getenv("KB_MIN_SCORE", "0.25"))
# DashScope 等接口限制单次最多 10 条，默认取 10
EMBED_BATCH = int(os.getenv("EMBEDDING_BATCH_SIZE", "10"))
# Rerank 模型（与 Embedding 共用 EMBEDDING_API_KEY），留空则不启用重排
RERANK_MODEL = os.getenv("RERANK_MODEL", "").strip()
# 送入 Rerank 的候选数量
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


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP):
    """固定步长分块，overlap 保证语义连续。"""
    text = text.strip()
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        chunk = text[start : start + size].strip()
        if chunk:
            chunks.append(chunk)
        start += size - overlap
    return chunks


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


def _bm25_scores(query_tokens, texts, k1=1.5, b=0.75):
    """标准 BM25，返回与 texts 同序的分数列表。"""
    n = len(texts)
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
    """条目结构：{"doc": 文档名, "text": 分块文本, "vec": 向量}；按 kb_id 独立存储。"""

    def __init__(self, kb_id: str = "default"):
        self.kb_file = _kb_path(kb_id)
        self.entries = []
        if self.kb_file.exists():
            try:
                self.entries = json.loads(self.kb_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.entries = []

    def save(self):
        self.kb_file.write_text(
            json.dumps(self.entries, ensure_ascii=False), encoding="utf-8"
        )

    def doc_names(self):
        return sorted({e["doc"] for e in self.entries})

    def add_document(self, doc_name: str, text: str) -> int:
        client = get_embedding_client()
        if client is None:
            raise RuntimeError("未配置 EMBEDDING_API_KEY，无法向量化")
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
        self.save()

    def clear(self):
        self.entries = []
        self.save()

    def search(self, query: str, top_k: int = TOP_K):
        """混合检索：向量余弦 + BM25 关键词 → RRF 融合 → Rerank 重排。

        返回 [{"doc", "text", "score"}]，按相关性降序。
        """
        client = get_embedding_client()
        if not self.entries or client is None:
            return []
        texts = [e["text"] for e in self.entries]
        # 向量检索：余弦相似度
        qvec = _embed(client, get_embedding_model(), [query])[0]
        vec_scores = [_cosine(qvec, e["vec"]) for e in self.entries]
        # 关键词检索：BM25
        bm25_scores = _bm25_scores(tokenize(query), texts)
        # RRF 融合：对两路排名分别取倒数后求和
        rrf = {}
        for scores in (vec_scores, bm25_scores):
            order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
            for rank, idx in enumerate(order):
                rrf[idx] = rrf.get(idx, 0.0) + 1.0 / (RRF_K + rank + 1)
        fused = sorted(rrf.items(), key=lambda kv: kv[1], reverse=True)
        cand_idx = [idx for idx, _ in fused[: max(top_k, RERANK_CANDIDATES)]]
        candidates = [self.entries[i] for i in cand_idx]
        # Rerank 重排：失败/未配置时回退混合排序（按向量分过滤阈值）
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
        ]
