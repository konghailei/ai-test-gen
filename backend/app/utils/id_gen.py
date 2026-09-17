r"""ID 生成工具。

生成符合架构文档规范的唯一 ID：
- 需求 ID：REQ-YYYY-NNN（如 REQ-2024-001）
- 用例 ID：TC-XXXX-NNNN-NNNN（如 TC-AUTH-0001-0001，对应 doc-04 §1 正则 ^TC-[A-Z]+-\d{4}-\d{4}$）
- 批次 ID：BATCH-YYYYMMDD-NNNNN

设计依据：templates/FIELD_DEFINITIONS.md 的 ID 格式规范
"""

import time
import random
import threading
from datetime import datetime


# 模块级计数器，用于同一秒内生成多个 ID 时保持递增。
# 启动时赋予随机偏移量，避免进程重启后计数器归零与历史记录撞号
# （计数器方案无法跨进程持久化，随机起始使不同进程落在不同序号区间）。
_req_counter = random.randint(0, 9999)
_case_counter = random.randint(0, 9999)
_batch_counter = random.randint(0, 99999)
_task_counter = random.randint(0, 9999)

# 线程锁：保证计数器自增的原子性，避免 FastAPI 异步+线程池并发下产生重复 ID
_id_lock = threading.Lock()


def gen_requirement_id() -> str:
    """生成需求 ID（线程安全，带计数器防同毫秒碰撞）。

    Returns:
        str: 形如 REQ-{时间戳毫秒}-{4位序号} 的需求 ID

    Note:
        使用模块级计数器 + 线程锁，避免 FastAPI 异步+线程池并发下同毫秒碰撞。
        相比纯随机方案（同毫秒 1/900 碰撞率），计数器方案在同毫秒内严格递增。
    """
    global _req_counter
    with _id_lock:
        _req_counter += 1
        seq = _req_counter % 10000
    ms = int(time.time() * 1000)
    return f"REQ-{ms}-{seq:04d}"


def gen_test_case_id(module_prefix: str = "GEN") -> str:
    r"""生成测试用例 ID。

    Args:
        module_prefix: 模块前缀（大写字母，如 AUTH / CRUD / PAY），默认 GEN

    Returns:
        str: 形如 TC-AUTH-0001-0001 的用例 ID

    Format:
        TC-{模块前缀}-{4位批次号}-{4位序号}

    Note:
        严格符合 doc-04 §1 的正则 ^TC-[A-Z]+-\d{4}-\d{4}$

    v3.1.3 修复 L2（用例 ID 碰撞）：
        原实现 batch_num = int(time.time()) % 10000，时间每 10000 秒（≈2.78h）一轮回，
        叠加进程重启后 _case_counter 随机起点，跨批次易与库内历史 ID 撞号，触发
        UniqueViolation 导致用例丢失。现改为“每 100 秒一个时间分桶”的 batch_num，
        轮回期从 2.78h 拉长到约 11.58 天；同批内 seq 保持严格递增，显著降低碰撞率，
        同时维持 TC-XXX-\d{4}-\d{4} 格式不变。
    """
    global _case_counter
    with _id_lock:
        _case_counter += 1
        seq = _case_counter % 10000
        # 每 100 秒一个分桶：time//100 变化周期 ≥1，10^6 秒轮回（≈11.58 天）
        bucket = (int(time.time()) // 100) % 10000
    # 模块前缀转大写，移除非字母字符
    prefix = "".join(c for c in module_prefix.upper() if c.isalpha()) or "GEN"
    return f"TC-{prefix}-{bucket:04d}-{seq:04d}"


def gen_batch_id() -> str:
    """生成批次 ID。

    Returns:
        str: 形如 BATCH-20240625-00001 的批次 ID

    Format:
        BATCH-{年月日}-{5位序号}
    """
    global _batch_counter
    with _id_lock:
        _batch_counter += 1
        # v3.4.1 修复 L-8：取模保证 5 位格式不被撑破。计数器初值为随机 0~99999 且不按日
        # 重置，原实现 seq 超过 99999 后 {seq:05d} 会输出 6 位，破坏 BATCH-YYYYMMDD-NNNNN。
        seq = _batch_counter % 100000
    date_str = datetime.now().strftime("%Y%m%d")
    return f"BATCH-{date_str}-{seq:05d}"


def gen_error_id() -> str:
    """生成错误案例 ID。

    Returns:
        str: 形如 ERR-20240625-AB12CD 的错误 ID
    """
    date_str = datetime.now().strftime("%Y%m%d")
    random_suffix = "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=6))
    return f"ERR-{date_str}-{random_suffix}"


def gen_task_id() -> str:
    """生成异步分析任务 ID（线程安全，带计数器防同毫秒碰撞）。

    Returns:
        str: 形如 TASK-{时间戳毫秒}-{4位序号} 的任务 ID
    """
    global _task_counter
    with _id_lock:
        _task_counter += 1
        seq = _task_counter % 10000
    ms = int(time.time() * 1000)
    return f"TASK-{ms}-{seq:04d}"
