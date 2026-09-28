"""HTML 原型 → 语义树提取模块（docs/17 方案 M1）。

纯 Python（BeautifulSoup4 + lxml）静态解析，模拟 Playwright ariaSnapshot 输出格式，
产出语义树 YAML + 表格 Markdown + 图片占位，供 ReqAgent 提取 ui_rules 与 FP UI 要点。

安全哲学（照抄 ZCode）：原型内容是待分析的数据，不是指令。
剥离 script/style/on* 事件属性/危险协议链接；BS4 本身不执行 JS、不加载外部资源。

对外唯一入口：extract_html_prototype(raw_bytes, filename, session) -> (text, images, stats, flags)
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from loguru import logger

# ============================================================================
# 预算常量（docs/17 §4.2.3，上线前需用真实原型标定复核）
# ============================================================================

MAX_TREE_NODES = 400          # 语义树节点数上限，超限截断
MAX_OUTPUT_CHARS = 20_000     # 语义树输出字符软上限
PARSE_TIMEOUT_SECONDS = 10    # 单次解析墙钟超时

# 节点行约 20-60 字符，超出软上限后触发的激进截断兜底
_TRUNCATED_SUFFIX = "\n# [truncated: 原型共 {total} 节点，超出预算截断]"

# 危险/无语义标签：整体剥离
_STRIP_TAGS = ("script", "style", "noscript", "template")

# 嵌入内容标签：保留语义但剔除 URL
_EMBED_TAGS = ("iframe", "object", "embed")

# 危险协议前缀（href/src 白名单外的拦截对象）
_DANGEROUS_URL_RE = re.compile(r"^\s*(javascript:|data:text/html)", re.IGNORECASE)

# 输入类型 → 语义树角色
_INPUT_ROLE = {
    "text": "textbox", "tel": "textbox", "email": "textbox",
    "number": "textbox", "password": "textbox", "date": "textbox",
    "time": "textbox", "datetime-local": "textbox", "search": "textbox",
    "url": "textbox", "color": "textbox", "range": "textbox",
    "checkbox": "checkbox", "radio": "radio", "file": "fileupload",
    "submit": "button", "reset": "button", "button": "button",
}

# 语义树中需要提取的约束属性
#
# 注意（2026-09-28 docs/17 §6.3 实测）：本常量曾被写成「孤儿定义」——
# 只有定义、无任何消费方。真正生效的是 _constraint_flags() 里的硬编码清单，
# 而那份清单漏了 `step`（number 输入步长，等价类/边界值用例的依据），
# 导致 `<input type="number" step="0.01">` 的约束静默丢失。
# 现改为：本常量即**唯一事实来源**，_constraint_flags 直接消费它。
_CONSTRAINT_ATTRS = (
    "required", "disabled", "readonly", "checked", "multiple",
    "maxlength", "minlength", "pattern", "min", "max", "step",
    "type", "placeholder", "accept",
)

# 布尔型约束属性（存在性即语义，渲染为 [attr]）
_BOOLEAN_CONSTRAINT_ATTRS = ("required", "readonly", "disabled", "multiple")

# 键值型约束属性（渲染为 [attr=value]）
_VALUE_CONSTRAINT_ATTRS = (
    "maxlength", "minlength", "pattern", "min", "max", "step",
)

# 需要标注 [type=...] 的输入类型（对测试有区分意义）
_TYPE_FLAG_VALUES = ("password", "email", "tel", "number", "date")

# 优先属性取名路径
_ARIA_LABEL_ATTRS = ("aria-label", "title")

# 非文本节点类型：参与 `find_all(string=True)` 但不代表容器「自带文本」。
# Comment 是 NavigableString 子类，Axure/墨刀 导出件几乎每层容器都带注释，
# 若不排除会把匿名容器误判为具名容器（详见 _is_anonymous_container 注释）。
def _init_non_text_node_types() -> tuple[type, ...]:
    """惰性导入 bs4 的非文本节点类型（Comment/CData/Declaration 等）。

    bs4 未安装时返回空元组——解析入口本就会因 ImportError 明确报错，
    此处不应提前抛错影响模块导入（对齐降级哲学）。
    """
    try:
        from bs4 import Comment, CData, Declaration, Doctype, ProcessingInstruction
    except Exception:  # noqa: BLE001
        return ()
    return (Comment, CData, Declaration, Doctype, ProcessingInstruction)


_NON_TEXT_NODE_TYPES: tuple[type, ...] = _init_non_text_node_types()


def _has_data_attr(el: Any) -> bool:
    """容器是否带 data-* 属性（方案 §4.2.4 的保留例外）。

    原实现写作 `el.get("data-*")`——这是**字面 key**（HTML 中并不存在名为
    "data-*" 的属性），恒返回 None，导致该保留例外成为死代码。
    正确做法是按属性名前缀匹配。
    """
    try:
        return any(str(k).lower().startswith("data-") for k in el.attrs)
    except Exception:  # noqa: BLE001
        return False



class _ProtoStats:
    """解析统计（随任务 result 透出，供前端展示）。"""

    def __init__(self) -> None:
        self.nodes = 0
        self.fields = 0
        self.unnamed_fields = 0
        self.buttons = 0
        self.tables = 0
        self.images = 0
        self.links = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "nodes": self.nodes, "fields": self.fields,
            "unnamed_fields": self.unnamed_fields, "buttons": self.buttons,
            "tables": self.tables, "images": self.images, "links": self.links,
        }


def validate_html_content(raw_bytes: bytes) -> bool:
    """HTML 内容校验（无固定魔数，按特征标签判断）。

    前 512 字节小写化，包含任一 HTML 特征标签即通过；
    含 <div>/<script> 兼容 Axure/墨刀等工具导出的片段式原型。
    """
    head = raw_bytes[:512].lower()
    markers = (b"<!doctype html", b"<html", b"<head", b"<body", b"<div", b"<script")
    return any(marker in head for marker in markers)


def _decode_bytes(raw_bytes: bytes) -> str:
    """UTF-8 → GBK 兜底解码，与 md/txt 路径同策略。"""
    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return raw_bytes.decode("gbk", errors="replace")


def _clean_text(text: str | None) -> str:
    """压缩空白，返回适合放进语义树节点名的短文本。"""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


# ============================================================================
# 安全剥离
# ============================================================================


def _strip_unsafe(soup: Any) -> None:
    """BS4 解析后立即剥离：script/style/noscript/template、on* 属性、危险协议。"""
    for tag in soup.find_all(_STRIP_TAGS):
        tag.decompose()
    for el in soup.find_all(True):
        # on* 事件属性
        for attr in list(el.attrs):
            if attr.lower().startswith("on"):
                del el.attrs[attr]
        # href / src 危险协议：置为空并标 external（iframe 等由角色层处理）
        for url_attr in ("href", "src", "action"):
            val = el.attrs.get(url_attr)
            if isinstance(val, str) and _DANGEROUS_URL_RE.match(val):
                el.attrs[url_attr] = ""
                el.attrs["data-proto-external"] = "1"
    # form action 外链剔除
    for form in soup.find_all("form"):
        if form.get("action"):
            form.attrs["action"] = ""
            form.attrs["data-proto-external"] = "1"


# ============================================================================
# label 关联与取名（docs/17 §4.2.2 六级优先级）
# ============================================================================


class _LabelResolver:
    """为控件字段解析可读名称。"""

    def __init__(self, soup: Any) -> None:
        self._by_for: dict[str, str] = {}
        for label in soup.find_all("label"):
            label_for = label.get("for")
            if label_for:
                self._by_for[label_for] = _clean_text(label.get_text())
        self._dt_map: dict[int, str] = {}

    def resolve(self, el: Any) -> tuple[str, bool]:
        """返回 (名称, 是否未命名)。"""
        # 1. aria-label / aria-labelledby / title
        for attr in _ARIA_LABEL_ATTRS:
            val = _clean_text(el.get(attr))
            if val:
                return val, False
        labelledby = el.get("aria-labelledby")
        if labelledby:
            target = el.find_parent(id=labelledby) or None
            # aria-labelledby 指向任意元素，全局找
            if target is None:
                root = el
                while root.parent is not None:
                    root = root.parent
                try:
                    target = root.find(id=labelledby)
                except Exception:
                    target = None
            if target is not None:
                text = _clean_text(target.get_text())
                if text:
                    return text, False
        # 2. label[for=id] 精确关联
        el_id = el.get("id")
        if el_id and el_id in self._by_for:
            return self._by_for[el_id], False
        # 3. 嵌套 label 包裹
        parent_label = el.find_parent("label")
        if parent_label is not None:
            # 排除控件自身文本（input 无文本，button/select 有）
            text = _clean_text(parent_label.get_text())
            if text and text != _clean_text(el.get_text()):
                return text, False
        # 4. placeholder
        ph = _clean_text(el.get("placeholder"))
        if ph:
            return ph, False
        # 5. dt/dd 或表格 td 邻近文本（左邻优先）
        name = self._sibling_context_name(el)
        if name:
            return name, False
        return "(未命名字段)", True

    @staticmethod
    def _sibling_context_name(el: Any) -> str:
        # dt 内控件 → 取 dt 文本；dd 内控件 → 取前一个 dt 文本
        holder = el.find_parent("dt") or el.find_parent("dd")
        if holder is not None:
            if holder.name == "dt":
                text = _clean_text(holder.get_text())
                if text:
                    return text
            prev_dt = holder.find_previous_sibling("dt")
            if prev_dt is not None:
                text = _clean_text(prev_dt.get_text())
                if text:
                    return text
            if holder.name == "dt":
                next_dd = holder.find_next_sibling("dd")
                if next_dd is not None:
                    inner = next_dd.find(True)
                    if inner is not None and inner.name in ("input", "select", "textarea"):
                        return ""
        # 表格单元格内：左邻 td 优先
        cell = el.find_parent("td")
        if cell is not None:
            prev = cell.find_previous_sibling("td")
            if prev is not None:
                text = _clean_text(prev.get_text())
                if text:
                    return text
        return ""


# ============================================================================
# 语义树构建
# ============================================================================


class _TreeBuilder:
    """BS4 树 → ariaSnapshot 风格语义树。"""

    def __init__(self, resolver: _LabelResolver, stats: _ProtoStats) -> None:
        self.resolver = resolver
        self.stats = stats
        self.lines: list[str] = []
        self.total_nodes = 0

    # ---- 主入口 ----

    def build(self, root: Any) -> list[str]:
        children = [c for c in root.children if getattr(c, "name", None)]
        for child in children:
            self._emit_flattened(child, depth=0)
        return self.lines

    # ---- 归一化三规则：匿名容器压平（自顶向下透传 depth）----

    def _is_anonymous_container(self, el: Any) -> bool:
        """无名、无角色、无语义属性的布局容器 → 压平（children 上提）。"""
        if el.name in ("div", "span", "main", "section", "header", "footer", "nav", "article"):
            if any(el.get(a) for a in _ARIA_LABEL_ATTRS) or el.get("role") or _has_data_attr(el):
                return False
            if el.get("class") and _has_semantic_class(el):
                return False
            # 有直接文本内容则视为有名。
            # 必须排除 Comment 等非文本节点——BS4 的 Comment 是 NavigableString
            # 子类，会被 `find_all(string=True)` 捞出，从而被误当「容器自带文本」。
            # 反例（2026-09-28 docs/17 §6.3 实测）：真实 Axure/墨刀 导出件几乎每层
            # 容器都带注释（`<!-- 容器 1 -->`），原实现据此把匿名容器判为「具名」
            # → 不压平 → 每层输出一个含**整个子树文本**的 generic 巨行且继续递归
            # → 30 层嵌套产生 30 行完全相同的巨行，把真实字段行挤到树底、
            # 吃掉 MAX_OUTPUT_CHARS 字符预算（噪声挤掉信号）。
            own_text = "".join(
                t for t in el.find_all(string=True, recursive=False)
                if not isinstance(t, _NON_TEXT_NODE_TYPES)
            ) if hasattr(el, "find_all") else ""
            if _clean_text(own_text):
                return False
            return True
        return False

    def _emit_flattened(self, el: Any, depth: int) -> None:
        if self._is_anonymous_container(el):
            for child in el.children:
                if getattr(child, "name", None):
                    self._emit_flattened(child, depth)
            return
        self._emit(el, depth)

    # ---- 单节点发射 ----

    def _emit(self, el: Any, depth: int) -> None:
        self.total_nodes += 1
        line = self._render(el)
        if line is None:
            # 无语义节点（无名 img 等）：按 ZCode 规则丢弃，但 children 仍处理
            for child in el.children:
                if getattr(child, "name", None):
                    self._emit_flattened(child, depth)
            return
        self.lines.append("  " * depth + line)
        self.stats.nodes += 1
        # 容器型节点继续递归 children
        for child in el.children:
            if getattr(child, "name", None):
                self._emit_flattened(child, depth + 1)

    # ---- 角色渲染 ----

    def _render(self, el: Any) -> str | None:
        name = el.name
        if name in _EMBED_TAGS:
            self.stats.buttons += 0  # 嵌入内容不计按钮
            return '- generic "嵌入内容" [external]'
        if name == "form":
            return None  # form 本身无语义，children 上提
        if re.fullmatch(r"h[1-6]", name):
            text = _clean_text(el.get_text())
            if not text:
                return None
            return f'- heading "{text}" [level={name[1]}]'
        if name == "img":
            return self._render_img(el)
        if name == "input":
            return self._render_input(el)
        if name == "textarea":
            return self._render_field(el, "textbox", extra="[multiline]")
        if name == "select":
            return self._render_select(el)
        if name == "button":
            text = _clean_text(el.get_text())
            if not text:
                return None
            self.stats.buttons += 1
            return self._with_flags(f'button "{text}"', el, flag_names=("disabled",))
        if name == "a":
            href = el.get("href", "")
            text = _clean_text(el.get_text())
            if not href and not text:
                return None
            self.stats.links += 1
            label = text or href or "(链接)"
            flags = " [external]" if el.get("data-proto-external") else ""
            return f'- link "{label}"{flags}'
        if name == "label":
            return None  # label 文本由取名通道消费，避免与字段名重复输出
        if name == "table":
            return None  # 表格走 Markdown 通道，树中不重复输出
        if name in ("ul", "ol", "dl"):
            return None  # 列表容器压平，li/dt/dd 语义由 children 承载
        if name in ("li",):
            text = _clean_text(el.get_text())
            if not text:
                return None
            return f'- listitem "{text}"'
        if name == "dialog" or el.get("role") == "dialog":
            text = _clean_text(el.get_text())[:60]
            return f'- dialog "{text or "(对话框)"}"'
        if name in ("dt", "dd", "tr", "td", "th", "thead", "tbody", "option", "optgroup"):
            return None  # 表格/定义列表/下拉选项子元素由聚合通道处理
        # 其余标签（p、span 有文本等）→ generic 文本节点
        text = _clean_text(el.get_text())
        if not text:
            return None
        return f'- generic "{text[:80]}"'

    def _render_img(self, el: Any) -> str | None:
        alt = _clean_text(el.get("alt"))
        if not alt:
            # base64 图片走图片识别通道；无名普通图按 ZCode 规则丢弃
            return None
        self.stats.images += 1
        return f'- img "{alt}"'

    def _render_input(self, el: Any) -> str | None:
        itype = (el.get("type") or "text").lower()
        role = _INPUT_ROLE.get(itype, "textbox")
        if role == "checkbox":
            self.stats.fields += 1
            label, _unnamed = self.resolver.resolve(el)
            return self._with_flags(f'checkbox "{label}"', el,
                                    flag_names=("required", "disabled", "checked"))
        if role == "radio":
            # 同 name 组枚举全列
            group = el.get("name") or ""
            values: list[str] = []
            root = el
            while root.parent is not None:
                root = root.parent
            for peer in root.find_all("input", attrs={"type": "radio", "name": group}) if group else []:
                v = _clean_text(peer.get("value"))
                if v:
                    values.append(v)
            label, _unnamed = self.resolver.resolve(el)
            self.stats.fields += 1
            opts = ", ".join(f'"{v}"' for v in values) or '""'
            return self._with_flags(f'radio "{label}" [{opts}]', el,
                                    flag_names=("required", "disabled"))
        if role == "fileupload":
            self.stats.fields += 1
            label, _unnamed = self.resolver.resolve(el)
            flags: list[str] = []
            for attr in ("required", "disabled", "multiple"):
                if el.has_attr(attr):
                    flags.append(f"[{attr}]")
            if el.get("accept"):
                flags.append(f"[accept={el.get('accept')}]")
            return f'- fileupload "{label}"' + ("".join(f" {f}" for f in flags))
        if role == "button":
            text = _clean_text(el.get("value"))
            if not text:
                return None
            self.stats.buttons += 1
            return self._with_flags(f'button "{text}"', el, flag_names=("disabled",))
        # textbox
        return self._render_field(el, role)

    def _render_field(self, el: Any, role: str, extra: str = "") -> str | None:
        self.stats.fields += 1
        label, unnamed = self.resolver.resolve(el)
        if unnamed:
            self.stats.unnamed_fields += 1
        parts = [f'{role} "{label}"']
        if extra:
            parts.append(extra)
        flags = self._constraint_flags(el)
        if flags:
            parts.append(" ".join(flags))
        self._check_boundary_flags(el)
        return "- " + " ".join(parts)

    def _render_select(self, el: Any) -> str | None:
        self.stats.fields += 1
        label, unnamed = self.resolver.resolve(el)
        if unnamed:
            self.stats.unnamed_fields += 1
        options: list[str] = []
        for opt in el.find_all("option"):
            v = _clean_text(opt.get_text())
            if v:
                options.append(v)
        opts = ", ".join(f'"{v}"' for v in options)
        parts = [f'combobox "{label}"', f"[{opts}]" if opts else ""]
        if el.has_attr("multiple"):
            parts.append("[multiple]")
        line = "- " + " ".join(p for p in parts if p)
        self._check_boundary_flags(el)
        return line

    # ---- 属性 flag ----

    def _constraint_flags(self, el: Any) -> list[str]:
        """字段约束属性 → 语义树 flag（按 _CONSTRAINT_ATTRS 单一事实来源）。

        修复（2026-09-28 §6.3 实测）：原实现硬编码属性清单且漏了 `step`，
        同时 _CONSTRAINT_ATTRS 是无人消费的孤儿定义。现两处合一，
        由 _BOOLEAN/_VALUE_CONSTRAINT_ATTRS 驱动，杜绝再次漂移。
        """
        flags: list[str] = []
        for attr in _BOOLEAN_CONSTRAINT_ATTRS:
            if el.has_attr(attr):
                flags.append(f"[{attr}]")
        accept = el.get("accept")
        if accept:
            flags.append(f"[accept={accept}]")
        for attr in _VALUE_CONSTRAINT_ATTRS:
            val = el.get(attr)
            if val not in (None, ""):
                flags.append(f"[{attr}={val}]")
        itype = el.get("type")
        if itype and itype.lower() in _TYPE_FLAG_VALUES:
            flags.append(f"[type={itype.lower()}]")
        ph = _clean_text(el.get("placeholder"))
        if ph:
            flags.append(f'[placeholder={ph}]')
        return flags

    def _with_flags(self, base: str, el: Any, flag_names: tuple[str, ...]) -> str:
        flags: list[str] = []
        for attr in flag_names:
            if el.has_attr(attr):
                flags.append(f"[{attr}]")
        if el.get("data-proto-external"):
            flags.append("[external]")
        line = f"- {base}" + ("".join(f" {f}" for f in flags))
        return line

    def _check_boundary_flags(self, el: Any) -> None:
        # pattern/min/max 已在 _constraint_flags 输出，此处占位以对齐方案文档 §4.4.1
        return


def _has_semantic_class(el: Any) -> bool:
    """class 命中 UI 框架语义词典（V2 扩展点，MVP 保守返回 False）。"""
    return False


# ============================================================================
# 表格 → Markdown
# ============================================================================


def _html_table_to_markdown(table_el: Any) -> str:
    """HTML table → Markdown 表格（对齐 docx 路径 _table_to_markdown 产物）。"""
    rows_data: list[list[str]] = []
    for tr in table_el.find_all("tr"):
        cells = [_clean_text(c.get_text()).replace("|", "\\|") for c in tr.find_all(("th", "td"))]
        if cells:
            rows_data.append(cells)
    rows_data = [r for r in rows_data if any(c for c in r)]
    if not rows_data:
        return ""
    col_count = max(len(r) for r in rows_data)
    rows_data = [r + [""] * (col_count - len(r)) for r in rows_data]
    lines = []
    for i, row_cells in enumerate(rows_data):
        lines.append("| " + " | ".join(row_cells) + " |")
        if i == 0:
            lines.append("|" + "|".join(["---"] * col_count) + "|")
    return "\n".join(lines)


# ============================================================================
# base64 图片提取（复用多模态识别链路的输入结构）
# ============================================================================


def _extract_base64_images(soup: Any) -> tuple[list[tuple[str, str]], int]:
    """提取 data:image base64 图片 → [(名称, base64), ...]。返回 (列表, 剥离数量)。"""
    result: list[tuple[str, str]] = []
    for img in soup.find_all("img"):
        src = img.get("src") or ""
        if not src.startswith("data:image"):
            continue
        alt = _clean_text(img.get("alt")) or "原型内嵌图片"
        # data:image/png;base64,XXXX → XXXX
        _, _, payload = src.partition(",")
        if payload:
            result.append((alt, payload))
            img.decompose()  # 从树中移除，避免语义树输出超大节点
    return result, len(result)


# ============================================================================
# 主入口
# ============================================================================


def _parse_html_sync(raw_bytes: bytes) -> tuple[str, list[dict], dict[str, int], list[str]]:
    """同步解析实现（供 asyncio.to_thread 包装）。返回 (text, images, stats, flags)。"""
    flags: list[str] = []
    stats = _ProtoStats()
    try:
        from bs4 import BeautifulSoup
    except ImportError as e:
        raise ImportError(
            "beautifulsoup4 未安装，无法解析 HTML 原型（需走 base 镜像依赖链路）"
        ) from e

    try:
        html_text = _decode_bytes(raw_bytes)
        soup = BeautifulSoup(html_text, "lxml")
    except Exception as e:
        # BS4 解析异常 → 降级纯文本
        flags.append("proto_parse_failed")
        logger.warning("[html-proto] BS4 解析失败，降级纯文本: {}", str(e)[:150])
        fallback = re.sub(r"<[^>]+>", " ", _decode_bytes(raw_bytes))
        fallback = re.sub(r"\s+", " ", fallback).strip()
        return fallback, [], stats.to_dict(), flags

    # 安全剥离（解析器内部强制执行）
    _strip_unsafe(soup)

    # base64 图片先提取（复用 docx 多模态链路的输入结构）
    b64_images, _img_n = _extract_base64_images(soup)

    resolver = _LabelResolver(soup)
    builder = _TreeBuilder(resolver, stats)
    tree_lines = builder.build(soup.body or soup)

    total = builder.total_nodes
    if total > MAX_TREE_NODES:
        tree_lines = tree_lines[:MAX_TREE_NODES]
        tree_lines.append(f"# [truncated: 原型共 {total} 节点，超出预算截断]")
        stats.nodes = MAX_TREE_NODES
        flags.append("proto_tree_truncated")

    tree_text = "\n".join(tree_lines)
    if len(tree_text) > MAX_OUTPUT_CHARS:
        tree_text = tree_text[:MAX_OUTPUT_CHARS] + "\n# [truncated: 输出超出字符软上限]"
        if "proto_tree_truncated" not in flags:
            flags.append("proto_tree_truncated")

    # 表格 Markdown（按文档顺序）
    tables_md: list[str] = []
    for table_el in soup.find_all("table"):
        md = _html_table_to_markdown(table_el)
        if md:
            stats.tables += 1
            tables_md.append(md)

    if not tree_lines and not tables_md and not b64_images:
        flags.append("proto_no_semantics")

    # 组装产出
    sections: list[str] = []
    if tree_lines:
        sections.append("┌─ 页面结构（语义树）────────────\n" + tree_text)
    if tables_md:
        sections.append("┌─ 表格数据 ────────────────────\n" + "\n\n".join(tables_md))
    images: list[dict] = []
    if b64_images:
        # 视觉 LLM 识别由调用方（对齐 _describe_docx_image 链路）处理；
        # 本模块先落占位，raw_images 按现有结构返回
        for i, (alt, payload) in enumerate(b64_images, 1):
            images.append({
                "index": i,
                "data_url": f"data:image/png;base64,{payload}",
                "content_type": "image/png",
            })
        sections.append(
            "┌─ 图片识别 ────────────────────\n"
            + "\n".join(f"[图{i}] {alt}（未配置多模态 LLM 时人工补充）"
                        for i, (alt, _p) in enumerate(b64_images, 1))
        )
    if stats.unnamed_fields:
        sections.append(f"# 解析提示：共 {stats.unnamed_fields} 个未命名字段，建议澄清补充业务含义")

    text = "\n\n".join(sections)
    return text, images, stats.to_dict(), flags


async def extract_html_prototype(
    raw_bytes: bytes,
    filename: str = "",
    session=None,
) -> tuple[str, list[dict], dict[str, int], list[str]]:
    """HTML 原型 → (语义文本, 图片列表, 统计, 降级标记)。

    返回的语义文本 = 语义树 YAML + 表格 Markdown + 图片占位 + 未命名字段提示
    图片列表与 _extract_docx_text 返回结构一致（复用多模态识别链路）
    stats/flags 供调用方写入任务 result_json（degraded_flags 与统计透出）

    Raises:
        ImportError: beautifulsoup4 未安装（调用方需 500 明确报错，对齐 docx 先例）
    """
    try:
        text, images, stats, flags = await asyncio.wait_for(
            asyncio.to_thread(_parse_html_sync, raw_bytes),
            timeout=PARSE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("[html-proto] 解析超时 {}s，降级纯文本: filename={}",
                       PARSE_TIMEOUT_SECONDS, filename)
        flags = ["proto_parse_timeout"]
        fallback = re.sub(r"<[^>]+>", " ", _decode_bytes(raw_bytes))
        fallback = re.sub(r"\s+", " ", fallback).strip()[:MAX_OUTPUT_CHARS]
        stats = {"nodes": 0, "fields": 0, "unnamed_fields": 0,
                 "buttons": 0, "tables": 0, "images": 0, "links": 0}
        text, images = fallback, []
    logger.info(
        "[html-proto] 解析完成: filename={}, text_len={}, stats={}, flags={}",
        filename, len(text), stats, flags,
    )
    return text, images, stats, flags


# ============================================================================
# 双接入点共享层（docs/17 §3.1 对称性契约）
#
# 历史教训：docx 时代两套解析逻辑曾出 3 个 Bug（见 _extract_docx_text v2.18 修复记录），
# 根因是「同一件事在两条分支各写一遍」。HTML 原型必须由构造保证对称——
# 两个入口只做分发，标记与文本组装一律经此函数，杜绝任一处单独漂移。
# ============================================================================

# 其它文档类型（md/docx/txt）的来源标记，保持与原实现逐字一致
_OTHER_MARKER = "【文档：{filename}】"


def build_source_marker(ext: str, filename: str) -> str:
    """来源边界标记（docs/17 §4.3）。

    原型类来源用独立「【原型：】」标记，供 ClarifyAgent 区分来源类型
    （静态原型无法体现 JS 动态行为 → 追加原型专属澄清问题）。
    """
    if ext in ("html", "htm"):
        return f"【原型：{filename or '未命名'}】"
    return _OTHER_MARKER.format(filename=filename or "未命名")


async def build_html_segment(
    raw_bytes: bytes,
    filename: str,
    session=None,
) -> tuple[str, list[dict], dict[str, int], list[str]]:
    """HTML 原型 → 可直接拼进 combined_raw 的完整片段（两入口唯一入口）。

    返回 (segment, images, stats, flags)：
      segment = 「【原型：文件名】」+ 换行 + 语义文本
    两个接入点（api/requirements.py::_parse_uploaded_files 与
    core/requirement_tasks.py phase 0 循环）都必须调用本函数，
    保证同一字节产出逐字节一致的片段（对称性由构造保证，非靠人工同步）。
    """
    text, images, stats, flags = await extract_html_prototype(
        raw_bytes, filename=filename, session=session,
    )
    return f"{build_source_marker('html', filename)}\n{text}", images, stats, flags
