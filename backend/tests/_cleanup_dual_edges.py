"""存量对偶依赖边清理清单生成器（v3.5.7 P1-B，只出清单不自动删）。

扫描 fp_dependencies 表中「A depends_on B」与「B depends_on A」并存的
对偶组，输出复核清单（含两端 FP 名称、所属需求、创建时间、来源推测），
供人工确认后按清单删除。

用法：
  docker exec -i ai-test-gen-api python - < backend/tests/_cleanup_dual_edges.py            # 扫描输出清单
  docker exec -i ai-test-gen-api python - < backend/tests/_cleanup_dual_edges.py --apply 813 815 812 816 811 817   # 确认后按 ID 删除
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.db.database import async_session
from app.db.models import FPDependency, FunctionalPoint, Requirement


async def scan() -> list[tuple[int, int, list[int]]]:
    """返回对偶组列表：[(keep_id, drop_id, [pair_ids])]，drop 为建议删除项。"""
    async with async_session() as s:
        rows = (await s.execute(
            select(
                FPDependency.id, FPDependency.source_fp_id,
                FPDependency.target_fp_id, FPDependency.relation_type,
            ).where(
                FPDependency.relation_type == "depends_on",
                FPDependency.is_active == True,  # noqa: E712
            )
        )).all()

    # (src, tgt) → [ids]
    fwd: dict[tuple[str, str], list[int]] = {}
    for rid, src, tgt, _ in rows:
        fwd.setdefault((src, tgt), []).append(rid)

    seen: set[int] = set()
    groups: list[tuple[str, str, list[int], list[int]]] = []  # (src_name, tgt_name, fwd_ids, rev_ids)
    for (src, tgt), ids in fwd.items():
        rev_ids = fwd.get((tgt, src))
        if not rev_ids:
            continue
        # 保留 LLM 依赖数组方向（创建更早的一批），删除 confirm 补建的反向边
        group_ids = sorted(ids + rev_ids)
        if any(i in seen for i in group_ids):
            continue
        seen.update(group_ids)
        groups.append((src, tgt, ids, rev_ids))
    return groups


async def main() -> int:
    apply_ids: set[int] = set()
    if "--apply" in sys.argv:
        idx = sys.argv.index("--apply")
        apply_ids = {int(x) for x in sys.argv[idx + 1:] if x.isdigit()}

    groups = await scan()
    if not groups:
        print("未发现 depends_on 对偶边，数据干净。")
        return 0

    print(f"发现 {len(groups)} 组对偶边（每组分「保留(早创建)/建议删除(晚创建)」）：\n")
    all_drop_ids: list[int] = []
    async with async_session() as s:
        for src_id, tgt_id, fwd_ids, rev_ids in groups:
            src_fp = await s.get(FunctionalPoint, src_id)
            tgt_fp = await s.get(FunctionalPoint, tgt_id)
            src_req = await s.get(Requirement, src_fp.requirement_id) if src_fp else None
            tgt_req = await s.get(Requirement, tgt_fp.requirement_id) if tgt_fp else None
            # 早创建 = LLM 依赖数组（保留），晚创建 = confirm 补建 flow 反向（删）
            keep_ids = fwd_ids if min(fwd_ids) < min(rev_ids) else rev_ids
            drop_ids = rev_ids if keep_ids is fwd_ids else fwd_ids
            # 同需求内的对偶才建议自动删；跨需求对偶需人工甄别
            same_req = src_req and tgt_req and src_req.id == tgt_req.id
            all_drop_ids.extend(drop_ids if same_req else [])
            print(f"  「{src_fp.name if src_fp else src_id}」←→「{tgt_fp.name if tgt_fp else tgt_id}」"
                  f"（{src_req.title if src_req else '?'} / {tgt_req.title if tgt_req else '?'}）")
            print(f"    保留(早): {keep_ids}   建议删除(晚): {drop_ids}"
                  + ("" if same_req else "   ⚠ 跨需求对偶，请人工甄别"))
            print()

    if not apply_ids:
        print(f"建议删除的边 ID 汇总（同需求内共 {len(all_drop_ids)} 条）: {all_drop_ids}")
        print("\n复核后执行: docker exec -i ai-test-gen-api python - < 本脚本 --apply <ID...>")
        return 0

    # apply 模式：逐条删除并校验
    deleted = 0
    async with async_session() as s:
        for dep_id in sorted(apply_ids):
            dep = await s.get(FPDependency, dep_id)
            if not dep:
                print(f"  [跳过] 边 {dep_id} 不存在")
                continue
            await s.delete(dep)
            deleted += 1
            print(f"  [删除] 边 {dep_id}: {dep.source_fp_id} -{dep.relation_type}-> {dep.target_fp_id}")
        await s.commit()
    print(f"\n共删除 {deleted} 条边。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
