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


# 澄清回环
MAX_CLARIFY_ITERATIONS = 3
