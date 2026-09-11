# -*- coding: utf-8 -*-
"""全局状态/角色/阈值常量（v3.2.2 收敛魔法字符串）。

原先 "draft"/"approved"/"archived"/"admin" 等裸字符串在 api/agents 数十处硬编码，
拼写错误无法静态发现、状态机语义分散。统一收敛到这里，全仓引用。
"""
from __future__ import annotations


class ReqStatus:
    """需求状态机（analyzing → draft → pending_review → confirmed → archived）。"""
    ANALYZING = "analyzing"
    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    CONFIRMED = "confirmed"
    ACTIVE = "active"
    ARCHIVED = "archived"
    SUPERSEDED = "superseded"
    DEPRECATED = "deprecated"


class CaseStatus:
    """测试用例状态。"""
    DRAFT = "draft"
    REVIEWED = "reviewed"
    APPROVED = "approved"
    REJECTED = "rejected"


class CorrectionStatus:
    """知识纠错状态。"""
    NONE = "none"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class TaskStatus:
    """异步任务状态。"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    DONE = "done"
    FAILED = "failed"
    ERROR = "error"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class VectorizationStatus:
    """向量化状态。"""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    PENDING_RETRY = "pending_retry"


class UserRole:
    """用户角色。"""
    ADMIN = "admin"
    USER = "user"


class Confidence:
    """置信度语义常量。"""
    DEFAULT = 0.6          # 无匹配时的兜底
    AUTO_APPROVE_THRESHOLD = 0.70  # 自动入库（approved）门槛
    LOW = 0.2              # 低置信度兜底下限
    GEN_DEFAULT = 0.7      # gen_metadata.gen_confidence 兜底


class RetrievalThreshold:
    """知识注入检索阈值与限量（docs/14 一期）。

    统一原则（docs/14 §3 B2）：任何注入 prompt 的知识都必须过相似度阈值、
    有数量上限、可追溯。常量集中在此，便于按真实数据统一调整。

    ⚠️ 阈值来自本项目实测分布，**不是照搬业界 0.75**（docs/14 §6 明确禁止）。
    嵌入模型 bge-large-zh-v1.5 / COSINE / 真实 Milvus 数据。
    标定脚本：`backend/_probe_threshold_calib.py`（query 构造与生产完全一致：
    `" / ".join(modules) + " " + fp_name`），两种情形对照：

        情形                                     分布                 中位
        S 信号（本产品内检索）                    0.413 ~ 0.588        0.527
        N 无关（同 query 检索另一产品）            0.345 ~ 0.458        0.396

    → 分界带 0.413~0.458。取 0.50 作 Few-shot 下限：保留 65% 真实信号、
      误放无关 0%（实测）。注意本系统相似度整体压在 0.34~0.59 窄带内，
      **业界 0.75 会过滤掉 100% 的结果**，这也是 docs/14 §6 警告的实证。

    ⚠️ 标定教训（重要，勿重蹈）：**必须用生产真实 query 构造来标定**。
    若偷懒用 `requirement.title` 当 query，信号区间只有 0.485~0.531、
    噪声 0.280~0.402，会得出"0.45 已足够"的错误结论；而生产 query 因含
    模块名与功能点名，信息量更大，相似度整体上移约 0.10，阈值需相应提高。
    """

    # Few-shot 用例检索
    FEW_SHOT_TOP_K = 3              # 注入上限（沿用原 Top-3）
    FEW_SHOT_CANDIDATE_K = 15       # 放大候选量，供阈值过滤后仍有足够条数
    FEW_SHOT_MIN_SIMILARITY = 0.50  # 实测分界带 0.413~0.458 → 取 0.50 零误放

    # 测试模式检索
    PATTERN_TOP_K = 3               # 注入上限（沿用原 Top-3）
    PATTERN_CANDIDATE_K = 8         # 模式库仅 8 条种子，全量召回后过滤
    # 模式是"通用测试设计骨架"（crud/pagination/auth…），且模式库全局共享、
    # 无产品归属问题，误注入代价低于 Few-shot，故比 Few-shot 宽松一档。
    PATTERN_MIN_SIMILARITY = 0.45


# 澄清回环
MAX_CLARIFY_ITERATIONS = 3
