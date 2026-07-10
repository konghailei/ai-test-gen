"""清理 ChromaDB 中的孤儿向量。

孤儿向量：ChromaDB 中存在但 SQLite 中已删除的需求/用例向量记录。
运行方式：cd backend && python ../scripts/cleanup_orphan_vectors.py
"""

import os
import sys
import sqlite3

# 确保能找到 backend 模块
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from app.db.vector_store import vector_store
from app.config import settings

def main():
    print("=" * 60)
    print("ChromaDB 孤儿向量清理工具")
    print("=" * 60)

    # 1. 获取向量库中所有需求 ID
    print("\n[1/4] 读取 ChromaDB 向量库...")
    vector_store._ensure_client()
    all_vectors = vector_store.get_all_requirements(limit=5000)
    vector_ids = {v["id"] for v in all_vectors}
    print(f"  向量库需求数: {len(vector_ids)}")

    # 2. 获取 SQLite 中所有需求 ID
    print("[2/4] 读取 SQLite 关系库...")
    db_path = settings.SQLITE_PATH
    if not os.path.exists(db_path):
        print(f"  ❌ 数据库文件不存在: {db_path}")
        return
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM requirements")
    sqlite_ids = {row[0] for row in cursor.fetchall()}
    conn.close()
    print(f"  SQLite 需求数: {len(sqlite_ids)}")

    # 3. 找出孤儿向量
    print("[3/4] 交叉比对...")
    orphans = vector_ids - sqlite_ids
    print(f"  孤儿向量: {len(orphans)} 条")
    
    if not orphans:
        print("\n✅ 没有孤儿向量，无须清理！")
        return

    # 列出孤儿详情
    print("\n  孤儿向量列表:")
    for v in sorted(all_vectors, key=lambda x: x["id"]):
        if v["id"] in orphans:
            meta = v.get("metadata", {})
            title = meta.get("title", "") or (v.get("document", "")[:60] if v.get("document") else "")
            print(f"    - {v['id']}  (标题: {title})")

    # 4. 确认后删除
    print(f"\n[4/4] 准备删除 {len(orphans)} 条孤儿向量...")
    confirm = input(f"  确认删除？(输入 yes 确认): ")
    if confirm.lower() != "yes":
        print("  ⏭️ 已取消")
        return

    deleted = 0
    failed = 0
    for req_id in orphans:
        try:
            vector_store.delete_requirement_from_kb(req_id)
            deleted += 1
            print(f"  ✅ 已删除: {req_id}")
        except Exception as e:
            failed += 1
            print(f"  ❌ 删除失败: {req_id} - {e}")

    print(f"\n{'=' * 60}")
    print(f"清理完成: 成功 {deleted} 条, 失败 {failed} 条")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
