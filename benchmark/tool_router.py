"""ToolRouter v1: 纯 NumPy 实现的 TF-IDF + 余弦相似度粗筛,零额外依赖。

设计见 docs/tool_router_design.md(三级漏斗的第 ② 级,BM25 版)。
V2 升级点:把 TFIDFIndex 换成 sentence-transformers embedding 余弦,接口不变。

用法:
    router = ToolRouter(tools)            # tools: List[{"name","description","input_schema"}]
    subset, meta = router.route(task_text)
"""

import math
import re
from collections import Counter
from typing import Any, Dict, List, Tuple


# ---------------------------------------------------------------------------
# 文本处理
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> List[str]:
    """粗粒度 tokenize:小写 + 拆分 snake_case + 按非字母数字切分。"""
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9_]+", " ", text)
    toks: List[str] = []
    for w in text.split():
        toks.extend(w.split("_"))  # create_new_case -> create, new, case
    return [t for t in toks if len(t) > 1]


def build_tool_signature(tool: Dict[str, Any]) -> str:
    """工具语义签名 = 名称(加权×2)+ description 前两句 + 参数名列表。

    工具名是最强语义信号(find_entitlements 直接对应"查 entitlement"),
    重复出现使 TF 翻倍,缓解 TF-IDF 词汇不匹配问题。
    """
    name = tool["name"]
    desc = (tool.get("description") or "").split(".")[:2]
    desc = ". ".join(desc)[:200]
    params = list((tool.get("input_schema") or {}).get("properties", {}).keys())
    return f"{name} {name} | {desc} | params: {', '.join(params[:12])}"


# ---------------------------------------------------------------------------
# TF-IDF 检索索引(不依赖 sklearn / rank_bm25)
# ---------------------------------------------------------------------------


class TFIDFIndex:
    """文档集合 -> TF-IDF 权重;查询文本 -> 余弦相似度 top-k。"""

    def __init__(self, docs: Dict[str, str]):
        self.doc_names = list(docs.keys())
        self.doc_tokens = {n: _tokenize(d) for n, d in docs.items()}
        df: Counter = Counter()  # 文档频率
        for toks in self.doc_tokens.values():
            df.update(set(toks))
        self.N = len(self.doc_names)
        self.idf = {t: math.log((self.N + 1) / (f + 1)) + 1 for t, f in df.items()}

    def _tfidf_vec(self, toks: List[str]) -> Dict[str, float]:
        cnt = Counter(toks)
        return {t: cnt[t] / len(toks) * self.idf.get(t, 1) for t in cnt if t in self.idf}

    @staticmethod
    def _cosine(q: Dict[str, float], d: Dict[str, float]) -> float:
        dot = sum(q[t] * d.get(t, 0) for t in q)
        nq = math.sqrt(sum(v * v for v in q.values()))
        nd = math.sqrt(sum(v * v for v in d.values()))
        return dot / (nq * nd) if nq and nd else 0.0

    def search(self, query: str, top_k: int = 30) -> List[Tuple[float, str]]:
        qv = self._tfidf_vec(_tokenize(query))
        if not qv:
            return []
        scored = [(self._cosine(qv, self._tfidf_vec(toks)), name)
                  for name, toks in self.doc_tokens.items()]
        scored.sort(reverse=True)
        return scored[:top_k]


# ---------------------------------------------------------------------------
# 路由器(三级漏斗的第 ② 级;第 ③ 级精排、④⑤ 兜底见文档)
# ---------------------------------------------------------------------------


class ToolRouter:
    """TF-IDF 粗筛路由。route() 返回 (子集工具列表, 元数据)。"""

    def __init__(
        self,
        tools: List[Dict[str, Any]],
        k_candidate: int = 30,
        k_final: int = 20,
        min_score: float = 0.05,
    ):
        self.tools = tools
        self.by_name = {t["name"]: t for t in tools}
        docs = {t["name"]: build_tool_signature(t) for t in tools}
        self.index = TFIDFIndex(docs)
        self.k_candidate = k_candidate
        self.k_final = k_final
        self.min_score = min_score

    def route(self, task_text: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        hits = self.index.search(task_text, top_k=self.k_candidate)
        if not hits or hits[0][0] < self.min_score:
            # ⑤ 置信度回退:语义差异过大,回退全量
            return self.tools, {
                "fallback": "low_confidence",
                "top_score": hits[0][0] if hits else 0.0,
                "subset_size": len(self.tools),
            }
        subset = [self.by_name[n] for _, n in hits[: self.k_final]]
        return subset, {
            "candidate_size": len(hits),
            "subset_size": len(subset),
            "top_score": hits[0][0],
            "bottom_score": hits[min(self.k_final, len(hits)) - 1][0],
        }


def expand_tool(tool_name: str, full_pool: List[Dict[str, Any]]) -> Dict[str, Any] | None:
    """④ 渐进式扩展:执行中请求子集外工具时,从全量池取 schema。"""
    for t in full_pool:
        if t["name"] == tool_name:
            return t
    return None
