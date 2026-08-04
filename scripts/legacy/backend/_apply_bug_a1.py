# -*- coding: utf-8 -*-
"""Apply Bug A-1 fix using line-based replacement (avoids quote-escaping issues)."""
import sys

p = 'app/api/requirements.py'
with open(p, 'r', encoding='utf-8') as f:
    lines = f.read().split('\n')

# Find the _save_fps_to_db signature line
start = None
for i, ln in enumerate(lines):
    if ln.startswith('async def _save_fps_to_db('):
        start = i
        break
if start is None:
    print("ERROR: _save_fps_to_db not found")
    sys.exit(2)

# Find the end of the function: next line at column 0 that starts with 'async ' or 'def ' or a top-level statement
# The function ends right before '_bump_req_revision' (async def)
end = None
for j in range(start + 1, len(lines)):
    if lines[j].startswith('async def _bump_req_revision'):
        end = j
        break
if end is None:
    print("ERROR: end of _save_fps_to_db not found")
    sys.exit(2)

# The function body is lines[start:end]; there's a trailing blank line at end-1
# We'll replace lines[start:end-1] (exclude the trailing blank line that separates functions)
# Actually keep the blank line. Replace [start:end] but preserve trailing blank.
# Find the last non-blank line of the function
func_end = end
while func_end > start and lines[func_end - 1].strip() == '':
    func_end -= 1

new_func = '''async def _save_fps_to_db(
    session: AsyncSession, req_id: str, fps: list[dict], priority: str = "P2",
    product_id: str = "",
) -> int:
    """将 ReqAgent 提取的功能点列表存入 functional_points 表。

    v2.55 修复 Bug A-1：解析 suggested_module（模块名称）-> Module.id（FK），
    写入 module_id + modules JSON 列，避免 FP 入库后模块信息丢失导致：
      - FP 复用候选查询命中 0 条
      - 依赖图谱模块筛选失效
      - FP 影响分析跳过模块级 BFS
      - 归档向量 modules 元数据为空
    每个 FP 的 sub_srr -> srr_json，suggested_module 解析后仍保留在 srr_json 中。
    返回实际入库的 FP 数量。
    """
    if not fps:
        return 0

    saved = 0
    # v2.48: 数据类型中文化兜底（防止 LLM 输出英文类型）
    from app.exporters.req_xmind import _data_type_cn

    # v2.55 Bug A-1：预加载该产品下的 Module 表，构建 name -> id 映射
    module_name_to_id: dict[str, str] = {}
    if product_id:
        try:
            from app.db.models import Module as _ModuleModel
            mod_rows = await session.execute(
                select(_ModuleModel.id, _ModuleModel.name).where(
                    _ModuleModel.product_id == product_id
                )
            )
            for mid, mname in mod_rows.all():
                if mname:
                    module_name_to_id[mname.strip()] = mid
                    module_name_to_id[mname.strip().lower()] = mid
        except Exception as mod_e:
            logger.warning(
                "[FP-AutoSave] 预加载 Module 映射失败（FP module_id 将留空）: product_id={}, err={}",
                product_id, str(mod_e)[:150],
            )

    def _resolve_module_id(suggested: str) -> str | None:
        """suggested_module 名称 -> Module.id，支持精确/忽略大小写匹配。"""
        if not suggested or not module_name_to_id:
            return None
        s = suggested.strip()
        if not s:
            return None
        if s in module_name_to_id:
            return module_name_to_id[s]
        return module_name_to_id.get(s.lower())

    for fp in fps:
        fp_id = gen_requirement_id()
        sub_srr = fp.get("sub_srr", {}) if isinstance(fp, dict) else {}
        # v2.48: 对 sub_srr 中的 data_constraints.type 做中文化兜底
        if isinstance(sub_srr, dict):
            for dc in sub_srr.get("data_constraints", []) or []:
                if isinstance(dc, dict) and dc.get("type"):
                    dc["type"] = _data_type_cn(str(dc["type"]))

        # v2.55 Bug A-1：解析 suggested_module -> module_id
        suggested_module = (
            fp.get("suggested_module", "") if isinstance(fp, dict) else ""
        )
        resolved_module_id = _resolve_module_id(suggested_module)
        modules_list: list[str] = [resolved_module_id] if resolved_module_id else []

        record = FunctionalPoint(
            id=fp_id,
            requirement_id=req_id,
            module_id=resolved_module_id,
            modules=modules_list,
            name=((fp.get("name") or "未命名功能点") if isinstance(fp, dict) else "未命名功能点")[:255],
            fp_type=(
                fp.get("fp_type", "workflow") if isinstance(fp, dict) else "workflow"
            ),
            priority=priority,
            srr_json={
                **sub_srr,
                "summary": sub_srr.get("summary") or (fp.get("summary", "") if isinstance(fp, dict) else ""),
                "suggested_module": suggested_module,
            },
            atomicity_check=(
                fp.get("atomicity_check", {}) if isinstance(fp, dict) else {}
            ),
            test_hints=[],
            status="pending_review",
        )
        session.add(record)
        saved += 1

    await session.flush()
    logger.info(
        "[FP-AutoSave] 需求 {} 自动存入 {} 个功能点 (product_id={}, module 映射表大小={})",
        req_id, saved, product_id, len(module_name_to_id),
    )
    return saved'''

new_lines = lines[:start] + new_func.split('\n') + lines[func_end:]
with open(p, 'w', encoding='utf-8') as f:
    f.write('\n'.join(new_lines))
print(f"Bug A-1 applied: replaced lines {start}..{func_end} ({func_end - start} lines -> {len(new_func.split(chr(10)))} lines)")
