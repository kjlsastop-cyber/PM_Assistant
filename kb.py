# -*- coding: utf-8 -*-
"""本地知识库：文本分块、向量化（OpenAI 兼容 Embedding 接口）、余弦检索、JSON 持久化。"""
import json
import math
import os
from pathlib import Path

from openai import OpenAI

BASE_DIR = Path(__file__).parent
KB_FILE = BASE_DIR / "kb_store.json"

CHUNK_SIZE = int(os.getenv("KB_CHUNK_SIZE", "500"))
CHUNK_OVERLAP = int(os.getenv("KB_CHUNK_OVERLAP", "50"))
TOP_K = int(os.getenv("KB_TOP_K", "4"))
MIN_SCORE = float(os.getenv("KB_MIN_SCORE", "0.25"))
# DashScope 等接口限制单次最多 10 条，默认取 10
EMBED_BATCH = int(os.getenv("EMBEDDING_BATCH_SIZE", "10"))


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


class KnowledgeBase:
    """条目结构：{"doc": 文档名, "text": 分块文本, "vec": 向量}"""

    def __init__(self):
        self.entries = []
        if KB_FILE.exists():
            try:
                self.entries = json.loads(KB_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.entries = []

    def save(self):
        KB_FILE.write_text(
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
        """返回 [{"doc", "text", "score"}]，按相似度降序。"""
        client = get_embedding_client()
        if not self.entries or client is None:
            return []
        qvec = _embed(client, get_embedding_model(), [query])[0]
        scored = [
            {"doc": e["doc"], "text": e["text"], "score": _cosine(qvec, e["vec"])}
            for e in self.entries
        ]
        scored.sort(key=lambda x: x["score"], reverse=True)
        return [s for s in scored[:top_k] if s["score"] >= MIN_SCORE]
