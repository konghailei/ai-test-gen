# docs/27 — 语义去重 embedding 性能优化设计方案（D → E → 定标 → A → B）

> 撰写日期：2026-10-12 ｜ 上游：docs/26 性能排查（结论已复核成立）+ 本机补充取证
> **本文档为开发设计方案，未做任何代码/数据变更，待拍板后实施。**
> 依据纪律：阈值/合同类改动先用真实数据定标；一项一 commit，跑完回归再 push。

> ⚠️ **2026-10-12 复核更正（见 `docs/29`）**：附录 A 的 C3 定标 gate 存在**方法学缺陷**，
> 其 verdict（`E_FAIL` / `T3 FAIL` / `T4 FAIL`）**不成立**，E 与 A 目前是「未验证」而非「已否决」。
> 三处缺陷：① 定标语料把 6 个需求汇成一张矩阵，**66% 的对是跨需求对，生产里永远不会比较**
> （T1 漏判明细 54/60、T4 divergent 35/40 都是跨 req）；② `pos_pairs_matched=0`，
> 无正样本标签，gate 退化为「与 T0 一致」；③ `divergent`（标题 3-gram<0.45）不是误删信号
> （T4 同 req 的 5 条 divergent **全部是真重复**）。
> 另：§A.2「漏判 469 对全部是真重复」不成立（抽样里至少 8 条明显不是重复）；
> §2.3「bge-base 1024 维」有误（实为 768 维）；§A.5 的 `1.6×` 算式不自洽且未实测。
> **同时新发现生产正确性缺陷**：0.92 阈值在真实留痕里约 25-34% 是误删
> （产品/项目、编辑/创建、通过/未通过、.docx/.markdown），docs/26「正确性无影响」不成立。
> 处置建议：先修误删（P0）→ 重建 per-req 评测（P1）→ 零风险提速（P2），详见 docs/29。

---

## 0. 摘要

| 项 | 内容 |
|---|---|
| 问题 | `dedup_semantic`（v3.4.5）在编排主链路 3 个对称点对全量用例做 bge-large CPU embedding，v1.0-r2 轮单点 ~35min、全链路去重等待 ~60min（占总时长 60%），进度条停在 70% 造成假死观感 |
| 方案组合 | **D 进度可观测 + E 语义字段序列化 + A 去重专用小模型 + B 进程内向量缓存**（原 F 并入 B） |
| 预期收益 | dev 机（4 物理核）：全链路语义去重 ~52min → **~3-4min（约 12-15×）**；多核服务器再 ÷3~4×（§2.6）；即使 A 被定标否决，B+D+E 保底 ~3× |
| 硬约束 | ① A 绝不动全局 `EMBEDDING_MODEL_NAME`（KB 向量 dim=1024 依赖它）；② 阈值 0.92 换模型/换口径必须复标；③ 去 重哲学不变——宁可不报，不误删 |
| 实施顺序 | D（独立先行）→ E（锁序列化口径）→ **定标 gate** → A（过 gate 才上）→ B（最后，key 绑定模型+口径） |

### 对 docs/26 的两处修正（本次取证新发现）

1. **A 不需要重导 base 镜像**：docker-compose.yml:50 显示 bge 模型是 **bind mount**（`./models/bge-large-zh-v1.5:/models/bge-large-zh-v1.5:ro`），不是镜像内置。加 small 模型 = 宿主机下载 + compose 加一行挂载 + 重启，**零镜像重建**（依赖纪律针对 requirements.txt，不针对模型文件）。
2. **"调大 torch 线程"不可行**：容器 8 逻辑核，实测 threads=4 耗时 35.2s vs threads=8 **42.9s（反而慢 22%）**——超线程对 matmul 纯竞争，4 线程已是最优，该方向关闭。

### 本次取证的其他关键事实

- bge `max_seq_length=512`，真实用例整例 JSON 300~950 字符 → 大面积截断，且**语义信号本就集中在 title/steps/assertions**，priority/gen_metadata/confidence 等字段对"是否重复"零贡献却计入 embedding 文本（E 的依据）；
- cgroup `cpu.max 100000`（无配额限制），慢不是资源受限，是**算力花在了不该花的地方**；
- `merge_regression_cases`（gen_agent.py:967）只合并不改写内容；ReviewAgent 的 reviewed_cases **可能追加审核元数据字段**——E 口径（语义字段序列化）天然排除这些字段 → B 缓存对"review 后第二轮去重"仍能命中（E 与 B 的协同点）；
- 生成节点已有 `_emit_gen` 消息通道（orchestrator.py:1141，走 `_gen_event_queue` ContextVar → SSE），`STAGE_PROGRESS["generate"]=70` 即假死观感来源——D 方案有现成挂载点。

---

## 1. 目标与非目标

**目标**
1. 全链路语义去重耗时降至分钟级（3-4min 量级）；
2. 去重判定结果与现状**零漂移**（或差异可解释、可接受后才切换）；
3. 去重期间任务有可观测进度，消除假死观感；
4. KB 检索路径（Milvus 向量化、celery 归档 embedding）**零影响**。

**非目标**
- 不动 0.92 阈值哲学（宁可不报，不误删）——复标若需微调，单独立项说明；
- 不动 GenAgent 四级去重的 L2/L4（标题级、量小非瓶颈、0.95 阈值是 large 定的标，少动一个定标面）；
- 不引入 GPU / Redis / Milvus 改动（celery 的 KB 向量化与去重无关，不碰）。

---

## 2. 方案详细设计

### 2.1 D：去重进度可观测（先行，独立，纯体验）

**问题**：语义去重期间（最长 35min）无任何 SSE 消息，进度恒 70%，无法与挂死区分。

**设计**：两个挂载点，去重**前后各发一条**消息：

```python
# 挂载点 1：orchestrator._generate_node（批内去重日志后、dedup_semantic 前）
await _emit_gen(f"语义去重中：{len(cases)} 条，预计 ~{est_min} 分钟", {
    "key": "__dedup__", "name": "语义去重", "status": "running",
    "cases": len(cases), "est_seconds": est_sec,
})
cases, semantic_removed = await asyncio.to_thread(dedup_semantic, ...)
await _emit_gen(f"语义去重完成：去掉 {len(semantic_removed)} 条（保留 {len(cases)} 条）", {
    "key": "__dedup__", "name": "语义去重", "status": "done",
    "removed": len(semantic_removed), "kept": len(cases),
})

# 挂载点 2：orchestrator._review_node（补生成合并后、第二次 dedup_semantic 前后，同构）
```

**预计时长估算**：`est_sec = n_cases × SINGLE_CASE_SEC`，`SINGLE_CASE_SEC` 为配置常量（初值按实测：E 上线前 4.0，E 后 1.8，A 后 0.3——每个阶段用定标数据更新，宁可高估）。review 节点无 `_emit_gen`，复用 `self._gen_event_queue.get()` + `make_sse_event` 构造（与 generate 节点同构的局部 helper）。

**验收**：复跑一轮生成，SSE 流中出现「语义去重中（N 条，预计 ~Xmin）」消息；前端 FP 进度卡片出现去重条目；去重期间 `updated_at` 仍推进（消息侧带时间戳）。

### 2.2 E：语义字段序列化（锁口径，A/B 的前置）

**问题**：现对整例 `json.dumps(case)` 做 embedding，元数据字段（priority/gen_metadata/confidence/covered_point_ids/source 等）占 40-60% 文本量，对"是否重复"零贡献，还加剧 512 截断。

**设计**（`case_dedup.py`）：

```python
SEMANTIC_SERIALIZATION_VERSION = "e1"  # 口径版本号，进缓存 key（见 B），改口径必改版本

def _semantic_text(case: dict) -> str:
    """去重专用序列化：只保留语义负载字段（口径 v=e1）。

    字段清单与取舍依据：
    - title：场景主信号（一级）
    - steps[].action / steps[].expected：操作与预期（一级；不含 step 序号等结构字段）
    - assertions：断言（一级）
    - type：用例类型（边界/异常/集成改写常改 type，弱信号，保留）
    - 显式排除：priority / confidence / gen_metadata / covered_point_ids /
      source / id / status / review 相关字段——对重复判定零贡献，
      且 review 节点会追加/修改它们（排除后 B 缓存才能跨 review 命中）。
    """
    parts = [str(case.get("title", "")), str(case.get("type", ""))]
    for s in case.get("steps") or []:
        if isinstance(s, dict):
            parts.append(f"{s.get('action','')}->{s.get('expected','')}")
    for a in case.get("assertions") or []:
        parts.append(str(a))
    return "\n".join(p for p in parts if p)
```

`dedup_semantic` 内 `texts = [_json.dumps(c, ...)]` → `texts = [_semantic_text(c) for _, c in indexed]`。

**协同收益**（E↔B）：review 后的用例若只被追加审核元数据 → `_semantic_text` 输出不变 → 第二轮去重缓存全命中；若 title/steps/assertions 被实质修改 → 文本变 → 正确重算。

**验收**：
- 单测：`_semantic_text` 对"同语义负载、不同元数据"的两条用例产出相同文本；对"改了 steps.action"的用例产出不同文本；
- 定标（见 §2.3）：E 口径下 known-duplicate 判定与整例口径一致。

### 2.3 定标 gate（E 之后、A/B 之前的必过关卡）

**目的**：① 验证 E 口径不改变 large 模型的去重判定；② 验证 small 在 E 口径下与 large 的判定一致性（A 的上线依据）。

**脚本**：`_calib_dedup_embed.py`（gitignored，`_` 前缀，容器内 `docker exec -i ... python - < _calib_dedup_embed.py` 方式跑，不碰 `/app` 避免 reload 杀任务）。

**数据源**（三层）：
1. **分布对照**：DB 落库用例（`test_cases` 最新批次，v1.0-r2 242 条 + v2.0-r2 等，`case_json` 列），三档全量 embed：
   `T0 = large × 整例 JSON`（现状基线）、`T1 = large × _semantic_text`、`T2 = small × _semantic_text`；
2. **ground truth 增强**：`docker logs ai-test-gen-api` grep「语义去重」提取 v1.0-r2 已知重复对（removed/kept 标题 + 相似度值），映射回 DB 用例（title 匹配）作为已知正样本对；
3. **两两矩阵**：每档内计算全部两两余弦相似度（403 条 ≈ 81k 对，余弦计算 <1s，耗时全在 embed：T0/T1 各 ~15-25min，T2 ~2min）。

**判定标准（结构化，不比对文本措辞）**：

| 对照 | 指标 | 通过线 |
|---|---|---|
| T1 vs T0（E 漂移） | 已知重复对相似度差值 Δ sim 的分布 | 正样本对 sim(T1) ≥ 0.92 的比例 ≥ sim(T0) 比例（E 不得降低召回）；须保留对（DB 内不同用例对，排除已知重复）sim(T1) ≥ 0.92 的新增误删对 = 0 |
| T2 vs T1（A 漂移） | 同上两指标 + 判定翻转对清单 | 正样本漏判 ≤ 1 对（与 09-16 定标 8/10 同水位）；须保留对误删 = 0；翻转对逐条人工复核 |
| 阈值适配 | 若 T2 分布整体左移但双峰仍分离 | 允许给 A 配独立阈值 `DEDUP_SEMANTIC_THRESHOLD`（small 专用），在定标报告中给出依据 |

**gate 结论三态**：通过 → A 上 small；降级 → A 改用 bge-base-zh-v1.5（102M、**1024 维**、与 KB 模型同维度更稳）重新定标；否决 → A 搁置，B+D+E 照常上线（保底 3×）。

### 2.4 A：去重专用小模型（独立配置 + 独立加载器）

**问题**：bge-large 335M 在 CPU 上 1.1-4.4s/条是物理瓶颈。

**设计**（核心原则：**与全局 KB 模型完全解耦**）：

```python
# config.py 新增（默认空 = 完全向后兼容，行为同现状）
DEDUP_EMBEDDING_MODEL_NAME: str = ""   # 例：/models/bge-small-zh-v1.5
DEDUP_EMBEDDING_DEVICE: str = "cpu"
DEDUP_SEMANTIC_THRESHOLD: float = 0.92  # 去重专用阈值（A 定标后可独立适配）
```

```python
# milvus_store.py：VectorStore 新增独立模型槽（与 self._model 完全隔离）
def _ensure_dedup_model(self):
    """去重专用模型。空配置 → 返回 None（调用方回退全局模型）。
    加载逻辑复用 _model_dir_valid + 双检锁，但绝不写 self._model。"""

def embed_texts_for_dedup(self, texts: list[str]) -> list[list[float]]:
    """去重专用 embed 入口。模型链：
    DEDUP_EMBEDDING_MODEL_NAME 有效 → 专用模型；
    为空/加载失败 → 全局 _embed（log warning）；
    embed 异常 → 由 dedup_semantic 既有降级接住（3-gram）。"""
```

`orchestrator` 两处 `dedup_semantic` 调用的 `embed_func` 从 `_vs.embed_texts` 改为 `_vs.embed_texts_for_dedup`（grep 确认仅这两处 + GenAgent L2/L4 **保持不动**，后者继续用全局模型）。

**部署**（修正 docs/26，零镜像重建）：
1. 宿主机下载模型到 `./models/bge-small-zh-v1.5/`（办公网用 `HF_ENDPOINT=https://hf-mirror.com` 或 modelscope，权重 + config.json + tokenizer 齐全，用 `_model_dir_valid` 同等标准验收）；
2. `docker-compose.yml` api service 加挂载 `- ./models/bge-small-zh-v1.5:/models/bge-small-zh-v1.5:ro` + 环境变量 `DEDUP_EMBEDDING_MODEL_NAME=/models/bge-small-zh-v1.5`（**celery_worker 不加**——去重只在 API 进程编排链路，celery 的 KB 向量化不碰）；
3. `docker-compose up -d backend`（bind mount 变更需重建容器，非镜像构建）。

**内存核查**（上线前必测）：容器内实驻 large ~1.5-2GB + small ~0.2GB，`docker stats` 确认无压力；超限则 small 换 onnx int8 量化版（预期再省 60%）。

**验收**：配置生效（日志打印"去重专用模型: bge-small"）；`embed_texts_for_dedup` 单测（空配置回退/加载失败回退/正常三态）；定标 gate 报告归档进本 docs 附录。

### 2.5 B：进程内向量缓存（F 场景并入）

**问题**：第二轮去重（review 合并点）对第一轮已算过的 ~90% 用例全量重算。

**设计**（`case_dedup.py`，module 级 LRU，不引 Redis——两次去重同进程，跨进程无需求）：

```python
from collections import OrderedDict

_EMBED_CACHE: OrderedDict[str, list[float]] = OrderedDict()
_EMBED_CACHE_MAX = 4096  # 条目上限：4096 × 1024 维 float ≈ 16MB（small 512 维更小）
_CACHE_LOCK = threading.Lock()

def _cache_key(model_id: str, text: str) -> str:
    """key = md5(model_id | SEMANTIC_SERIALIZATION_VERSION | text)。
    模型换 → key 变（A/B 协同）；口径改版本号 → 全量失效（防 stale）。
    model_id 用加载后的模型路径（配置值），不是全局 settings——防 A/B 环境错位。"""

def _embed_cached(embed_func, model_id: str, texts: list[str]):
    """批量查缓存 → 只 embed miss 的 → 回填。命中数进 logger.debug
    （D 的消息可透出 hit/miss，验证缓存真实工作——"打标记必须验证到达消费方"）。"""
```

`dedup_semantic` 签名扩展：`model_id: str = ""`（orchestrator 传入实际生效的模型路径），内部 `vectors = _embed_cached(embed_func, model_id, texts)`。

**失效与正确性**：
- 内容变更（title/steps/assertions 改动）→ text 变 → key 变 → 自动重算（无需主动失效）；
- 口径变更 → `SEMANTIC_SERIALIZATION_VERSION` 递增 → 全量 miss；
- 进程重启 → 缓存清空（可接受：只损失一轮，第一轮全量算完后第二轮就命中）；
- **单测三件套**：同输入二次调用 100% 命中（embed_func 调用计数=1）；改一条用例的 action 后该条 miss 其余命中；换 model_id 后全量 miss。

**收益**：v1.0-r2 场景第二轮 280 条中 ~266 条命中 → 只 embed 14 条新增 → 第二轮 17min → <1min。

### 2.6 多核服务器扩缩容说明（部署形态相关，实现内聚在 A 的 embed 入口）

**本机实证**（2026-10-12，dev 环境）：容器 8 逻辑核 = 4 物理核 + 超线程；torch 默认 4 线程已是最优，8 线程反而慢 22%（超线程对 matmul 纯竞争）。**"加线程数"在超线程核上无收益，收益只来自物理核。**

**上线多核服务器**（如 16/32 物理核）：
- 单进程 intra-op（一进程多线程）非线性：transformer 层间串行 + 内存带宽瓶颈，16 物理核预计 ~3-4×（非 4× 整）；
- 更优形态是**分片并行**（data parallel）：`embed_texts_for_dedup` 内部把 texts 分片，线程池 `min(物理核数/4, 4)` 个 worker 并行 encode，每 worker `torch.set_num_threads(4)`（4 线程是单次 encode 的实测最优粒度）；核数探测用 `os.cpu_count() ÷ 2` 保守估物理核（容器内逻辑核含超线程），支持配置覆盖。作为实现细节内聚在 A 的 embed 入口，不改变方案结构；
- 部署注意：容器 cpu 配额别卡在 4 核以下（会吃掉全部多核收益）。

**叠加关系（正交，可乘）**：多核 = 单条耗时 ÷ 核数因子；E/A = 砍掉 80%+ 计算量；B = 砍掉 ~90% 重复计算。例：526 条、16 物理核服务器、D+E+A+B 全上 → 单条 ~0.3s ÷ 3.5 ≈ 0.09s，主合并 ~48s + 第二轮缓存命中 ≈ **分钟内**。

---

## 3. 实施顺序与 commit 划分

```
C1  D  进度可观测（含 SINGLE_CASE_SEC 配置）           ← 独立，先行，当天可上
C2  E  _semantic_text + 口径版本常量 + dedup_semantic 切换 + 单测
C3  定标脚本跑批 → gate 报告（不改产品代码，报告附录进 docs/27）
    ├─ 通过 → C4A small；降级 → C4B bge-base 再定标；否决 → 跳过 C4
C4  A  独立配置 + embed_texts_for_dedup + compose 挂载 + 单测 + 内存核查
C5  B  缓存 + model_id 透传 + hit/miss 透出 + 单测三件套
C6  性能回验：复跑 v1.0 规模生成，对照 docs/26 基线（35min → 目标 ≤5min）
```

- 每个 commit 跑对应回归：C1/C2/C5 动 orchestrator/case_dedup → **phase4/5/6 + unit 125**（生成链路共享模块纪律）；C4 动 milvus_store/config → phase2-6 全量 + 真实端到端（`verify_real_fixes.py` 模式）；
- 一项一 commit（中文 message），跑完回归再 push；
- C3 是 gate 不是 commit：定标报告不通过则 C4 不存在，C5 不依赖 C4（缓存对 large 同样有效）。

## 4. 验收标准（总）

1. **性能**：v1.0 规模（~500 条）全链路语义去重 ≤ 5min（docs/26 基线 60min）；进度条期间有消息、无假死；
2. **正确性**：定标 gate 报告（E 漂移 + A 漂移）通过线全绿，归档本 docs；回归全绿（基线 74/91/158/112/141 + unit 125/1 skip）；
3. **兼容**：`DEDUP_EMBEDDING_MODEL_NAME` 为空时行为与现状完全一致（默认部署零变化）；KB 检索/归档向量化路径无任何改动（grep 实证 `_embed`/`_upsert` 调用方未变）；
4. **可观测**：SSE 消息含 hit/miss 与预计时长；docker logs 可 grep 到缓存命中数。

## 5. 风险与回滚

| 风险 | 概率 | 缓解/回滚 |
|---|---|---|
| E 口径改变相似度分布（漏报增加） | 低 | 定标 gate 拦截；`SEMANTIC_SERIALIZATION_VERSION` 可独立回退（revert C2 即回整例口径） |
| small 定标不通过 | 中 | 三态 gate：降级 bge-base（同 1024 维）或搁置 A；B+D+E 收益独立成立（~3×保底） |
| 双模型内存压力 | 低 | 上线前 `docker stats` 实测；超限换 onnx int8 |
| 缓存 stale（模型/口径错位） | 低 | key 三元组绑定 + 单测三件套；进程内生命周期天然兜底 |
| review 改写 steps 导致缓存全 miss | 低 | 属正确行为（内容变则重算）；hit 率进日志可观测，若持续低位再优化 |
| 4 核容器并发 embed 打满 CPU（docs/22 担忧） | 低 | small 上线后单次 embed CPU 占用降 ~85%；D 消息使等待可感知 |

## 6. 附：定标脚本 `_calib_dedup_embed.py` 设计概要

```python
# 输入：DB test_cases 最新 N 批（case_json）+ docker logs 提取的已知重复对
# 步骤：
# 1) 载入用例集 C（含 title 匹配回 DB 的已知重复对 P_pos、须保留对 P_keep）
# 2) 三档 embed：T0(large, 整例JSON) / T1(large, _semantic_text) / T2(small, _semantic_text)
#    —— small 模型先挂载再跑（脚本内直接 SentenceTransformer('/models/bge-small-zh-v1.5')）
# 3) 每档内全量两两余弦矩阵（numpy，81k 对 <1s）
# 4) 输出结构化报告（JSON + 控制台表）：
#    - P_pos 集合在 T0/T1/T2 下 sim≥0.92 的命中率（召回）
#    - P_keep 集合在 T0/T1/T2 下 sim≥0.92 的误删数
#    - T1-T0 / T2-T1 的 Δsim 直方图分桶（0.01 步进）
#    - 判定翻转对明细（title 对 + 两档 sim 值，供人工复核）
# 5) gate 判定：按 §2.3 通过线自动给出 通过/降级/否决 三态结论
```

约束：只读 DB + logs，不写任何库表；容器内 `python - <` 方式执行（不落 /app 文件，防 reload 杀在飞任务）；预计总耗时 ~40min（T0/T1 embed 占大头），可拆两次跑。

---

**待大哥拍板**：① 组合与顺序是否认可；② small 模型下载渠道（hf-mirror / modelscope）；③ 定标 gate 的通过线（召回 ≥ 现状、误删 = 0、漏判 ≤1）是否放行。

---

## 附录 A：定标 gate 执行记录（2026-10-12，1055 条真实用例）

**数据源**：`test_cases` 表 10-09~10-12 全部 7 个批次共 **1055 条**（`case_json` 列）；已知重复对从 `docker logs` 的「语义去重」记录提取（32 对，但 removed 侧用例被去重后不落库 → 双侧匹配 0 对，gate 硬指标改为**相对 T0 的结构性判定**）。脚本 `_calib_dedup_embed.py` / `_calib_t3.py`（gitignored，容器内 detached 执行）。

### A.1 主实验（三档对照）

| 档 | 模型 × 口径 | 判重对 | 单条 embed | 总耗时 | vs T0 |
|---|---|---|---|---|---|
| **T0（基线）** | large × 整例 JSON | **586** | 3.810s | 4019s（67min） | — |
| **T1（E）** | large × 语义字段 | **119** | — | — | 漏判 **469**、多判 2 |
| **T2（A）** | small × 语义字段 | **152** | — | — | 漏判 441、多判 7（差异显著 5） |

T0 的 67min/1055 条 ≈ 3.81s/条，独立复现 docs/26 的瓶颈量化（526 条 ≈ 33min vs docs/26 实测 35min）✅。

### A.2 E 否决（gate: E_FAIL）〔**结论已被 docs/29 推翻，保留原文以供对照**〕

- T1 漏判的 469 对经抽样**全部是真重复**（如「每轮澄清问题数刚好为5个」三个变体互判、「用户提交空答案」三个变体互判）；
- T1 漏判对 sim 分布：中位 **0.872**、43/60 落在 0.90+——真重复簇整体掉到 0.85~0.92，与 09-16 定标中须保留对的上界（0.908）**重叠，调阈值无法救回**；
- **根因（新认知）**：整例 JSON 的键名/结构共现**不是噪声，是该去重口径的工作部件**——同场景用例的 JSON 结构 token 完全共现，系统性抬高余弦基数；0.92 阈值（09-16 定标）就是在含该基数的分布上定的。E 去掉结构 = 分布塌陷 = 判定失效。
- 处置：口径回退整例 JSON（版本 e1→e2），commit `450a542`。**教训：任何"序列化瘦身"尝试都会打破该基数；去重口径不得瘦身。**

### A.3 A（bge-small）否决（gate: FAIL）〔**结论已被 docs/29 推翻（判据无效），保留原文以供对照**〕

- T3 = small × 整例 JSON：判重 **979 对**（T0 的 1.67 倍），其中 **871 对标题 3-gram < 0.45（差异显著）**——sim 分布 min=0.92、中位 0.938，全部堆在阈值刚上线；
- **根因**：bge-small（24M）向量空间分辨率不足，对"不同场景"也打 ≥0.92，分布严重重叠；
- 处置：A 的 small 路径否决。docs/27 §2.4 的降级路径（bge-base）见 A.4。

### A.4 A 降级路径（T4 = bge-base × 整例 JSON）——**FAIL，A 全线否决**

| 档 | 判重对 | vs T0 | T1 漏判对召回 | 差异显著对 | 结论 |
|---|---|---|---|---|---|
| T3 small × 整例 JSON | 979 | 1.67× | 52/60 | **871** | ❌ 过判（分辨率不足，不同场景也 ≥0.92） |
| T4 base × 整例 JSON | 415 | 0.71× | **32/60（53%）** | 331 | ❌ 漏判+混入（分布下移且重叠） |

**最终结论：A（换轻量模型）全线否决。** small 与 base 的相似度分布都无法用单一阈值与须保留对分离——0.92 阈值与 bge-large 的向量分布**深度绑定**，这不是"微调阈值"能迁移的。docs/26 方案 A 从根上不可行（除非接受去重质量显著下降）。

**处置**：compose 挂载行已清理（不留死配置）；模型文件保留在 `./models/`（small 92MB / base 409MB，未删）；代码入口 `embed_texts_for_dedup` 保留且默认关闭（单测把守隔离性），将来若重启 A 路线（如部署环境变化/愿意接受重新定标），加一行挂载 + 环境变量即可，但**必须先重跑本附录定标**。

### A.5 最终收益盘点（2026-10-17 定稿）

| 方案 | 结论 | 收益 |
|---|---|---|
| D 进度可观测 | ✅ 上线（C1） | 消除 70% 假死观感 |
| B 向量缓存 | ✅ 上线（C5 + key/文本解耦修正） | 第二轮去重 17min → <1min |
| E 语义字段瘦身 | ❌ 否决并回退（e2=整例 JSON） | — （负收益：判重 -80%） |
| A 轻量模型 | ❌ 全线否决（small/base 双实验） | — （small 误删风险 871/979） |

**净收益：v1.0 规模全链路语义去重 ~52min → ~33min（1.6×），判定逻辑与阈值零改动、零风险。** 若将来部署到多核服务器：按 §2.6 分片并行再 ÷3~4×（→ ~10min）。

---

## 附录 B：定标后方案重定向与实施记录（2026-10-12~13，v3.5.9）

### B.1 定标结论对方案空间的重定向（大哥 10-12 拍板）

附录 A 的 E/A 否决不是坏消息，而是划清了可行域边界：

| 方向 | 定标结论 | 含义 |
|---|---|---|
| **换空间**（E 瘦身口径 / A 轻量模型） | ❌ 全线否决 | 0.92 阈值与 bge-large×整例JSON 的分布**深度绑定**，任何"换模型/换序列化"都要重跑 67min×N 档定标 |
| **物理加速**（并行 / 量化 / 算力） | ✅ 未被否决 | 不碰模型、口径、阈值 → 相似度分布不变 → **判定结果逐对一致**，无需重新定标 |

据此大哥拍板：**做 1️⃣ 分片并行 + 2️⃣ 增量去重（预嵌入），不做 3️⃣ ONNX 量化**（1+2 已把问题压到可接受区间，ONNX 需改 base 镜像 + 重定标，暂缓）。

### B.2 1️⃣ 分片并行：探针数据与形态决策

探针（`_probe_shard.py`，容器内 detached，48 条真实量级文本）：

| 形态 | 总耗时 | vs 串行 | 结论 |
|---|---|---|---|
| 串行 4 线程 | 127.2s | 1.00× | 基线 |
| ThreadPool 2 workers | 122.4s | **1.04×** | 假并行——GIL 之外，torch intra-op 线程池**进程级共享**，worker 线程在同 4 个 OMP 线程上排队 |
| ThreadPool 4 workers | 333.2s | **0.38×** | 负优化——4×4=16 线程争 8 逻辑核 |
| ProcessPool 2/4 workers | 未完成 | — | dev 机内存不足（宿主 16GB、模型 1.3GB/worker + 系统占用，MemAvailable 一度 ~1.8GB）无法完成基准 |

**形态决策**：
- **线程池形态永久无效**（GIL/thread 语义已证伪，勿再尝试）；
- **dev 机不开并行**：workers=1（默认），行为与 v3.5.8 前逐位一致；
- **服务器用 ProcessPoolExecutor 分片**：每 worker 独立加载模型 + `torch.set_num_threads(4)`（4 线程是单次 encode 实测最优粒度，docs/26 线程定标），进程池 spawn 启动、常驻复用。

**服务器性能保证机制**（dev 机容量低、无法预先实测，故保证不靠"猜"，靠三层）：
1. **自适应默认**：`DEDUP_EMBED_WORKERS=auto` → `max(1, 物理核-1)`（物理核 = 逻辑核÷2 保守估计，留 1 核给主进程）；默认留空 = 串行（dev 形态零风险）。
2. **服务器端基准脚本**：`backend/scripts/bench_dedup_embed.py`——部署后在目标机跑一遍，实测 1/2/4/auto 各档加速比 + 向量一致性校验，直接给出推荐配置（<1.3× 的档位不值得开）。
3. **kill-switch**：任何并行异常自动回退串行重算（不击穿去重链路），配置改 `DEDUP_EMBED_WORKERS=1` 即彻底关闭。

内存预算：**每 worker ~1.6GB**（模型权重 + 运行时）；服务器开启前确认空闲内存 ≥ workers×1.6GB。

### B.3 2️⃣ 增量去重：预嵌入（三个挂点）

核心洞察：`dedup_semantic` 已走 `_embed_cached`，所以增量去重最简形态 = **在用例终态化的时刻提前把向量写入缓存**，主合并点自动全命中——不改 checkpoint 结构、不改判定逻辑。

| 挂点 | 位置 | 场景 |
|---|---|---|
| FP 子轮完成回调 | orchestrator.py `_gen_one_fp`（checkpoint 保存后） | 主场景：每子轮预热，与后续 FP 的 LLM 网络等待错峰 |
| 断点续跑恢复 | orchestrator.py 续跑循环后 | 续跑恢复的历史用例（没经过生成阶段，否则主合并点全量 miss） |
| 合并前统一预热 | orchestrator.py 主去重点前 | 全量兜底/跨 FP 集成两通道不经子轮挂点，统一补齐 |

配套：
- `prewarm_semantic_cache()`（case_dedup.py）：分块 64 条预热，失败静默（纯性能优化，失败只回退"现场计算"旧行为）；
- `count_cache_miss()`（case_dedup.py）：进度消息按 **miss 数**估算（预热后 miss≈0，原按 len(cases)×4s 估算会虚高到"预计 70 分钟"实际 30 秒，观感像卡死）；
- review 改写 `confidence` 不影响命中（缓存 key 是语义投影，排除 confidence——B 方案的 key/文本解耦设计在此复用）。

**等价性验证**（容器内行为测试，fake embed 确定向量）：分批预热 12+8 条 vs 不预热直算 → kept/removed 逐条一致；预热后 miss=0；review 改写 confidence 后 miss 仍=0。数学依据：`dedup_semantic` 判定是贪心保留 + pairwise 比较（`semantic_near_duplicate`：sim + 标题数字集合，无传递闭包），预热只填缓存不改任何判定输入 → 结果必然一致。

### B.4 本轮回归与收益盘点

| 测试 | 结果 |
|---|---|
| unit test_case_dedup + test_dedup_model_isolation | 36 passed |
| phase2_core / html_upload / 3 / 4 / 5 / 6 | 74 / 32 / 91(2 skip) / **188** / 112 / 141，全绿 |
| 等价性行为验证（预热 vs 直算） | kept/removed 一致、miss=0、confidence 改写后仍命中 |

收益（v1.0 规模 1055 条，dev 机形态）：
- D+B（已上线）+ 预热 → 主合并点 embed 从"全量 67min"变"各子轮分摊 + 全命中"，主链路语义去重墙**基本消失**（剩余成本 = 余弦判定 ~分钟级 + 集成通道增量）；
- 服务器叠加分片并行（bench 实测后配置）→ 生成期预热本身再 ÷N；
- 判定逻辑、阈值、序列化口径**零改动**——这是与 E/A 的本质区别。

**未决事项**：真实生成 e2e 回验（需大哥提供需求跑一轮，验 SSE 进度消息合理性 + 主合并点命中率日志）；本批 commit 尚未 push（等回归全绿后按发布纪律处理）。
