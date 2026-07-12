"""Cross-Encoder 重排器 — bge-reranker-base。

对 hybrid_searcher 的 RRF 融合结果进行二次排序，提升 Top-N 精度。

设计依据：
- docs/02-knowledge-base.md §9.5（Re-rank 优化）
- sentence-transformers 的 CrossEncoder 接口

降级策略：
- 模型加载失败 → rerank() 抛异常 → hybrid_searcher 捕获后降级为 RRF 排序
- results 不足 top_n → 直接返回，不触发模型推理
"""
from __future__ import annotations

import math
import threading

from app.config import settings
from app.utils.logger import logger


class CrossEncoderReranker:
    """bge-reranker-base Cross-Encoder 重排器。

    使用 sentence-transformers 的 CrossEncoder（已安装），而非 FlagEmbedding.FlagModel。
    FlagModel 是嵌入模型类（encode 方法），FlagReranker 才是重排类（compute_score 方法）；
    但 FlagEmbedding 在 Python 3.12 下无 wheel，安装失败。CrossEncoder 功能等价且已随
    sentence-transformers 安装，无需额外依赖。
    """

    def __init__(self):
        self._model = None
        self._lock = threading.Lock()

    def _ensure_model(self):
        """延迟加载模型（线程安全）。"""
        if self._model is None:
            with self._lock:
                if self._model is None:
                    import os
                    from sentence_transformers import CrossEncoder

                    model_name = settings.RERANKER_MODEL_NAME
                    device = settings.RERANKER_DEVICE

                    if os.path.isabs(model_name) and os.path.isfile(os.path.join(model_name, "config.json")):
                        logger.info("[Reranker] 加载本地模型: {}", model_name)
                    else:
                        logger.info("[Reranker] 加载模型: {} (device={})", model_name, device)

                    self._model = CrossEncoder(
                        model_name,
                        device=device,
                        max_length=512,
                    )
        return self._model

    @staticmethod
    def _sigmoid(x: float) -> float:
        """将 logits 归一化到 (0, 1) 区间。"""
        if x >= 0:
            z = math.exp(-x)
            return 1.0 / (1.0 + z)
        z = math.exp(x)
        return z / (1.0 + z)

    def rerank(self, query: str, results: list[dict], top_n: int = 5) -> list[dict]:
        """对检索结果进行 Cross-Encoder 重排。

        Args:
            query: 查询文本
            results: RRF 融合后的结果列表
            top_n: 返回 Top-N

        Returns:
            list[dict]: 重排后的 Top-N 结果，附加 rerank_score 字段
        """
        if not results:
            return []
        if len(results) <= top_n:
            return results

        model = self._ensure_model()
        pairs = [
            [query, r.get("document", "") or r.get("metadata", {}).get("title", "")]
            for r in results
        ]
        raw_scores = model.predict(pairs)
        # CrossEncoder.predict 返回 numpy array；归一化为 (0,1) 概率
        scores = [self._sigmoid(float(s)) for s in raw_scores]

        scored = list(zip(results, scores))
        scored.sort(key=lambda x: -x[1])
        logger.info(
            "[Reranker] 重排完成: {} 条 → Top-{}, 最高分={:.4f}, 最低分={:.4f}",
            len(results), top_n, scored[0][1], scored[-1][1],
        )
        return [{**r, "rerank_score": round(s, 4)} for r, s in scored[:top_n]]


reranker = CrossEncoderReranker()
