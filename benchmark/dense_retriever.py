"""稠密向量检索后端(Dense Retriever)— 参考 Spring AI Alibaba 工具检索思路。

问题:TF-IDF(稀疏)只认词面重叠 —— query 与工具名/描述用词不同就零分。
企业任务的语义表达与工具命名常"同义不同词"(如 query="raise support tier of an
entitlement" vs 工具 update_entitlement),稀疏检索在此漏检(此前实测 zero_overlap
约占漏检 1/3)。稠密向量把工具语义签名(名字+描述+参数)与查询都编码成语义向量,
用余弦相似度捕获"意思相近但词面不重叠"的意图。

本模块设计(与 benchmark/tool_router.py 的 ToolRouter 配合):
- TextEmbedder       抽象:embed_texts(list[str]) -> np.ndarray (n, dim)
- SentenceTransformerEmbedder 本地开源模型实现(sentence-transformers 为可选依赖;
  默认 BAAI/bge-small-en-v1.5 —— 轻量英文 384 维,CPU 毫秒级,工具 schema 与
  任务文本均为英文,不需要多语模型)。
- APIEmbedder        预留:OpenAI 兼容 /embeddings 端点(当前无可用端点;ustc 网关
  是 Anthropic 兼容,不提供 embeddings)。后续有端点时只需实现 embed_texts。
- DenseIndex         工具池一次性编码为 (n,dim) 矩阵,查询时余弦 top-k。
- get_embedder()     模块级单例缓存:e2e 每任务重复构造 orchestrator/检索器时,
  模型只加载/只编码一次(服务端并发任务共享)。

依赖:可选 ``sentence-transformers``(与 torch)。安装:
    pip install '.[dense]'          # 或 uv sync --extra dense
未安装时请求 dense/hybrid 会抛出带安装提示的 ImportError —— 检索后端显式失败,
绝不静默降级(诚实性:实验口径必须真实可复现)。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"

# bge-en-v1.5 官方建议:短 query → passage 检索时为 query 加指令前缀。
# 工具检索中 query(能力描述)与 doc(签名)同域同风格,可省;保留参数以便调优。
BGE_QUERY_INSTRUCTION = ""
QUERY_INSTRUCTION_HINT = (
    "For asymmetric retrieval you may set query_instruction="
    "'Represent this sentence for searching relevant passages: ' (BGE-en v1.5)."
)


class TextEmbedder:
    """embed_texts(list[str]) -> np.ndarray (n, dim)。子类实现,向量无需预归一。

    embed_query 默认由 embed_texts([query])[0] 派生(抽象面保持单一);需要为
    query 加指令前缀的 embedder(如非对称检索)可覆写。
    """

    def embed_texts(self, texts: List[str]):
        raise NotImplementedError

    def embed_query(self, query: str):
        return self.embed_texts([query])[0]

    def __call__(self, texts: List[str]):
        return self.embed_texts(texts)


class SentenceTransformerEmbedder(TextEmbedder):
    """本地开源 embedding 模型(sentence-transformers,可选依赖懒加载)。

    Args:
        model_name: HF 模型名(默认 BAAI/bge-small-en-v1.5)。
        device: "cuda"/"cpu"/None(auto)。并发 e2e 建议 CPU(小模型毫秒级),
            显存受限时不要盲目上 cuda(每进程一份拷贝)。
        query_instruction: 可选 query 前缀(见模块 docstring)。
        encode_batch_size: 工具池编码分批大小(单域 ≤ 90 工具,默认 32 足够)。
    """

    def __init__(
        self,
        model_name: str = DEFAULT_EMBEDDING_MODEL,
        device: Optional[str] = None,
        query_instruction: str = BGE_QUERY_INSTRUCTION,
        encode_batch_size: int = 32,
    ):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:  # pragma: no cover - 依赖缺失路径
            raise ImportError(
                "Dense retrieval requires the optional 'dense' extra: "
                "pip install '.[dense]'  (sentence-transformers + torch). "
                f"Model requested: {model_name}"
            ) from e
        self.model_name = model_name
        self.device = device or self._auto_device()
        self.query_instruction = query_instruction
        self.encode_batch_size = encode_batch_size
        logger.info(f"[EMBED] loading model {model_name} on {self.device} ...")
        self._model = SentenceTransformer(model_name, device=self.device)
        logger.info(f"[EMBED] model ready: {model_name} (dim={self._model.get_sentence_embedding_dimension()})")

    @staticmethod
    def _auto_device() -> str:
        try:
            import torch

            return "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:  # noqa: BLE001 - torch 不可用时回 CPU
            return "cpu"

    def embed_texts(self, texts: List[str]):
        import numpy as np

        if not texts:
            return np.zeros((0, self._model.get_sentence_embedding_dimension()), dtype=np.float32)
        vecs = self._model.encode(
            texts,
            batch_size=self.encode_batch_size,
            normalize_embeddings=False,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return np.asarray(vecs, dtype=np.float32)

    def embed_query(self, query: str):
        text = f"{self.query_instruction}{query}" if self.query_instruction else query
        return self.embed_texts([text])[0]


class APIEmbedder(TextEmbedder):
    """预留:OpenAI 兼容 /embeddings 端点。

    当前无可用端点(项目 LLM 网关为 Anthropic 兼容);接入时实现 embed_texts:
    POST {endpoint}/embeddings {model, input:texts} -> data[].embedding,
    用 numpy 堆成 (n, dim)。与 SentenceTransformerEmbedder 同接口,上层无感。
    """

    def __init__(self, api_key: str, endpoint: str, model: str = "text-embedding-3-small"):
        raise NotImplementedError(
            "APIEmbedder is reserved for an OpenAI-compatible /embeddings endpoint; "
            "none is configured. Use SentenceTransformerEmbedder (local) instead."
        )


class DenseIndex:
    """工具池 -> 稠密矩阵;查询 -> 全池余弦相似度(与 TFIDFIndex.search 同形)。

    - 文档文本与 ToolRouter 共用 build_tool_signature 的签名(名字×2+desc+参数)。
    - L2 归一化由本类负责:矩阵与查询都归一,余弦 = 点积。
    """

    def __init__(self, embedder: TextEmbedder, docs: Dict[str, str]):
        self.doc_names = list(docs.keys())
        self.embedder = embedder
        texts = [docs[n] for n in self.doc_names]
        self.matrix = self._l2_normalize(embedder.embed_texts(texts))
        self._dim = self.matrix.shape[1]

    @staticmethod
    def _l2_normalize(mat):
        import numpy as np

        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (mat / norms).astype(np.float32)

    def _query_vec(self, query: str):
        import numpy as np

        vec = np.asarray(self.embedder.embed_query(query), dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        return vec / norm if norm > 0 else vec

    def search(self, query: str, top_k: int = 30) -> List[Tuple[float, str]]:
        """返回全池余弦相似度排序后的前 top_k (score, name)。"""
        if not query or not query.strip() or len(self.doc_names) == 0:
            return []
        import numpy as np

        q = self._query_vec(query)
        sims = self.matrix @ q  # (n,) 余弦(两向量均已归一)
        order = np.argsort(-sims)
        return [(float(sims[i]), self.doc_names[i]) for i in order[:top_k]]

    def search_all(self, query: str) -> List[Tuple[float, str]]:
        """全池排序(不截断),供 hybrid 融合用。"""
        if not query or not query.strip() or len(self.doc_names) == 0:
            return []
        import numpy as np

        q = self._query_vec(query)
        sims = self.matrix @ q
        order = np.argsort(-sims)
        return [(float(sims[i]), self.doc_names[i]) for i in order]


# ---------------------------------------------------------------------------
# 模块级单例缓存:e2e 每任务重建检索器时,模型只加载/编码一次
# ---------------------------------------------------------------------------

_EMBEDDER_CACHE: Dict[Tuple[str, Optional[str]], TextEmbedder] = {}


def get_embedder(
    model_name: str = DEFAULT_EMBEDDING_MODEL,
    device: Optional[str] = None,
) -> TextEmbedder:
    """获取(并缓存)embedder。同 (model, device) 只构造一次。"""
    key = (model_name, device)
    if key not in _EMBEDDER_CACHE:
        _EMBEDDER_CACHE[key] = SentenceTransformerEmbedder(model_name=model_name, device=device)
    return _EMBEDDER_CACHE[key]


def clear_embedder_cache() -> None:
    """测试用:清空模型缓存(释放显存/便于换模型)。"""
    _EMBEDDER_CACHE.clear()
