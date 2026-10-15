"""配置管理模块。

集中管理所有配置项，支持通过环境变量或 .env 文件覆盖默认值。
配置项对应架构文档 docs/00-05 中的技术决策。

设计依据：
- LLM 配置：docs/05-technical-selection.md §2（DeepSeek 主力 + OpenAI 备用）
- 数据库：docs/05-technical-selection.md §11（PostgreSQL + Milvus）
- 知识库配置：docs/02-knowledge-base.md §2.5（知识库维护 + 检索配置）
- 质量门禁阈值：docs/01-requirement-parsing.md §5.2（冷启动 0.60 / 正常 0.75）
- 最大澄清轮次：docs/01-requirement-parsing.md §5.3（最多 3 轮）
- 限流配置：docs/05-technical-selection.md §11.2

安全说明：
- API Key 在数据库中以加密形式存储（Fernet 对称加密）
- 响应中 API Key 自动脱敏（仅显示前3后4位）
- 生产环境必须设置 APP_SECRET_KEY（用于 JWT 和加密）
"""

import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置。

    通过环境变量或 .env 文件加载，优先级：
    环境变量 > .env 文件 > 类属性默认值
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ========== 安全配置 ==========
    """应用密钥（用于 JWT 签名 + 加密盐值）

    安全策略：
    - 生产环境（DEBUG=False）必须通过环境变量设置，否则启动报错
    - 开发环境（DEBUG=True）允许使用默认值
    """
    APP_SECRET_KEY: str = ""

    """Fernet 主密钥（用于加密数据库中存储的 API Key）

    安全策略：
    - 不再硬编码默认密钥（旧版固定密钥已废弃）
    - 优先级：环境变量 > 密钥文件（自动生成）> 启动报错（生产环境）
    - crypto.py 负责实际加载逻辑，此处仅作占位
    """
    MASTER_KEY: str = ""

    """JWT 令牌过期时间（分钟）"""
    JWT_EXPIRE_MINUTES: int = 480  # 8 小时

    # ========== LLM 配置 ==========
    """LLM 提供商：deepseek（主力）或 openai（备用）"""
    LLM_PROVIDER: str = "deepseek"

    """DeepSeek API Key（用户从 .env 配置）"""
    DEEPSEEK_API_KEY: str = ""

    """DeepSeek API 基础地址（OpenAI 兼容接口）"""
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com/v1"

    """DeepSeek 默认模型"""
    DEEPSEEK_MODEL: str = "deepseek-chat"

    """OpenAI API Key（备用提供商）"""
    OPENAI_API_KEY: str = ""

    """OpenAI API 基础地址"""
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"

    """OpenAI 默认模型"""
    OPENAI_MODEL: str = "gpt-4o"

    # ========== 数据目录配置 ==========
    """数据目录路径（相对 backend 目录，存放日志、密钥等持久化文件）"""
    DATA_DIR: str = "./data"

    # ========== 向量存储（Milvus，系统唯一向量库后端） ==========
    """向量存储后端：milvus（ChromaDB 已弃用，统一使用 Milvus）"""
    VECTOR_STORE: str = "milvus"

    """Milvus 服务地址（仅 VECTOR_STORE=milvus 时生效）"""
    MILVUS_HOST: str = "localhost"

    """Milvus gRPC 端口"""
    MILVUS_PORT: int = 19530

    """Milvus Collection 名称前缀（按环境隔离）"""
    MILVUS_COLLECTION_PREFIX: str = "aitestgen"

    # ========== 数据库配置（PostgreSQL，系统唯一关系数据库后端） ==========
    """PostgreSQL 连接字符串（必填，格式：postgresql://user:pass@host:port/dbname）"""
    DATABASE_URL: str = ""
    # v3.4（docs/15 §4.2）：LangGraph 检查点后端。postgres = AsyncPostgresSaver（checkpoints
    # 落 PG，thread_id=task_id，进程重启后任务状态可审计/恢复）；memory = 单进程内存（老行为，
    # PG 不可用时的显式降级，日志与 degraded_flags 可见）。
    PIPELINE_CHECKPOINT_BACKEND: str = "postgres"

    """PG 连接池大小"""
    PG_POOL_SIZE: int = 10

    """PG 连接超时（秒）"""
    PG_TIMEOUT: int = 30

    """LLM 调用超时（秒），0 表示不限时"""
    LLM_TIMEOUT: int = 0

    # ========== LLM 首响应超时（v3.3 新增，N4 根治） ==========
    """流式调用的「首个有效响应」等待上限（秒）。

    本项目所有 LLM 调用均使用**流式响应**。判定规则：
    - 只要在此时长内收到**任意**响应即视为模型存活 —— 正文 chunk **或**
      思考/推理 chunk（`reasoning_content` / `reasoning`）都算；
    - 收到首个响应后转入「chunk 空闲超时」（`LLMClient.STREAM_IDLE_TIMEOUT=180s`，
      两次 chunk 之间无新数据视为链路死亡）；
    - 超过此时长仍无任何响应 → 判定该模型异常，**跳过剩余重试直接切换备用提供商**。

    取代原「按 prompt 长度动态放宽至 1800s」的首 chunk 预算：原策略下上游
    prefill 停滞时单次调用可挂起 30 分钟，叠加任务层无超时即表现为 N4
    「任务永久 running / 需求永久 analyzing」。若某提供商在超大 prompt 下
    prefill 确实长期超过 180s，可通过环境变量 `LLM_FIRST_RESPONSE_TIMEOUT`
    调大，无需改代码。
    """
    LLM_FIRST_RESPONSE_TIMEOUT: int = 180

    """单次 LLM 调用的总墙钟预算（秒）——跨 provider × 跨重试累计。

    v3.4.2（真实生成链路实测发现）：供应商全部不可用时（免费额度耗尽 403 / 限流 429 /
    首响应超时），原实现会逐家串行等待 `LLM_FIRST_RESPONSE_TIMEOUT`（3 家 × 180s ≈ 9 分钟），
    再叠加每家的重试，单次调用可悬挂数十分钟——生成任务进度停滞、无终态提示，
    还一直占用该需求的生成互斥槽位。

    本预算给单次 `call()` 设上限：超预算立即停止尝试剩余提供商并抛出
    `LLMNoProviderAvailable`（错误信息提示用户更换供应商）。同时，
    额度耗尽/认证失败等"硬失败"会立即禁用该提供商，使后续调用直接跳过。

    v3.5.6（2026-10-08 自举复测实证）：**默认 0 = 取消总预算**。240s 会把推理模型
    的正常生成中请求拦腰截断（火山 glm-5.3-flash 解析 9K 需求：思考 45.8K 字符，
    流式全程 279-370s 且 chunk 持续到达无停滞）。挂起防护由两道精准超时兜底：
    首响应超时（LLM_FIRST_RESPONSE_TIMEOUT=180s，无任何输出才切供应商）+
    流空闲超时（STREAM_IDLE_TIMEOUT=180s，吐字中断才切供应商）。
    如需恢复旧的总预算行为，显式设置本值为正整数（如 240）。
    """
    LLM_CALL_TOTAL_BUDGET: int = 0

    # ========== 生成阶段 FP 子批并发（v3.5.5，docs/22 方案①） ==========
    """功能点（FP）分片生成的并发度（1-5）。

    v3.5.5：FP 之间无数据依赖（各生成各的，汇合点统一三道去重），原串行循环
    是纯浪费——自举验证实测生成阶段 ~50min 中 LLM 占 3×14.5min 全程串行。
    并发化后预期 50min → ~20min。

    - 1 = 串行（与 v3.5.4 行为完全一致，回退开关）
    - 2-5 = 受控并发（Semaphore 限流；供应商实测 N=8 无 429，默认 3 稳妥起步）
    - 生成任务启动时从 kb_settings 动态读取（设置页可调，改后立即对下一次生成生效）；
      本字段只是默认值兜底。
    """
    GEN_FP_CONCURRENCY: int = 3

    # ========== 资源感知的全局生成并发（v4.0.2） ==========
    """全局并发生成任务的**硬上限**（跨所有需求/用户）。

    背景：`reserve_generation_slot` 只做「同一 req_id 互斥」，orchestrator 的
    `_gen_sem` 只限「单任务内 FP 并发」——两道闸都**不看服务器还能吃多少**。
    N 个用户同时点生成 = N × FP并发 个本地模型推理（bge / CrossEncoder）同时
    开跑，实测会把事件循环拖到秒级（看门狗 warn 级卡顿即此形态）。
    v4.0.2 起由 `core/resource_guard.py` 按 CPU 核数推导实时上限，本字段只是
    防误配的硬顶（如 8 核推导出 3，这里至多放到 3）。

    推导口径：`min(max(1, 物理核-1), 本字段)`，再按 CPU/内存负载降级。
    """
    GEN_TASK_CONCURRENCY_MAX: int = 4

    """CPU 10s 平均使用率达到该百分比且 1 分钟负载超过物理核 → 并发预算降一档。"""
    CPU_OVERLOAD_PERCENT: float = 85.0

    """可用内存占比低于该值 → 并发预算压到 1（防 OOM Kill，那比慢更致命）。"""
    MEM_LOW_RATIO: float = 0.15

    """排队等待资源释放的最长秒数；超过则返回"系统繁忙"让用户稍后再试。

    生成本身要 20-50 分钟，等 5 分钟资源完全可接受；但不设上限会让用户
    对着一个永远不动的页面干等，故必须有一个"明确失败"的边界。
    """
    GEN_RESOURCE_WAIT_TIMEOUT_S: float = 300.0

    """同时排队者上限；超过直接拒绝，防止请求无限堆积把连接池也占满。"""
    GEN_RESOURCE_MAX_WAITERS: int = 8

    # ========== 任务看门狗配置（v3.3 新增） ==========
    """是否启用分析任务看门狗。

    背景（N4）：LLM 流式调用自 v3.1.3 起「总生成时长不设上限」，仅施加
    首 chunk 超时（上限 1800s）与 chunk 空闲超时（STREAM_IDLE_TIMEOUT=180s）。
    因此当上游持续以极慢速率吐字时，单次调用可无限长；叠加任务层无任何
    超时/看门狗，任务会永久停在 running、需求永久停在 analyzing。

    看门狗不试图杀死协程（无协程注册表），而是把「长时间无状态更新」的任务
    标记为 interrupted —— 该状态受 requirement_tasks._update_task 的
    `status != 'interrupted'` 守卫保护，僵尸协程无法再把状态改回 running。
    """
    TASK_WATCHDOG_ENABLED: bool = True

    """任务无状态更新超过该分钟数即判定为卡死。
    取值须大于「两次状态更新之间可能出现的最长单次 LLM 调用」：
    实测最长合法调用 498s（8.3 分钟）；v3.3 起流式调用的**首响应**预算固定为
    LLM_FIRST_RESPONSE_TIMEOUT=180s（超时即切换备用），但**首响应到达后的生成
    时长仍不设上限**（大 prompt 可达 10-30 分钟），故默认 60 分钟留足余量，
    仅用于兜底真正的无限期挂起。"""
    TASK_STALE_MINUTES: int = 60

    """看门狗巡检间隔（秒）"""
    TASK_WATCHDOG_INTERVAL: int = 120

    # ========== 知识库检索口径（v3.3 / N1 治理） ==========
    """KB 问答的用例检索是否**只取已固化（归档）用例**。

    口径与需求侧对齐：需求检索只取 `status="archived"` 的需求（"只信已归档"），
    而用例侧原本**没有任何归档过滤**，尚未定稿/仍在审核的用例也会被当作依据——
    这是 N1「答案依据 AI 生成用例」的直接原因之一。

    用例的"已归档"= 随所属项目归档时写入 `test_cases.archived_at`
    （见 `api/products.py` 归档流程第 11b 步）。置 False 可回退到旧行为。
    """
    KB_CASES_ARCHIVED_ONLY: bool = True

    """用例检索的召回条数（过滤前）。只取已固化用例时，需要多召回一些再筛，
    否则 top-5 被未固化用例占满会导致过滤后所剩无几。"""
    KB_CASES_FETCH_K: int = 15

    # ========== 知识问答来源相关度门槛（v4.0.1 / 缺陷 S6） ==========
    """KB 问答参考来源的**最低余弦相似度**门槛。

    背景（docs/TEST_PLAN.md §F S6）：原实现直接把检索结果截 top-8 作为
    「参考来源」，没有任何相关度下限——弱相关/未覆盖的提问会返回 8 条
    相似度 0.43-0.47 的噪声来源，同时答案正文却是「知识库中未找到相关内容」，
    前端于是展示「参考来源」却没有任何有效内容，自相矛盾且误导用户。

    取值依据：Milvus 使用 COSINE 度量（`hit.distance` 即余弦相似度）。
    实测（§G.2）强相关命中 Top-1 ≈ 0.746；未覆盖提问的噪声集中在 0.43-0.47。
    取 0.50 作为相关度下限，可在两者之间切开，且对 bge 系列中文 embedding
    的「弱相关但不为零」区间（约 0.3-0.45）留有余量。

    置 0 可回退到旧行为（不过滤）。
    """
    KB_QA_MIN_SIMILARITY: float = 0.50

    """知识问答对敏感信息（密钥/token + PII）脱敏的总开关（v4.0.1 / 缺陷 S9）。

    需求非功能条款：「检索结果中的敏感信息（密钥、token）必须脱敏后展示」。
    关闭后回到「原样回显」的旧行为（不推荐）。
    """
    KB_REDACT_SENSITIVE: bool = True

    """是否连同个人隐私类（手机号/邮箱/身份证号）一并脱敏。

    需求条款只点名「密钥、token」，但实测泄露样本为手机号（§I #35）；
    默认一并覆盖。置 False 则仅脱敏密钥/token。
    """
    KB_REDACT_PII: bool = True

    # ========== 事件循环卡顿监控（v4.0.1 / S1 从根治理） ==========
    """是否启用事件循环卡顿看门狗。

    背景：S1（生成期所有端点超时）只能靠"端点超时"这一间接现象倒推根因，
    对**未知**阻塞源无能为力。开启后，事件循环被占住超过阈值即记录卡顿时长
    并**采样全线程调用栈**（同步阻塞调用必然出现在某个线程栈上），一次定位。
    指标由 `/api/health` 的 `data.event_loop` 透出。
    """
    LOOP_WATCHDOG_ENABLED: bool = True

    """卡顿告警阈值（秒）：超过即 WARNING（并按下方开关决定是否采样栈）。"""
    LOOP_WATCHDOG_WARN_S: float = 0.5

    """卡顿错误阈值（秒）：超过即 ERROR，并**强制**采样全线程栈。"""
    LOOP_WATCHDOG_ERROR_S: float = 3.0

    """WARNING 级卡顿是否也采样栈（默认开——更快拿到阻塞位置，冷却窗口抑制刷屏）。"""
    LOOP_WATCHDOG_DUMP_ON_WARN: bool = True

    # ========== 知识库维护任务（v3.3 / N5-c 根治） ==========
    """是否启用知识库维护周期任务。

    背景：N5 追查发现「无 PG 状态 ↔ Milvus 实际的对账机制」（N5-c），
    导致向量缺失 / 状态未回写这类不一致**无法被自动发现**。
    开启后周期执行：① 对账自愈（补写缺失向量、修正卡住的状态，**只新增不删除**）
    ② 孤儿向量扫描（**只报告，不自动删除**——删除不可逆，需人工确认）。
    """
    KB_MAINTENANCE_ENABLED: bool = True

    """知识库维护周期（秒）。默认 1 小时——对账本身轻量，但会遍历 Milvus 全量 ID。"""
    KB_MAINTENANCE_INTERVAL: int = 3600

    # ========== CRSR 编译层接入检索（v3.5.0 / docs/18 P0） ==========
    """KB 问答/澄清检索是否启用 fp_crsr 条目（功能点权威完整版 SRR）。

    False 时 kqa/clarify 检索结果过滤掉 fp_crsr 类型（向量仍在库，一键回退）。
    归档/纠错时的 CRSR 向量化（出箱投递）不受此开关控制——编译层持续更新，
    检索侧按需消费。
    """
    KB_CRSR_ENABLED: bool = True

    """KB lint 周期自检间隔（秒）。默认 24h——只读扫描 + 报告，轻量。
    置 0 或 KB_LINT_ENABLED=False 关闭。"""
    KB_LINT_ENABLED: bool = True
    KB_LINT_INTERVAL: int = 86400

    # ========== 嵌入模型配置 ==========
    """嵌入模型类型：local（sentence-transformers，默认）| cloud（远程 API 嵌入）。
    注：ChromaDB 已弃用，chroma_default 选项不再可用；模型实际加载由 EMBEDDING_MODEL_NAME 决定。"""
    EMBEDDING_MODEL_TYPE: str = "local"

    """本地嵌入模型名称（仅 EMBEDDING_MODEL_TYPE=local 时生效）"""
    EMBEDDING_MODEL_NAME: str = "BAAI/bge-large-zh-v1.5"

    """嵌入模型计算设备：cpu | cuda | mps"""
    EMBEDDING_DEVICE: str = "cpu"

    """语义去重单条用例 embedding 耗时估算（秒，docs/27 D 方案进度消息用）。
    按实测分阶段更新：E 口径前 4.0（bge-large × 整例 JSON）；E 上线后 1.8；
    A（bge-small 专用模型）上线后 0.3。仅用于 SSE「语义去重中」的预计时长
    提示，不影响去重判定逻辑。"""
    DEDUP_SINGLE_CASE_SEC: float = 4.0

    """去重专用嵌入模型（docs/27 A）：绝对路径，仅 dedup_semantic 使用。
    与全局 EMBEDDING_MODEL_NAME **完全解耦**——KB 检索/Milvus 向量化继续用
    全局 bge-large（1024 维），去重可用轻量模型（bge-small 512 维），两者
    向量空间不同，混用即错。默认空 = 不启用（用全局模型，行为同现状）。
    目录无效/加载失败 → log warning 并回退全局模型（性能特性，不阻断生成）。"""
    DEDUP_EMBEDDING_MODEL_NAME: str = ""

    """去重专用模型计算设备：cpu | cuda | mps"""
    DEDUP_EMBEDDING_DEVICE: str = "cpu"

    """语义去重阈值（docs/27）：默认 0.92（09-16 定标值）。
    换模型（A）后若定标报告要求独立适配，经环境变量覆盖（如 small 0.90）。"""
    DEDUP_SEMANTIC_THRESHOLD: float = 0.92

    """去重 embedding 分片并行 worker 数（docs/27 分片并行，v3.5.9）。
    - ""/"1"/"0"/"off" → 串行（默认，dev 机形态——8 逻辑核实测并行无收益，
      见 milvus_store.py 探针注释）；
    - "auto" → max(1, 物理核-1)（物理核 = 逻辑核÷2 保守估计）；
    - 整数 N → N 个 worker（ProcessPool，每 worker 独立加载模型 + 4 线程）。
    内存预算：每 worker ~1.6GB；服务器开启前先确认空闲内存 ≥ N×1.6GB。
    判定零影响：分片不改变向量数值，只改计算并行度。"""
    DEDUP_EMBED_WORKERS: str = ""

    # ========== 知识库维护配置 ==========
    """时间衰减率（半衰期 ~180 天）"""
    KB_DECAY_RATE: float = 0.005

    """复用加分系数"""
    KB_BOOST_REUSE: float = 1.5

    """最低权重（防止完全遗忘）"""
    KB_MIN_WEIGHT: float = 0.1

    """超过多少天未使用的知识自动归档"""
    KB_CLEANUP_DAYS: int = 365

    # ========== 检索配置 ==========
    """召回数量"""
    MATCH_TOP_K: int = 20

    """最终返回数量"""
    MATCH_FINAL_N: int = 5

    """最低相似度阈值"""
    MATCH_SIMILARITY_THRESHOLD: float = 0.70

    """GenAgent Few-shot 注入相似度阈值（优化 5：可配置，默认 0.70）"""
    FEW_SHOT_SIMILARITY_THRESHOLD: float = 0.70

    """是否启用 RRF 融合"""
    MATCH_ENABLE_RRF: bool = True

    """是否启用图谱扩展"""
    MATCH_ENABLE_GRAPH: bool = False

    # ========== 重排器配置 ==========
    """是否启用 Cross-Encoder 重排（bge-reranker-base）"""
    RERANKER_ENABLED: bool = True

    """重排模型名称（本地路径或 HuggingFace ID）"""
    RERANKER_MODEL_NAME: str = "BAAI/bge-reranker-base"

    """重排模型计算设备"""
    RERANKER_DEVICE: str = "cpu"

    """重排 Top-N 数量（超过此数量才触发重排）"""
    RERANKER_THRESHOLD: int = 5

    # ========== 质量门禁配置 ==========
    """正常模式质量评分通过阈值（docs/01 §5.2）"""
    DEFAULT_PASS_SCORE: float = 0.75

    """冷启动模式质量评分通过阈值（KB 为空时放宽，docs/01 §5.2）"""
    COLD_START_PASS_SCORE: float = 0.60

    """ClarifyAgent 最大迭代轮次（docs/01 §5.3，防止死循环）"""
    MAX_CLARIFY_ITERATIONS: int = 3

    """ClarifyAgent Optional 类问题上限（v2.1：Critical/Important 不限条数）"""
    CLARIFY_OPTIONAL_LIMIT: int = 20

    # ========== KB 访问日志配置 ==========
    """是否启用 KB 访问日志（v2.1 新增）"""
    KB_ACCESS_LOG_ENABLED: bool = True

    """单次需求分析最多记录条数"""
    KB_ACCESS_LOG_MAX_ENTRIES: int = 50

    # ========== Redis 配置 ==========
    """Redis 主机地址"""
    REDIS_HOST: str = ""

    """Redis 端口"""
    REDIS_PORT: int = 6379

    """Redis 数据库编号"""
    REDIS_DB: int = 0

    """Redis 密码"""
    REDIS_PASSWORD: str = ""

    """LLM 每分钟最大请求数（限流用）"""
    LLM_RPM_LIMIT: int = 500

    """LLM 每分钟最大 Token 数"""
    LLM_TPM_LIMIT: int = 100000

    # ========== Celery 配置 ==========
    """Celery broker URL（Redis）"""
    CELERY_BROKER_URL: str = ""

    """Celery result backend URL（Redis）"""
    CELERY_RESULT_BACKEND: str = ""

    # ========== 限流配置 ==========
    """全局 API 每分钟最大请求数（内存 Token Bucket 限流）"""
    API_RPM_LIMIT: int = 60

    """限流窗口大小（秒）"""
    API_RATE_WINDOW: int = 60

    # ========== 服务配置 ==========
    """后端服务端口"""
    APP_PORT: int = 8000

    """前端开发服务地址（CORS 白名单）"""
    FRONTEND_URL: str = "http://localhost:5173"

    """调试模式（生产环境必须设为 False）"""
    DEBUG: bool = False

    """SQL 语句全量日志（v3.1.2：与 DEBUG 解耦，默认关闭；全量 SQL echo 拖慢事件循环）"""
    SQL_ECHO: bool = False

    @property
    def pg_async_url(self) -> str:
        """获取 PostgreSQL 异步连接字符串。

        Returns:
            str: 异步 PG URL（使用 asyncpg 驱动）

        Raises:
            ValueError: 未配置 DATABASE_URL 时抛出
        """
        if not self.DATABASE_URL:
            raise ValueError("DATABASE_URL 未配置，PostgreSQL 连接字符串为必填项")
        # 替换 postgresql:// 为 postgresql+asyncpg://
        return self.DATABASE_URL.replace(
            "postgresql://",
            "postgresql+asyncpg://"
        ).replace(
            "postgres://",
            "postgresql+asyncpg://"
        )

    @property
    def pg_sync_url(self) -> str:
        """获取 PostgreSQL 同步连接字符串（供 prompt_store 等同步场景使用）。

        Returns:
            str: 同步 PG URL（使用 psycopg2 驱动）

        Raises:
            ValueError: 未配置 DATABASE_URL 时抛出
        """
        if not self.DATABASE_URL:
            raise ValueError("DATABASE_URL 未配置，PostgreSQL 连接字符串为必填项")
        # 替换为 psycopg2 同步驱动
        return self.DATABASE_URL.replace(
            "postgresql+asyncpg://",
            "postgresql+psycopg2://"
        ).replace(
            "postgresql://",
            "postgresql+psycopg2://"
        ).replace(
            "postgres://",
            "postgresql+psycopg2://"
        )

    @property
    def llm_config(self) -> dict:
        """获取当前 LLM 提供商配置。

        优先级：DB 持久化配置 > .env 文件默认值.
        """
        # 优先从 DB 加载的配置读取
        provider_cfgs = getattr(self, "_all_provider_configs", {})
        db_cfg = provider_cfgs.get(self.LLM_PROVIDER)
        if db_cfg and db_cfg.get("api_key"):
            return db_cfg

        # 回退到 .env
        if self.LLM_PROVIDER == "openai":
            return {
                "api_key": self.OPENAI_API_KEY,
                "base_url": self.OPENAI_BASE_URL,
                "model": self.OPENAI_MODEL,
            }
        return {
            "api_key": self.DEEPSEEK_API_KEY,
            "base_url": self.DEEPSEEK_BASE_URL,
            "model": self.DEEPSEEK_MODEL,
        }

    @property
    def fallback_llm_config(self) -> dict | None:
        """获取备用 LLM 配置（故障转移用）。

        Returns:
            dict | None: 备用配置；若主提供商是 deepseek 则返回 openai 配置，反之亦然。
            若备用 Key 未配置则返回 None。
        """
        if self.LLM_PROVIDER == "deepseek":
            if self.OPENAI_API_KEY:
                return {
                    "api_key": self.OPENAI_API_KEY,
                    "base_url": self.OPENAI_BASE_URL,
                    "model": self.OPENAI_MODEL,
                }
        else:
            if self.DEEPSEEK_API_KEY:
                return {
                    "api_key": self.DEEPSEEK_API_KEY,
                    "base_url": self.DEEPSEEK_BASE_URL,
                    "model": self.DEEPSEEK_MODEL,
                }
        return None

    @property
    def has_redis(self) -> bool:
        """判断是否配置了 Redis。

        Returns:
            bool: True 表示已配置 Redis 主机
        """
        return bool(self.REDIS_HOST)

    def ensure_data_dirs(self) -> None:
        """确保数据目录存在。

        在应用启动时调用，创建数据所需的目录结构（密钥、日志等）。
        """
        Path(self.DATA_DIR).mkdir(parents=True, exist_ok=True)

    def validate_security(self) -> list[str]:
        """启动时安全校验，返回警告列表（空列表表示无警告）。

        生产环境（DEBUG=False）发现致命问题会抛 RuntimeError。

        Returns:
            list[str]: 警告消息列表

        Raises:
            RuntimeError: 生产环境缺少必要的安全配置
        """
        warnings: list[str] = []

        # APP_SECRET_KEY 校验
        if not self.APP_SECRET_KEY:
            if not self.DEBUG:
                raise RuntimeError(
                    "生产环境必须设置 APP_SECRET_KEY 环境变量（用于 JWT 签名）"
                )
            # 开发环境自动生成
            import secrets as _secrets
            self.APP_SECRET_KEY = _secrets.token_urlsafe(32)
            warnings.append("开发环境：已自动生成临时 APP_SECRET_KEY，重启后 JWT 失效")
        elif len(self.APP_SECRET_KEY) < 16:
            warnings.append("APP_SECRET_KEY 长度不足 16 字符，建议使用更长的随机串")

        # MASTER_KEY 校验（LLM API Key 加密密钥）
        # v2.8.60 修复：原代码使用 os.environ.get("MASTER_KEY") 读取，
        # 但 pydantic-settings 已将 .env 中的 MASTER_KEY 加载到 self.MASTER_KEY，
        # 本地 python -m app.main 启动时（未 export 到 shell）会导致 self.MASTER_KEY 有值
        # 但 os.environ.get 返回空，触发误报 RuntimeError。
        master_key_env = self.MASTER_KEY or ""
        if not master_key_env and not self.DEBUG:
            raise RuntimeError(
                "生产环境必须设置 MASTER_KEY 环境变量（用于加密数据库中存储的 LLM API Key）。\n"
                "生成方法: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"\n"
                "注意: 请妥善保管此密钥，丢失将导致已加密 API Key 永久不可解密。"
            )
        if not master_key_env and self.DEBUG:
            # 开发/内网环境将使用确定性派生密钥 + 密钥文件持久化
            # 实际密钥由 crypto.py:_derive_default_master_key() 生成
            warnings.append(
                "开发环境：未设置 MASTER_KEY，将使用内网固定派生密钥。"
                "生产环境请务必设置 MASTER_KEY 环境变量。"
            )
        if master_key_env:
            # 简单格式校验（Fernet 密钥为 44 字符 base64）
            if len(master_key_env) != 44 or not master_key_env.endswith("="):
                warnings.append("MASTER_KEY 格式可能不正确（应为 44 字符 base64），请检查")

        return warnings


# 全局配置单例
settings = Settings()
