# -*- coding: utf-8 -*-
"""Bug C-1: Add PG trigger for auto-maintaining search_vec on INSERT/UPDATE."""
import sys

p = 'app/db/database.py'
with open(p, 'r', encoding='utf-8') as f:
    lines = f.read().split('\n')

# Find the BM25_SETUPS line and the end of the for loop block
start_idx = None
for i, line in enumerate(lines):
    if 'BM25_SETUPS: list[tuple[str, str, str]] = [' in line:
        start_idx = i
        break

if start_idx is None:
    print("ERROR: BM25_SETUPS line not found")
    sys.exit(2)

# Find the end of the for loop block: look for the line after the last
# "BM25 设置失败" warning that closes the for body
# We search forward for the next line that is at the same or lower indentation
# level as the "for table, vec_col, ts_expr in BM25_SETUPS:" line
for_idx = None
for i in range(start_idx, len(lines)):
    if 'for table, vec_col, ts_expr in BM25_SETUPS:' in lines[i]:
        for_idx = i
        break

if for_idx is None:
    print("ERROR: for loop line not found")
    sys.exit(2)

# The for loop indentation
for_indent = len(lines[for_idx]) - len(lines[for_idx].lstrip())

# Find the end: first line after for_idx that is non-empty and indented
# at <= for_indent (i.e., dedented back to or past the for level)
end_idx = None
for i in range(for_idx + 1, len(lines)):
    stripped = lines[i].strip()
    if stripped == '' or stripped.startswith('#'):
        continue
    indent = len(lines[i]) - len(lines[i].lstrip())
    if indent <= for_indent:
        end_idx = i
        break

if end_idx is None:
    print("ERROR: could not find end of for loop block")
    sys.exit(2)

print(f"Replacing lines {start_idx+1}..{end_idx} (0-indexed {start_idx}..{end_idx-1})")

new_block = '''            BM25_SETUPS: list[tuple[str, str, str, str]] = [
                ("requirements", "search_vec",
                 "title || ' ' || COALESCE(raw_text, '')",
                 "NEW.title || ' ' || COALESCE(NEW.raw_text, '')"),
                ("test_cases", "search_vec",
                 "title || ' ' || COALESCE(case_json::text, '')",
                 "NEW.title || ' ' || COALESCE(NEW.case_json::text, '')"),
            ]

            # 检测可用的中文分词配置：优先 chinese(zhparser)，回退 simple
            ts_config = "chinese"
            try:
                cfg_check = conn_sync.execute(
                    text("SELECT 1 FROM pg_ts_config WHERE cfgname = 'chinese'")
                ).fetchone()
                if not cfg_check:
                    logger.warning(
                        "DB 迁移: 'chinese' 文本搜索配置不存在（zhparser 扩展未安装），"
                        "BM25 将使用 'simple' 配置（中文分词效果有限，建议安装 zhparser）"
                    )
                    ts_config = "simple"
            except Exception:
                ts_config = "simple"

            for table, vec_col, ts_expr, ts_expr_new in BM25_SETUPS:
                if not inspector.has_table(table):
                    continue
                existing_cols = {c["name"] for c in inspector.get_columns(table)}
                if vec_col not in existing_cols:
                    # 使用 savepoint 隔离失败，避免污染外层事务导致 init_db 整体失败
                    try:
                        sp = conn_sync.begin_nested()
                        conn_sync.execute(text(
                            f"ALTER TABLE {table} ADD COLUMN {vec_col} tsvector"
                        ))
                        conn_sync.execute(text(
                            f"UPDATE {table} SET {vec_col} = to_tsvector('{ts_config}', {ts_expr})"
                        ))
                        conn_sync.execute(text(
                            f"CREATE INDEX idx_{table}_{vec_col} ON {table} USING GIN({vec_col})"
                        ))
                        sp.commit()
                        logger.info("DB 迁移: {}.{} tsvector({}) + GIN 索引已创建", table, vec_col, ts_config)
                    except Exception as mig_e:
                        sp.rollback()
                        logger.warning("DB 迁移: {}.{} BM25 设置失败（已跳过，不影响启动）: {}",
                                       table, vec_col, str(mig_e)[:120])

                # v2.55 Bug C-1: 创建触发器，INSERT/UPDATE 时自动维护 search_vec
                # 之前仅有一次性 UPDATE，后续新增/修改的数据 search_vec 为 NULL，BM25 检索失效
                try:
                    sp_trig = conn_sync.begin_nested()
                    fn_name = f"search_vec_update_{table}"
                    trig_name = f"trg_{table}_search_vec"
                    conn_sync.execute(text(f"""
                        CREATE OR REPLACE FUNCTION {fn_name}() RETURNS trigger AS $$
                        BEGIN
                            NEW.{vec_col} := to_tsvector('{ts_config}', {ts_expr_new});
                            RETURN NEW;
                        END;
                        $$ LANGUAGE plpgsql
                    """))
                    conn_sync.execute(text(f"DROP TRIGGER IF EXISTS {trig_name} ON {table}"))
                    conn_sync.execute(text(f"""
                        CREATE TRIGGER {trig_name}
                        BEFORE INSERT OR UPDATE ON {table}
                        FOR EACH ROW EXECUTE FUNCTION {fn_name}()
                    """))
                    # 回填历史 NULL 行（触发器仅对后续 INSERT/UPDATE 生效）
                    conn_sync.execute(text(
                        f"UPDATE {table} SET {vec_col} = to_tsvector('{ts_config}', {ts_expr}) "
                        f"WHERE {vec_col} IS NULL"
                    ))
                    sp_trig.commit()
                    logger.info("DB 迁移: {}.{} 触发器 {} 已创建（自动维护 search_vec）",
                                table, vec_col, trig_name)
                except Exception as trig_e:
                    sp_trig.rollback()
                    logger.warning("DB 迁移: {}.{} 触发器创建失败（已跳过）: {}",
                                   table, vec_col, str(trig_e)[:120])'''

new_lines = lines[:start_idx] + new_block.split('\n') + lines[end_idx:]
with open(p, 'w', encoding='utf-8') as f:
    f.write('\n'.join(new_lines))

print(f"Bug C-1 applied: replaced lines {start_idx+1}..{end_idx} ({end_idx - start_idx} lines -> {len(new_block.split(chr(10)))} lines)")
