"""需求模式库（Requirement Pattern Library）。

存储同类需求的抽象维度集合，用于 ClarifyAgent 的遗漏检测。
对应 docs/01-requirement-parsing.md §6。

职责边界（§6.0）：
- **需求模式库**（本模块）：同类需求"应考虑哪些方面"（需求完整性）
  → ClarifyAgent 遗漏检测使用
- **测试模式库**（doc-03 §7.1）：已知测试用例骨架模式（测试策略）
  → GenAgent 用例生成使用

两者物理独立，通过 module/pattern_id 关联但互不包含内容。
"""

from __future__ import annotations

from typing import Any

from app.utils.logger import logger


# ============================================================================
# 预定义需求模式（docs/01 §6.1）
# ============================================================================

REQUIREMENT_PATTERNS: dict[str, dict[str, Any]] = {
    "auth": {
        "name": "认证授权类",
        "modules": ["auth", "login", "register", "sso", "oauth", "session"],
        "keywords": ["登录", "注册", "认证", "授权", "密码", "token", "session", "sso", "单点登录"],
        "required_dimensions": [
            "正常登录/注册流程",
            "异常处理（错误密码、账号不存在）",
            "密码重试锁定机制",
            "会话管理（Token/Session）",
            "密码加密存储",
        ],
        "common_boundary": ["用户名长度边界", "密码长度边界", "SQL注入防护"],
        "risk_points": ["暴力破解", "会话固定攻击", "密码泄露"],
    },
    "crud": {
        "name": "CRUD 操作类",
        "modules": ["user", "order", "product", "article", "comment", "resource"],
        "keywords": ["增删改查", "创建", "查询", "更新", "删除", "列表", "分页", "CRUD"],
        "required_dimensions": [
            "创建/查询/更新/删除流程",
            "数据校验规则",
            "权限控制",
            "分页/排序",
        ],
        "common_boundary": ["字段长度边界", "空值/null处理", "并发更新冲突"],
        "risk_points": ["越权操作", "数据丢失", "并发冲突"],
    },
    "payment": {
        "name": "支付交易类",
        "modules": ["payment", "transaction", "refund", "order", "billing"],
        "keywords": ["支付", "退款", "交易", "金额", "订单", "对账", "幂等"],
        "required_dimensions": [
            "支付/退款流程",
            "支付状态机",
            "金额计算规则",
            "交易安全（签名/加密）",
            "幂等性保证",
            "对账机制",
        ],
        "common_boundary": ["金额边界（0/最小/最大）", "精度边界", "并发支付", "超时处理"],
        "risk_points": ["重复支付", "金额篡改", "退款超时", "对账不一致"],
    },
}


# ============================================================================
# 需求模式匹配器（docs/01 §6.2）
# ============================================================================

class RequirementPatternMatcher:
    """根据 module 和语义匹配需求模式。

    对应 docs/01-requirement-parsing.md §6.2 的 RequirementPatternMatcher。

    匹配策略：
        1. module 精确匹配（优先）
        2. 标题/摘要关键词匹配（兜底）

    Usage:
        matcher = RequirementPatternMatcher()
        pattern = matcher.match_pattern(srr)
        if pattern:
            # pattern["required_dimensions"] 中缺失的维度 → 遗漏
            # pattern["common_boundary"] / ["risk_points"] → 注入 LLM 提示
    """

    def match_pattern(self, srr: dict) -> dict | None:
        """根据 module 和语义匹配需求模式。

        Args:
            srr: SRR JSON 字典

        Returns:
            dict | None: 匹配到的模式，含 pattern_id/name/required_dimensions/
                         common_boundary/risk_points；未匹配返回 None
        """
        req = srr.get("requirement", {}) or {}
        modules = [m.lower() for m in (req.get("modules") or [])]
        title = req.get("title", "") or ""
        summary = req.get("summary", "") or ""
        combined_text = f"{title} {summary}".lower()

        # 1. module 精确匹配
        for pattern_id, pattern in REQUIREMENT_PATTERNS.items():
            pattern_modules = [m.lower() for m in pattern["modules"]]
            if any(m in pattern_modules for m in modules):
                logger.debug("[PatternMatcher] module 匹配: pattern_id={}", pattern_id)
                return self._build_result(pattern_id, pattern)

        # 2. 关键词匹配（兜底）
        for pattern_id, pattern in REQUIREMENT_PATTERNS.items():
            keywords = [kw.lower() for kw in pattern["keywords"]]
            matched = [kw for kw in keywords if kw in combined_text]
            if len(matched) >= 2:
                logger.debug("[PatternMatcher] 关键词匹配: pattern_id={}, matched={}",
                             pattern_id, matched)
                return self._build_result(pattern_id, pattern)

        logger.debug("[PatternMatcher] 未匹配到需求模式")
        return None

    @staticmethod
    def _build_result(pattern_id: str, pattern: dict) -> dict:
        """构建匹配结果。"""
        return {
            "pattern_id": pattern_id,
            "name": pattern["name"],
            "required_dimensions": list(pattern["required_dimensions"]),
            "common_boundary": list(pattern["common_boundary"]),
            "risk_points": list(pattern["risk_points"]),
        }

    def detect_missing_dimensions(self, srr: dict, pattern: dict) -> list[str]:
        """检测当前 SRR 相对模式缺失的维度。

        通过将 required_dimensions 与 SRR 现有字段内容做语义比对，找出缺失维度。
        采用轻量级关键词匹配（非 LLM），作为 LLM 遗漏检测的规则补充。

        Args:
            srr: SRR JSON 字典
            pattern: match_pattern 返回的模式字典

        Returns:
            list[str]: 缺失的维度描述列表
        """
        srr_text = self._flatten_srr_text(srr)
        missing: list[str] = []
        for dim in pattern.get("required_dimensions", []):
            # 提取维度的核心关键词进行匹配
            dim_keywords = self._extract_dim_keywords(dim)
            if not any(kw in srr_text for kw in dim_keywords):
                missing.append(dim)
        return missing

    @staticmethod
    def _flatten_srr_text(srr: dict) -> str:
        """将 SRR 主要字段展平为小写文本，供关键词匹配。"""
        parts: list[str] = []
        req = srr.get("requirement", {}) or {}
        parts.append(req.get("title", ""))
        parts.append(req.get("summary", ""))
        # flows
        flows = srr.get("flows", {}) or {}
        hp = flows.get("happy_path", {}) or {}
        for step in (hp.get("steps") or []):
            if isinstance(step, dict):
                parts.append(step.get("action", ""))
                parts.append(step.get("expect", ""))
        for af in (flows.get("alternate_flows") or []):
            if isinstance(af, dict):
                parts.append(af.get("name", ""))
                parts.append(af.get("trigger", ""))
        # business_rules / data_constraints / boundary_conditions
        for key in ("business_rules", "data_constraints", "boundary_conditions",
                    "preconditions", "acceptance_criteria"):
            items = srr.get(key) or []
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        parts.append(str(item.get("rule", "")))
                        parts.append(str(item.get("field", "")))
                        parts.append(str(item.get("constraints", "")))
                        parts.append(str(item.get("condition", "")))
                        parts.append(str(item.get("description", "")))
                        parts.append(str(item.get("point", "")))
                    else:
                        parts.append(str(item))
        return " ".join(parts).lower()

    # 同义词映射表：维度关键词 → 实际文档中可能使用的同义表达
    SYNONYM_MAP: dict[str, list[str]] = {
        "加密存储": ["bcrypt", "哈希", "加盐", "加密", "hash", "md5", "sha", "明文"],
        "重试锁定": ["锁定", "失败", "重试", "次数", "禁登", "冻结"],
        "会话管理": ["session", "token", "会话", "登录态", "sessionid", "jwt"],
        "异常处理": ["异常", "错误", "失败", "不存在", "不匹配", "无效"],
        "权限控制": ["权限", "角色", "授权", "rbac", "admin", "访客"],
        "数据校验": ["校验", "验证", "必填", "格式", "长度", "正则"],
        "分页排序": ["分页", "page", "limit", "offset", "排序", "sort", "order"],
        "幂等性": ["幂等", "重复", "idempotent", "去重"],
        "状态机": ["状态", "state", "流转", "待支付", "已支付", "已完成", "已取消"],
        "金额计算": ["金额", "价格", "计算", "精度", "小数", "decimal", "分"],
        "签名加密": ["签名", "sign", "加密", "rsa", "aes", "密钥"],
        "对账": ["对账", "核对", "一致性", "差异"],
        "正常登录": ["登录", "login", "登录页", "登录表单", "登录成功"],
        "注册": ["注册", "register", "signup", "sign up"],
        "crud": ["创建", "查询", "更新", "删除", "增删改查", "列表"],
    }

    @staticmethod
    def _extract_dim_keywords(dim: str) -> list[str]:
        """从维度描述中提取核心关键词 + 同义词扩展。

        v2.13: 增加同义词映射，避免"密码加密存储"匹配不到"bcrypt"等误报。
        """
        suffixes = ["机制", "流程", "规则", "处理", "管理", "保证", "防护"]
        cleaned = dim
        for s in suffixes:
            if cleaned.endswith(s):
                cleaned = cleaned[: -len(s)]
                break
        # 取清理后的整体 + 原文
        keywords = [cleaned.lower(), dim.lower()]
        # 对于含括号的维度，提取括号内关键词
        if "（" in dim and "）" in dim:
            inner = dim[dim.find("（") + 1: dim.find("）")]
            for part in inner.split("、"):
                part = part.strip()
                if part:
                    keywords.append(part.lower())
        # v2.13: 同义词扩展——对 cleaned 中的每个片段查找同义词
        cleaned_lower = cleaned.lower()
        for syn_key, syn_values in RequirementPatternMatcher.SYNONYM_MAP.items():
            if syn_key in cleaned_lower:
                keywords.extend([s.lower() for s in syn_values])
        return keywords


# 全局单例
pattern_matcher = RequirementPatternMatcher()
