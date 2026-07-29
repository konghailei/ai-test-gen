# 09 — 需求分析全链路

> 从原始需求上传 → 解析 → 澄清 → FP 拆分 → 确认 → 历史管理的完整流水线  
> 版本: v2.2 | 日期: 2026-07-21

---

## 1. 全链流程图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          需求分析全链路 (v2.2)                              │
└─────────────────────────────────────────────────────────────────────────────┘

                              ┌──────────────┐
                              │ 上传原始需求  │ (API: upload_requirement)
                              │ (Markdown/   │ (API: upload_requirement_file)
                              │  多文件/JSON) │ (API: upload_requirement_multi)
                              └──────┬───────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ ① ReqAgent 需求解析                                                         │
│   · 格式检测 + 预处理                                                        │
│   · LLM 结构化解析 → 三级递进 SRR JSON                                       │
│   · 自动提取功能点 (FP) + 交互层 SRR  ⭐ v2.0                                │
│                                                                              │
│   产出:                                                                      │
│     req.srr_json = interaction_srr  ← 跨 FP 交互规则/共享约束/契约           │
│     functional_points[]             ← 每个 FP 的内部规则 (sub_srr)           │
│                                                                              │
│   ⭐ v2.0 FP 去重跳过 LLM: 同模块已有 FP 名 → 复制到新需求，不重新生成        │
└──────────────────────┬──────────────────────────────────────────────────────┘
                       │
                       ▼
                 ┌──────────┐
                 │ status = │
                 │  draft   │
                 └────┬─────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ ② ClarifyAgent 需求澄清 (API: get_clarify_questions / submit_clarify_answers) │
│   · 完整性检测 (Checklist 规则)                                              │
│   · 歧义性检测 (规则 + LLM)                                                  │
│   · 遗漏检测 (需求模式库对比)                                                │
│   · KB 自动补全:                                                             │
│     - _detect_baseline_focus() 按问题维度精准返回  ⭐ v2.0                    │
│     - 正常流程→happy_path, 异常→alternate_flows, 数据→data_constraints...    │
│     - KB 答不出 → 生成人工问题 (最多 5 个)                                    │
│   · 最多 3 轮迭代                                                             │
│                                                                              │
│   产出: 细化后的 SRR + 澄清历史记录                                           │
└──────────────────────┬──────────────────────────────────────────────────────┘
                       │
                       ▼
                 ┌──────────────┐
                 │ status =     │
                 │ pending_review│
                 └────┬─────────┘
                      │
            ┌─────────┼─────────┐
            ▼         ▼         ▼
       确认完成   补充重新生成    编辑 SRR
      (confirm) (regenerate) (edit)
            │         │          │
            │         ▼          │
            │   ┌──────────┐    │
            │   │③ 重新分析 │    │
            │   │  Re-parse │    │
            │   │ ────────  │    │
            │   │· 当前SRR  │    │
            │   │  入历史版本│    │
            │   │· 重新LLM  │    │
            │   │  解析     │    │
            │   │· 自动触发 │    │
            │   │  fp_split │ ⭐ v2.2
            │   │  (更新FP  │
            │   │  子SRR)   │    │
            │   └────┬─────┘    │
            │        │           │
            │        ▼           │
            │   pending_review ──┘
            │
            ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ ④ 确认完成 (API: confirm)                                                   │
│   status: pending_review → confirmed                                        │
│   → 解锁后续: 生成测试用例 / 归档                                            │
└──────────────────────┬──────────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ ⑤ FP 管理 (展开详见图 2)                                                    │
│   · FP 审核 (pending_review → approved)                                      │
│   · FP 编辑: 名称 / 多模块选择 ⭐ v2.2 / SRR 字段                            │
│   · FP 重生成: 输入 hint → LLM 重新生成子 SRR                                │
│   · FP 合并: 多选 → LLM 合并为一个                                           │
│   · FP 依赖: 跨需求依赖图谱 + 影响分析                                       │
│   · 重新拆分: 全量重跑 fp_split                                              │
└──────────────────────┬──────────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ ⑥ 历史版本管理 (VERSION HISTORY)  ⭐ v2.0 / v2.2                            │
│                                                                              │
│   ┌─────────────────────────────────────────────┐                           │
│   │ 每次重新分析前，原 SRR 自动快照             │                           │
│   │ → 存在 req.srr_json._version_history[]      │                           │
│   │                                             │                           │
│   │ 版本列表摘要 (listVersions):                │                           │
│   │   标题 / 快照时间 / 触发原因 / 补充说明      │                           │
│   │                                             │                           │
│   │ 查看详情 (getVersion):                      │                           │
│   │   完整 SRR 快照 (渲染为结构化视图)           │                           │
│   │   ⭐ v2.2 修复: 对象字段不再显示 [object]    │                           │
│   │                                             │                           │
│   │ 切换使用 (restoreVersion) ⭐ v2.2:           │                           │
│   │   当前 SRR 先自动快照 → 目标版本覆盖 ←       │                           │
│   │   status → pending_review (需重新确认)       │                           │
│   └─────────────────────────────────────────────┘                           │
└──────────────────────┬──────────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ ⑦ 归档 (ARCHIVE)                                                            │
│   status: confirmed → archived                                              │
│   · SRR 写入知识库 (Milvus + PG) 作为后续需求的基线参考                      │
│   · FP 锁定为只读                                                            │
│   · 入口: 「生成测试用例」页面/需求详情页                                    │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. FP 管理子流程

```
┌──────────────────────────────────────────────────────────────────────┐
│                     FP 管理子流程                                    │
│                                                                      │
│  fp_split (全量重新拆分)                                             │
│    ├─ 查询同模块已有 FP 名 → 跳过 LLM  ⭐ v2.0                       │
│    ├─ LLM 生成新 FP + interaction_srr                                │
│    ├─ 复制已有 FP 到当前需求 (新 ID + 原 sub_srr)                    │
│    └─ FP 列表 = 已有(copy) + 新(LLM)                                 │
│                                                                      │
│  fp_update (编辑 FP)                                                 │
│    ├─ 名称修改 + 模块多选 ⭐ v2.2                                     │
│    └─ 子 SRR 字段级编辑 (FieldEditor)                                │
│                                                                      │
│  fp_regenerate (单个 FP 重生成)                                       │
│    └─ 输入 hint → LLM 重新生成该 FP 的 sub_srr → status = pending    │
│                                                                      │
│  fp_approve (审核通过)  pending → approved                           │
│  fp_merge (合并 FP)     多选 → LLM 合并为一个                          │
│  fp_delete (删除 FP)    级联删除依赖关系                              │
│                                                                      │
│  跨需求能力:                                                          │
│    fp_dependencies GET → FP 间依赖关系                                │
│    fp_dependency_graph GET → 跨需求 FP 依赖图谱                       │
│    fp_impact GET → BFS 影响分析 (3 层)                                │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 3. 双 SRR 分层架构 ⭐ v2.0 核心

```
LLM 一次性产出两份 SRR
    │
    ├── interaction_srr (写入 req.srr_json)    ← 总 SRR = 交互层
    │     ├ cross_fp_flows:        FP 间调用顺序 + 数据契约
    │     ├ shared_business_rules: 跨 ≥2 个 FP 的共享规则
    │     ├ fp_contracts:          FP 间接口约定
    │     ├ global_constraints:    全局性约束
    │     └ acceptance_criteria:   跨 FP 验收标准
    │
    └── functional_points[] (写入 FP 表)        ← FP 内部层
          └── sub_srr:
                ├ flows.happy_path:        FP 内部正常流程步骤
                ├ flows.alternate_flows:   FP 内部异常分支
                ├ business_rules:          FP 专属业务规则
                ├ data_constraints:        FP 专属数据约束
                ├ acceptance_criteria:     FP 专属验收标准
                └ test_hints:              FP 测试提示

原则: 一条规则只在一处定义
  · 单 FP 专属 → sub_srr
  · 多 FP 共享 → interaction_srr
  · 禁止重复
```

---

## 4. 关键 API 端点

| 方法 | 路径 | 功能 | v2.x 变更 |
|------|------|------|-----------|
| POST | `/requirements` | 上传需求 | ReqAgent → interaction_srr + FP |
| GET | `/{id}/clarify/questions` | 获取澄清问题 | v2.0 _detect_baseline_focus 精准补全 |
| POST | `/{id}/clarify/answers` | 提交澄清答案 | |
| POST | `/{id}/regenerate` | 补充重新生成 SRR | v2.2 自动触发 fp_split |
| POST | `/{id}/confirm` | 确认完成 | |
| POST | `/{id}/fp/split` | FP 拆分 | v2.0 FP 去重跳 LLM；v2.0 interaction_srr |
| PUT | `/{id}/fp/{fp_id}` | 编辑 FP | v2.2 modules 多选 |
| GET | `/{id}/versions` | 版本列表 | v2.0 |
| GET | `/{id}/versions/{idx}` | 版本详情 | v2.2 修复 Object 显示 |
| POST | `/{id}/versions/{idx}/restore` | 切换版本 | v2.2 新增 |

---

## 5. 状态机

```
                      upload
                        │
                        ▼
                    ┌───────┐
              ┌─────│ draft │──────────┐
              │     └───┬───┘          │
              │   clarify│   regenerate│
              │         ▼              │
              │  ┌───────────────┐     │
              │  │ pending_review│←────┘
              │  └───┬───────┬───┘
              │      │confirm│regenerate(补充重新生成)
              │      ▼       │      ⭐ v2.2 自动 fp_split
              │  ┌──────────┐│
              │  │confirmed ││
              │  └────┬─────┘│
              │       │      │
              │ archive│     │
              │       ▼      │
              │  ┌──────────┐│
              │  │ archived ││
              │  └──────────┘│
              │              │
              │  restore ⭐v2.2 path:
              │  any status → 先快照当前 → 覆盖为目标版本 → pending_review
              │
              └── 可补充重新生成 (所有非 archived 状态) ⭐ v2.2
```

---

## 6. 今日变更汇总 (v2.2)

| 类别 | 变更 | 文件 |
|------|------|------|
| **架构** | 双 SRR 分层（interaction_srr + FP sub_srr），消除规则重复 | `fp_agent.py`, `req_agent.py`, `requirements.py` |
| **KB** | 基线补全精准化（_detect_baseline_focus 按维度返回） | `clarify_agent.py` |
| **FP** | FP 去重跳过 LLM（同模块已有名 → 复制不重生成） | `req_agent.py`, `requirements.py` |
| **FP** | 重新生成 SRR 后自动 fp_split（之前遗漏 FP 更新） | `requirements.py` |
| **FP** | 模块多选（models 加 modules JSON 字段，前端复选框） | `models.py`, `FunctionalPoints.tsx`, `FPSRRViewer.tsx`, `client.ts`, `types/index.ts` |
| **UI** | 「补充重新生成」按钮放开到所有非归档状态 | `RequirementDetail.tsx` |
| **版本** | 历史版本修复 Object 显示（safeStr 兜底） | `VersionHistoryPanel.tsx` |
| **版本** | 历史版本可直接切换使用（restore 端点 + 「使用此版本」按钮）| `requirements.py`, `VersionHistoryPanel.tsx`, `client.ts` |
| **已知差距** | 生成期 KB 语义召回待增强：GenAgent 轻量 KB 当前为"同产品最新 N 条"，未调用 Milvus 语义召回（详见 docs/11 §5.3） | `gen_agent.py` |

---

## 7. 文档对照

| 本文章节 | 对应架构文档 |
|---------|------------|
| §1 全链流程图 | [00 §2 编排层](./00-architecture-overview.md#2-五层架构全景), [00 §3 核心数据流](./00-architecture-overview.md#3-核心数据流) |
| §2 FP 子流程 | [08 功能点子系统](./08-functional-points.md) |
| §3 双 SRR | [08 §v2.0 双 SRR 分层](./08-functional-points.md#v20-双-srr-分层interaction_srr--fp-sub_srr) |
| §5 状态机 | [00 §9.1 MVP 简化架构](./00-architecture-overview.md#91-mvp-简化架构可行性优化) |
| API 端点 | [08 §API 端点](./08-functional-points.md#api-端点) |

> **维护提示**: 本文档 (09) 应随需求分析相关变更同步更新。架构总览变更更新 doc-00, FP 相关变更更新 doc-08。
