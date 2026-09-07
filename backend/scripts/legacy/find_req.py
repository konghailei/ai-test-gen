"""查询分析任务状态和所有 REQ-178599 开头的需求。"""
import asyncio
import os

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://aitestgen:aitestgen123@postgres:5432/ai_test_gen",
)

from sqlalchemy import text
from app.db.database import async_session


async def main():
    async with async_session() as s:
        # 1. 查询任务状态
        rows = (
            await s.execute(
                text(
                    "SELECT id, task_type, status, phase, progress, message, error, "
                    "created_at, updated_at "
                    "FROM analysis_tasks "
                    "WHERE id LIKE 'TASK-1786002%' "
                    "ORDER BY created_at DESC LIMIT 5"
                )
            )
        ).all()
        print("=== 最近分析任务 ===")
        for r in rows:
            print(f"\n任务: {r[0]}")
            print(f"  task_type={r[1]} status={r[2]} phase={r[3]} progress={r[4]}")
            print(f"  message={r[5]}")
            if r[6]:
                print(f"  error={str(r[6])[:300]}")
            print(f"  created={r[7]} updated={r[8]}")

        # 2. 查询所有 REQ-178599 开头的需求
        rows = (
            await s.execute(
                text(
                    "SELECT id, title, status, revision "
                    "FROM requirements WHERE id LIKE 'REQ-178599%' "
                    "ORDER BY id DESC LIMIT 10"
                )
            )
        ).all()
        print("\n=== REQ-178599 开头的需求 ===")
        if not rows:
            print("无")
        else:
            for r in rows:
                print(f"  {r[0]} | {(r[1] or '')[:50]} | status={r[2]} | rev={r[3]}")

        # 3. 查询所有分析中的需求（可能 regenerate 创建了新 ID）
        rows = (
            await s.execute(
                text(
                    "SELECT id, title, status, revision "
                    "FROM requirements WHERE status IN ('analyzing', 'draft') "
                    "ORDER BY updated_at DESC LIMIT 10"
                )
            )
        ).all()
        print("\n=== 分析中/草稿状态的需求 ===")
        if not rows:
            print("无")
        else:
            for r in rows:
                print(f"  {r[0]} | {(r[1] or '')[:50]} | status={r[2]} | rev={r[3]}")


if __name__ == "__main__":
    asyncio.run(main())
