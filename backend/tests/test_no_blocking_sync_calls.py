"""守卫：async 函数内禁止直调同步阻塞 API（v4.0.1 / S1 从根治理）。

## 为什么是"守卫"而不是"再补几个 to_thread"

S1 的成因不是某一个调用点写错，而是**机制脆弱**：`vector_store` 是同步实现
（Milvus gRPC + 本地 bge/CrossEncoder CPU 推理），依赖"每个调用点都记得用
`asyncio.to_thread` 包一层"。人一定会漏——`impact_agent` 就漏了，代价是
生成期间**所有** HTTP 端点超时。所以从根上解决 = 把"记得包"变成"不包就红"。

## 规则

在 `app/` 下任何 `async def` 函数体内：

1. 禁止对 `vector_store.*` / `reranker.*` 的**裸调用**（必须作为
   `asyncio.to_thread(...)` / `loop.run_in_executor(...)` 的实参）；
2. **禁止裸调"会在 async 函数内部定义的同步阻塞助手"**——这类嵌套 def 是
   项目既有惯例（如 `_existing_ids` / `_scan_sync` / `_list_vector_ids`），
   它们**必须**由调用方 `to_thread` 包起来执行；
3. 扫描不进入嵌套 `def` 体（嵌套 def 的执行由调用方负责，见第 2 条）。

## 已知边界（有意不覆盖）

- 同步 `def` 内部的调用不做检查（其调用方负责隔离，且规则 2 已覆盖
  "从 async 函数直调同步助手"这一实际风险路径）；
- 线程池语义上的 CPU 争抢（GIL 热点）不在本守卫范围——那由
  `app/core/loop_watchdog.py` 的运行时栈采样负责定位。

## 自验

`test_guard_detects_violation` 等用例用**合成源码**验证守卫本身有效，
避免"守卫悄悄失效却仍然全绿"。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # 支持 python tests/xxx.py 直跑

APP_DIR = Path(__file__).resolve().parent.parent / "app"

# 同步阻塞 API 的对象名：这些对象的方法**全部**是同步实现
FORBIDDEN_BASES = {"vector_store", "reranker"}
# 显式列入的模块级同步助手（S1 的典型成因就是漏包它）
FORBIDDEN_NAMES = {"_dispatch_embedding_task"}
# 视为"已隔离"的包装调用
WRAPPER_ATTRS = {"to_thread", "run_in_executor"}

_NESTED_DEF = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _iter_no_nested_defs(node: ast.AST):
    """遍历节点，但**不进入**嵌套的函数/类定义体。

    注意：若传入的节点本身就是一个定义（`stmt` 为嵌套 `def`），必须直接返回——
    否则会连它的 `arguments` / 函数体一起遍历，把"由调用方 to_thread 执行的
    同步助手"误判为裸调（守卫自身的第一版就踩了这个坑，见自验用例）。
    """
    if isinstance(node, _NESTED_DEF):
        return
    yield node
    for child in ast.iter_child_nodes(node):
        yield from _iter_no_nested_defs(child)


def _wrapped_ids(tree: ast.AST) -> set[int]:
    """收集作为 to_thread / run_in_executor 实参出现的节点 id（视为已隔离）。"""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in WRAPPER_ATTRS:
                for arg in node.args:
                    for sub in ast.walk(arg):
                        ids.add(id(sub))
    return ids


def _forbidden_target(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        base = func.value
        if isinstance(base, ast.Name) and base.id in FORBIDDEN_BASES:
            return f"{base.id}.{func.attr}"
        # self.vector_store.xxx(...)
        if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
            if base.value.id in FORBIDDEN_BASES:
                return f"{base.value.id}.{func.attr}"
        return None
    if isinstance(func, ast.Name) and func.id in FORBIDDEN_NAMES:
        return func.id
    return None


def _body_has_blocking(nodes: list[ast.stmt]) -> bool:
    """判断一段语句体内是否含同步阻塞调用（用于识别"嵌套阻塞助手"）。"""
    for stmt in nodes:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call) and _forbidden_target(node):
                return True
    return False


def find_blocking_calls(source: str, filename: str = "<src>") -> list[tuple[int, str, str]]:
    """返回 [(行号, async 函数名, 被裸调的同步目标)]。"""
    tree = ast.parse(source, filename=filename)
    wrapped = _wrapped_ids(tree)
    findings: list[tuple[int, str, str]] = []

    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue

        # ① 找出"本 async 函数内定义的、且自身含同步阻塞调用"的嵌套助手
        blocking_helpers: set[str] = set()
        for nested in fn.body:
            for node in ast.walk(nested):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not fn:
                    if _body_has_blocking(node.body):
                        blocking_helpers.add(node.name)

        # ② 扫本函数体（不进嵌套 def），找出未隔离的裸调
        for stmt in fn.body:
            for node in _iter_no_nested_defs(stmt):
                if not isinstance(node, ast.Call) or id(node) in wrapped:
                    continue
                target = _forbidden_target(node)
                if target is None and isinstance(node.func, ast.Name) \
                        and node.func.id in blocking_helpers:
                    target = f"{node.func.id}()（本函数内定义的同步阻塞助手，需 to_thread 包裹）"
                if target:
                    findings.append((node.lineno, fn.name, target))
    return findings


def scan_app() -> list[tuple[str, int, str, str]]:
    """扫描 `app/` 全量源码，返回 [(文件, 行号, async 函数, 目标)]。"""
    out: list[tuple[str, int, str, str]] = []
    for py in sorted(APP_DIR.rglob("*.py")):
        try:
            src = py.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue
        for lineno, fn, target in find_blocking_calls(src, filename=str(py)):
            out.append((str(py.relative_to(APP_DIR.parent)), lineno, fn, target))
    return out


# ---------------------------------------------------------------------------
# 守卫本体
# ---------------------------------------------------------------------------

def test_no_unguarded_blocking_calls_in_app():
    """app/ 下不得存在"async 内裸调同步阻塞 API"——S1 的成因形态。"""
    found = scan_app()
    detail = "\n".join(f"  {f}:{ln}  async {fn}() -> {tgt}" for f, ln, fn, tgt in found)
    assert not found, (
        "检测到 async 函数内未隔离的同步阻塞调用（会占住事件循环，"
        f"导致生成期其它端点超时）：\n{detail}\n"
        "修复：改为 `await asyncio.to_thread(obj.method, ...)`；"
        "若整段是同步链路，收进一个同步函数后整体 to_thread。"
    )


# ---------------------------------------------------------------------------
# 守卫自验：合成源码必须被判对，避免"守卫失效却全绿"
# ---------------------------------------------------------------------------

_VIOLATION_DIRECT = """
import asyncio
from app.db.milvus_store import vector_store

async def bad():
    hits = vector_store.search_similar_requirements(query="x")
    return hits
"""

_COMPLIANT_TO_THREAD = """
import asyncio
from app.db.milvus_store import vector_store

async def good():
    hits = await asyncio.to_thread(vector_store.search_similar_requirements, "x")
    return hits
"""

_COMPLIANT_NESTED_HELPER_WRAPPED = """
import asyncio
from app.db.milvus_store import vector_store

async def good():
    def _q(ids):
        col = vector_store._get_collection("requirements")
        return col.query(expr="", output_fields=["id"])
    return await asyncio.to_thread(_q, ["a"])
"""

_VIOLATION_NESTED_HELPER_BARE = """
import asyncio
from app.db.milvus_store import vector_store

async def bad():
    def _q(ids):
        col = vector_store._get_collection("requirements")
        return col.query(expr="", output_fields=["id"])
    return _q(["a"])
"""

_COMPLIANT_PURE_NESTED_HELPER = """
import asyncio

async def good():
    def _fmt(x):
        return str(x).strip()
    return _fmt( 1 )
"""


def test_guard_detects_direct_call():
    found = find_blocking_calls(_VIOLATION_DIRECT)
    assert len(found) == 1 and "vector_store.search_similar_requirements" in found[0][2]


def test_guard_allows_to_thread():
    assert find_blocking_calls(_COMPLIANT_TO_THREAD) == []


def test_guard_allows_wrapped_nested_helper():
    """嵌套同步助手是项目惯例，由调用方 to_thread 执行 → 合规。"""
    assert find_blocking_calls(_COMPLIANT_NESTED_HELPER_WRAPPED) == []


def test_guard_detects_bare_nested_helper_call():
    """嵌套助手自身含同步阻塞调用却被裸调 → 必须抓到（最隐蔽的 S1 形态）。"""
    found = find_blocking_calls(_VIOLATION_NESTED_HELPER_BARE)
    assert len(found) == 1 and "_q" in found[0][2]


def test_guard_allows_pure_nested_helper():
    """纯计算的嵌套助手不受影响（避免守卫过度收紧）。"""
    assert find_blocking_calls(_COMPLIANT_PURE_NESTED_HELPER) == []


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
