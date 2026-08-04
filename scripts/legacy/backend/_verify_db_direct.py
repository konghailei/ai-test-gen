# -*- coding: utf-8 -*-
"""
直接连 Docker PG 做数据侧验证（绕过后端）
连接字符串：postgresql://aitestgen:aitestgen123@localhost:5432/ai_test_gen
"""
import sys
import asyncio
import asyncpg

DB_URL = "postgresql://aitestgen:aitestgen123@localhost:5432/ai_test_gen"

async def main():
    print("=" * 70)
    print("v2.55 Bug 修复 - 数据库侧直接验证")
    print("=" * 70)
    conn = await asyncpg.connect(DB_URL)
    try:
        # 1. functional_points module_id 填充率
        print("\n[1/6] functional_points.module_id 填充率")
        row = await conn.fetchrow("""
            SELECT
                COUNT(*) AS total,
                COUNT(module_id) AS with_module_id,
                COUNT(modules) AS with_modules_json,
                COUNT(*) - COUNT(module_id) AS missing_module_id
            FROM functional_points
        """)
        total = row['total']
        fill_rate = (row['with_module_id'] / total * 100) if total > 0 else 0
        print(f"  总数: {total}")
        print(f"  有 module_id: {row['with_module_id']}")
        print(f"  有 modules JSON: {row['with_modules_json']}")
        print(f"  缺失 module_id: {row['missing_module_id']}")
        print(f"  填充率: {fill_rate:.1f}%")
        if fill_rate > 80:
            print("  ✓ module_id 填充率良好")
        else:
            print("  ⚠ 填充率偏低（注意：A-1 修复仅对新入库的 FP 生效，历史数据需要单独回填脚本）")

        # 2. search_vec 触发器是否存在
        print("\n[2/6] search_vec 触发器检查")
        trg = await conn.fetch("""
            SELECT trigger_name, event_manipulation, event_object_table
            FROM information_schema.triggers
            WHERE trigger_name IN ('trg_requirements_search_vec', 'trg_test_cases_search_vec')
            ORDER BY trigger_name
        """)
        if trg:
            print(f"  已创建 {len(trg)}/2 个触发器:")
            for t in trg:
                print(f"    - {t['trigger_name']} ({t['event_object_table']} ON {t['event_manipulation']})")
        else:
            print("  ⚠ 触发器未创建！需要重启后端让 init_db 迁移生效")
        if len(trg) == 2:
            print("  ✓ 所有触发器已就绪（C-1 修复代码已包含，init_db 运行后会生效）")

        # 3. search_vec NULL 率
        print("\n[3/6] requirements.search_vec NULL 率")
        row3 = await conn.fetchrow("""
            SELECT
                COUNT(*) AS total,
                COUNT(search_vec) AS with_search_vec,
                COUNT(*) - COUNT(search_vec) AS null_count
            FROM requirements
        """)
        total3 = row3['total']
        null_rate = (row3['null_count'] / total3 * 100) if total3 > 0 else 0
        print(f"  总数: {total3}")
        print(f"  有 search_vec: {row3['with_search_vec']}")
        print(f"  NULL 数量: {row3['null_count']}")
        print(f"  NULL 率: {null_rate:.1f}%")
        if null_rate < 5:
            print("  ✓ search_vec NULL 率很低")
        else:
            print("  ⚠ NULL 率偏高，等触发器生效后 UPDATE 任何一行即可触发回填")

        # 4. fp_dependencies.is_active 分布
        print("\n[4/6] fp_dependencies.is_active 分布")
        row4 = await conn.fetchrow("""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE is_active = true) AS active,
                COUNT(*) FILTER (WHERE is_active = false) AS inactive
            FROM fp_dependencies
        """)
        print(f"  总数: {row4['total']}")
        print(f"  活跃 (true): {row4['active']}")
        print(f"  冻结/归档 (false): {row4['inactive']}")
        print("  ✓ is_active 字段可查询（B-1: include_inactive 默认=True 可见所有边）")

        # 5. module 表存在性（A-1 映射依赖）
        print("\n[5/6] module 表 & products 表检查")
        row5a = await conn.fetchrow("SELECT COUNT(*) AS cnt FROM modules")
        row5b = await conn.fetchrow("SELECT COUNT(*) AS cnt FROM products")
        print(f"  modules 记录数: {row5a['cnt']}（A-1: FP.suggested_module 名称映射到 Module.id）")
        print(f"  products 记录数: {row5b['cnt']}")

        # 6. 最近 FP 入库（验证新代码是否在容器中生效）
        print("\n[6/6] 最近 10 条 functional_points module_id 样例")
        rows6 = await conn.fetch("""
            SELECT id, name, module_id, modules, created_at
            FROM functional_points
            ORDER BY created_at DESC NULLS LAST
            LIMIT 10
        """)
        if not rows6:
            print("  （无记录）")
        else:
            filled = 0
            for r in rows6:
                mid = r['module_id'] or '(空)'
                mods = r['modules'] if r['modules'] else '(空)'
                flag = '✓' if r['module_id'] else ' '
                if r['module_id']:
                    filled += 1
                ts = str(r['created_at'])[:16] if r['created_at'] else 'NULL'
                name = (r['name'] or '')[:20]
                print(f"  {flag} [{ts}] {name:20s}  module_id={mid:14s}  modules={mods}")
            print(f"  最近10条中有 {filled}/10 已填充 module_id")
            if filled == 10:
                print("  ✓ 新入库 FP 已全部填充（容器代码包含 A-1 修复）")
            elif filled == 0:
                print("  ⚠ 最近10条全空！Docker 容器使用的是旧镜像，宿主机修复代码尚未重新 build + 重启")
            else:
                print("  ⚠ 部分填充，可能刚好是修复前后交界时间段")

    finally:
        await conn.close()

    print("\n" + "=" * 70)
    print("验证完成")
    print("=" * 70)
    print("\n注意事项:")
    print("  1. A-1/C-1/A-2 等修改仅在【宿主机】生效，Docker 容器运行的是旧镜像代码。")
    print("  2. 若要让修复生效，需要重新构建镜像并重启 ai-test-gen-api：")
    print("       docker-compose -f docker-compose.yml build api")
    print("       docker-compose -f docker-compose.yml up -d api celery")
    print("  3. 历史数据回填（functional_points.module_id / search_vec NULL）")
    print("     可以写一次性脚本，或者交给触发器处理（新增/修改时自动填充）")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"验证失败: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)
