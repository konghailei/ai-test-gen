"""去重专用嵌入模型的隔离性单测（docs/27 A，不加载真实模型/不连 DB）。

核心契约：
1. 空配置 → embed_texts_for_dedup 走全局模型（行为同现状）；
2. 专用模型加载失败 → 回退全局模型 + dedup_model_id 变 global-fallback；
3. 专用模型生效 → 全局 _embed **绝不被调用**（隔离性，防 KB 向量被小模型污染）；
4. 全局模型槽 _model 全程不被去重路径触碰。
"""
import pytest

from app.config import settings
from app.db.milvus_store import MilvusVectorStore


@pytest.fixture
def store():
    return MilvusVectorStore()


@pytest.fixture
def restore_dedup_cfg():
    origin = (settings.DEDUP_EMBEDDING_MODEL_NAME, settings.DEDUP_EMBEDDING_DEVICE)
    yield
    settings.DEDUP_EMBEDDING_MODEL_NAME, settings.DEDUP_EMBEDDING_DEVICE = origin


def _fake_model(tag="fake"):
    import numpy as np

    class _M:
        def encode(self, texts, normalize_embeddings=True, show_progress_bar=False):
            # 真实 SentenceTransformer.encode 返回 np.ndarray——fake 必须同构，
            # 否则测不出 .tolist() 契约（本次踩过：list 没有 tolist）。
            return np.array([[float(len(t)), 1.0] for t in texts], dtype=np.float32)

    _M.__name__ = tag
    return _M()


def test_empty_config_falls_back_to_global(store, monkeypatch, restore_dedup_cfg):
    """空配置 → 全局模型（默认部署零变化）。"""
    settings.DEDUP_EMBEDDING_MODEL_NAME = ""
    calls = {"global": 0}

    def _fake_global(ts):
        calls["global"] += 1
        return [[0.1, 0.2] for _ in ts]

    monkeypatch.setattr(store, "_embed", _fake_global)
    out = store.embed_texts_for_dedup(["a", "b"])
    assert calls["global"] == 1
    assert len(out) == 2
    assert store.dedup_model_id() == "global"


def test_dedup_model_used_and_global_never_touched(store, monkeypatch, restore_dedup_cfg):
    """专用模型生效 → 全局 _embed 绝不被调用（隔离性红线）。"""
    settings.DEDUP_EMBEDDING_MODEL_NAME = "/models/bge-small-zh-v1.5"
    monkeypatch.setattr(store, "_ensure_dedup_model", lambda: _fake_model())
    called = {"global": False}
    monkeypatch.setattr(store, "_embed", lambda ts: called.__setitem__("global", True) or [[9.9]])
    out = store.embed_texts_for_dedup(["hello", "world"])
    assert not called["global"], "去重路径绝不允许触碰全局 KB 模型"
    assert out == [[5.0, 1.0], [5.0, 1.0]]
    assert store.dedup_model_id() == "/models/bge-small-zh-v1.5"


def test_broken_dedup_model_falls_back_with_id_marker(store, monkeypatch, restore_dedup_cfg):
    """专用模型加载失败 → 回退全局 + model_id 标记（缓存 key 不错位）。"""
    settings.DEDUP_EMBEDDING_MODEL_NAME = "/models/not-exist"

    def _boom():
        raise FileNotFoundError("去重专用模型未就绪")

    monkeypatch.setattr(store, "_ensure_dedup_model", _boom)
    monkeypatch.setattr(store, "_embed", lambda ts: [[0.5]])
    out = store.embed_texts_for_dedup(["x"])
    assert out == [[0.5]]
    assert store.dedup_model_id() == "global-fallback"
    assert store._dedup_model_broken is True


def test_ensure_dedup_model_rejects_relative_path(store, monkeypatch, restore_dedup_cfg):
    """相对路径 → 明确报错（与全局 _ensure_model 同哲学，不静默联网下载）。"""
    settings.DEDUP_EMBEDDING_MODEL_NAME = "BAAI/bge-small-zh-v1.5"
    with pytest.raises(FileNotFoundError, match="绝对路径"):
        store._ensure_dedup_model()


def test_ensure_dedup_model_empty_returns_none(store, restore_dedup_cfg):
    settings.DEDUP_EMBEDDING_MODEL_NAME = ""
    assert store._ensure_dedup_model() is None


def test_dedup_and_global_slots_are_independent(store, monkeypatch, restore_dedup_cfg):
    """两个模型槽互不覆盖（C4A 隔离性的结构性断言）。

    直接给 _dedup_model 槽赋值（绕过 loader），验证去重路径只用专用槽、
    全局 _model 槽全程不被触碰——防"小模型污染 KB 向量"的回归红线。
    """
    settings.DEDUP_EMBEDDING_MODEL_NAME = "/models/bge-small-zh-v1.5"
    store._dedup_model = _fake_model("dedup")
    called = {"global": False}
    monkeypatch.setattr(store, "_embed", lambda ts: called.__setitem__("global", True) or [[9.9]])
    out = store.embed_texts_for_dedup(["abc"])
    assert not called["global"], "去重路径绝不允许触碰全局 KB 模型"
    assert out == [[3.0, 1.0]]
    assert store._model is None, "去重路径不得初始化全局模型槽"
    assert store._model_lock is not store._dedup_model_lock
    # 槽位内容互不串：清专用槽不影响全局槽（反之同理）
    store._dedup_model = None
    assert store._model is None
