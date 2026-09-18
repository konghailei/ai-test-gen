import { useState, useEffect } from 'react'
import type { ReactNode } from 'react'
import {
  Upload, FileText, Database, FolderTree, Settings, Search,
  BookOpen, ArrowLeft, Workflow, Cpu, GitBranch, ShieldCheck,
  Layers, Zap, Image, TestTube, Server, Activity, ClipboardCheck,
} from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { health } from '../api/client'

interface ManualStep {
  title: string
  desc: string
  img?: string
}

interface ManualSection {
  id: string
  icon: typeof Upload
  title: string
  subtitle: string
  color: string
  features: { label: string; desc: string }[]
  logic: { step: string; detail: string }[]
  config?: { key: string; value: string; note?: string }[]
  diagram?: ReactNode
  steps?: ManualStep[]
  terms?: { term: string; desc: string }[]
}

/* ===================== 架构图组件 ===================== */

// 图1：LangGraph DAG 流水线总览
function DagPipelineDiagram() {
  const node = (x: number, y: number, w: number, h: number, fill: string, stroke: string, title: string, sub: string) => (
    <g key={title}>
      <rect x={x} y={y} width={w} height={h} rx={10} fill={fill} stroke={stroke} strokeWidth={1.5} />
      <text x={x + w / 2} y={y + h / 2 - 4} textAnchor="middle" fontSize={13} fontWeight={600} fill="#1e293b">{title}</text>
      {sub && <text x={x + w / 2} y={y + h / 2 + 13} textAnchor="middle" fontSize={11} fill="#64748b">{sub}</text>}
    </g>
  )
  const arrow = (x1: number, y1: number, x2: number, y2: number, dashed = false) => (
    <line x1={x1} y1={y1} x2={x2} y2={y2} stroke="#94a3b8" strokeWidth={1.5} markerEnd="url(#um-arrow)" strokeDasharray={dashed ? '4 3' : undefined} />
  )
  return (
    <svg viewBox="0 0 680 400" width="100%" className="my-2">
      <defs>
        <marker id="um-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M2 1L8 5L2 9" fill="none" stroke="#94a3b8" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round" />
        </marker>
      </defs>
      {node(60, 40, 150, 44, '#E6F1FB', '#185FA5', 'parse', '需求解析')}
      {node(250, 40, 170, 44, '#EEEDFE', '#534AB7', 'select_channel', '生成准备')}
      {node(60, 140, 150, 44, '#FAEEDA', '#BA7517', 'clarify', '需求澄清(含KB补全)')}
      {node(250, 140, 150, 44, '#FAECE7', '#993C1D', 'gate', '质量门禁')}
      {node(450, 140, 150, 44, '#E1F5EE', '#0F6E56', 'impact', '影响分析')}
      {node(450, 230, 150, 44, '#EAF3DE', '#3B6D11', 'generate', '用例生成')}
      {node(250, 320, 150, 44, '#FBEAF0', '#993556', 'review', '用例审核')}
      {node(60, 320, 100, 44, '#F1EFE8', '#5F5E5A', 'END', '')}
      {arrow(210, 62, 250, 62)}
      {arrow(282, 84, 165, 140)}
      {arrow(335, 84, 325, 140)}
      {arrow(210, 162, 250, 162)}
      {arrow(400, 162, 450, 162)}
      {arrow(250, 162, 212, 162, true)}
      {arrow(525, 184, 525, 230)}
      {arrow(450, 252, 400, 342)}
      {arrow(250, 342, 160, 342)}
      <text x="196" y="104" fontSize={10} fill="#94a3b8">启用澄清</text>
      <text x="348" y="118" fontSize={10} fill="#94a3b8">默认路径</text>
      <text x="422" y="150" fontSize={10} fill="#94a3b8">pass</text>
      <text x="224" y="156" fontSize={10} fill="#94a3b8">retry</text>
    </svg>
  )
}

// 图2：知识库三层存储架构
function KbArchitectureDiagram() {
  const box = (x: number, y: number, w: number, h: number, fill: string, stroke: string, title: string, sub: string) => (
    <g key={title}>
      <rect x={x} y={y} width={w} height={h} rx={10} fill={fill} stroke={stroke} strokeWidth={1.5} />
      <text x={x + w / 2} y={y + h / 2 - 3} textAnchor="middle" fontSize={13} fontWeight={600} fill="#1e293b">{title}</text>
      <text x={x + w / 2} y={y + h / 2 + 13} textAnchor="middle" fontSize={11} fill="#64748b">{sub}</text>
    </g>
  )
  const arrow = (x1: number, y1: number, x2: number, y2: number, dashed = false) => (
    <line x1={x1} y1={y1} x2={x2} y2={y2} stroke="#94a3b8" strokeWidth={1.5} markerEnd="url(#um-arrow)" strokeDasharray={dashed ? '4 3' : undefined} />
  )
  return (
    <svg viewBox="0 0 680 250" width="100%" className="my-2">
      <defs>
        <marker id="um-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M2 1L8 5L2 9" fill="none" stroke="#94a3b8" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round" />
        </marker>
      </defs>
      {box(260, 20, 160, 42, '#E6F1FB', '#185FA5', '用户 / API', '需求上传 · KB 问答')}
      {box(250, 92, 180, 44, '#EEEDFE', '#534AB7', 'Milvus', '向量检索引擎')}
      {box(450, 92, 140, 44, '#FAEEDA', '#BA7517', 'etcd', '元数据')}
      {box(250, 172, 180, 44, '#E1F5EE', '#0F6E56', 'MinIO', 'S3 兼容对象存储')}
      {box(40, 172, 180, 44, '#F1EFE8', '#5F5E5A', 'PostgreSQL', '结构化数据')}
      {arrow(340, 62, 340, 92)}
      {arrow(430, 114, 450, 114)}
      {arrow(340, 136, 340, 172)}
      {arrow(220, 194, 250, 114, true)}
      <text x="282" y="80" fontSize={10} fill="#94a3b8">向量检索</text>
      <text x="446" y="84" fontSize={10} fill="#94a3b8">元数据</text>
      <text x="280" y="158" fontSize={10} fill="#94a3b8">数据文件</text>
      <text x="120" y="160" fontSize={10} fill="#94a3b8">结构化双写</text>
    </svg>
  )
}

// 图3：增量需求基线补全流程
function IncrementalFlowDiagram() {
  const box = (x: number, y: number, w: number, h: number, fill: string, stroke: string, title: string) => (
    <g key={title}>
      <rect x={x} y={y} width={w} height={h} rx={10} fill={fill} stroke={stroke} strokeWidth={1.5} />
      <text x={x + w / 2} y={y + h / 2} textAnchor="middle" fontSize={13} fontWeight={600} fill="#1e293b">{title}</text>
    </g>
  )
  const arrow = (x1: number, y1: number, x2: number, y2: number) => (
    <line x1={x1} y1={y1} x2={x2} y2={y2} stroke="#94a3b8" strokeWidth={1.5} markerEnd="url(#um-arrow)" />
  )
  return (
    <svg viewBox="0 0 680 250" width="100%" className="my-2">
      <defs>
        <marker id="um-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M2 1L8 5L2 9" fill="none" stroke="#94a3b8" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round" />
        </marker>
      </defs>
      {box(40, 30, 170, 44, '#E6F1FB', '#185FA5', 'V1 完整需求（已归档）')}
      {box(300, 30, 160, 44, '#EEEDFE', '#534AB7', 'Milvus 向量库')}
      {box(40, 140, 170, 44, '#FAEEDA', '#BA7517', 'V2 优化需求')}
      {box(300, 140, 180, 44, '#E1F5EE', '#0F6E56', 'ClarifyAgent 基线补全')}
      {box(540, 140, 120, 44, '#EAF3DE', '#3B6D11', '合并 SRR')}
      {arrow(210, 52, 300, 52)}
      {arrow(210, 162, 300, 162)}
      {arrow(380, 74, 380, 140)}
      {arrow(480, 162, 540, 162)}
      <text x="228" y="44" fontSize={10} fill="#94a3b8">归档写入</text>
      <text x="385" y="112" fontSize={10} fill="#94a3b8">KB 检索命中</text>
      <text x="486" y="130" fontSize={10} fill="#94a3b8">写入 _baseline_context</text>
    </svg>
  )
}

// 图：需求分析生命周期（上传 → 解析 → 澄清 → 门禁 → 归档）
function RequirementAnalysisDiagram() {
  const node = (x: number, y: number, w: number, h: number, fill: string, stroke: string, title: string, sub: string) => (
    <g key={title}>
      <rect x={x} y={y} width={w} height={h} rx={10} fill={fill} stroke={stroke} strokeWidth={1.5} />
      <text x={x + w / 2} y={y + h / 2 - 4} textAnchor="middle" fontSize={12.5} fontWeight={600} fill="#1e293b">{title}</text>
      {sub && <text x={x + w / 2} y={y + h / 2 + 13} textAnchor="middle" fontSize={10.5} fill="#64748b">{sub}</text>}
    </g>
  )
  const arrow = (x1: number, y1: number, x2: number, y2: number, dashed = false) => (
    <line x1={x1} y1={y1} x2={x2} y2={y2} stroke="#94a3b8" strokeWidth={1.5} markerEnd="url(#ra-arrow)" strokeDasharray={dashed ? '4 3' : undefined} />
  )
  return (
    <svg viewBox="0 0 680 320" width="100%" className="my-2">
      <defs>
        <marker id="ra-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M2 1L8 5L2 9" fill="none" stroke="#94a3b8" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round" />
        </marker>
      </defs>
      {node(20, 50, 130, 46, '#E6F1FB', '#185FA5', '上传需求', '.md/.docx/.txt 原文')}
      {node(175, 50, 130, 46, '#EEEDFE', '#534AB7', 'ReqAgent 解析', '原始文本 → SRR')}
      {node(330, 50, 135, 46, '#E1F5EE', '#0F6E56', 'ClarifyAgent 澄清', '含知识库基线补全')}
      {node(500, 50, 160, 46, '#FAECE7', '#993C1D', 'QualityGate 门禁', '完整性 / 歧义 / 遗漏')}
      {node(500, 160, 160, 46, '#EAF3DE', '#3B6D11', '归档入库', 'PostgreSQL + Milvus')}
      {node(330, 160, 135, 46, '#F1EFE8', '#5F5E5A', 'END', '需求就绪可生成')}
      {arrow(150, 73, 175, 73)}
      {arrow(305, 73, 330, 73)}
      {arrow(465, 73, 500, 73)}
      {arrow(580, 96, 580, 160)}
      {arrow(500, 183, 465, 183)}
      <polyline points="500,73 460,73 460,290 380,290 380,96" fill="none" stroke="#94a3b8" strokeWidth={1.5} strokeDasharray="4 3" markerEnd="url(#ra-arrow)" />
      <text x="506" y="132" fontSize={10} fill="#0F6E56">通过</text>
      <text x="300" y="306" fontSize={10} fill="#94a3b8">未通过（最多 3 轮回澄清重试）</text>
    </svg>
  )
}

/* ===================== 章节数据 ===================== */

const SECTIONS: ManualSection[] = [
  {
    id: 'architecture',
    icon: Workflow,
    title: '系统架构总览',
    subtitle: 'LangGraph DAG 流水线 + 存储分层',
    color: 'text-slate-600 bg-slate-50',
    steps: [
      { title: '登录系统', desc: '使用默认管理员账号 admin / 888888 登录；登录后可在顶部栏「切换产品」选择当前工作产品。', img: 'login.png' },
      { title: '查看系统仪表盘', desc: '登录后进入「仪表盘」，总览需求数、测试用例数、LLM 配置状态与最近活动，快速掌握系统运行概况。', img: 'dashboard.png' },
    ],
    features: [
      { label: '流水线', desc: '需求分析到用例生成的端到端链路（由 LangGraph 编排）：上传 → 解析(SRR+FP拆分) → 澄清(含知识库基线补全) → 质量门禁 → 功能点审核 → 确认完成 → 影响分析 → 生成(四级去重) → 审核(一致性检查) → 入库' },
      { label: '生成准备', desc: 'select_channel（生成准备）构造本次生成的环节开关：已确认需求跳过生成期澄清（澄清已在需求分析阶段完成），直接进入质量门禁；多类型生成与完整审核始终启用' },
      { label: '质量门禁', desc: 'gate（质量门禁）评估完整性；未通过且可重试则回到 clarify，达到上限强制通过' },
      { label: '功能点审核', desc: '解析后自动拆分功能点(FP)，用户逐个审核通过(approved)后需求变为 pending_review（待确认）' },
      { label: '确认完成', desc: '需求状态 pending_review 时点击「确认完成」变为 confirmed，只有 confirmed 状态可生成用例' },
      { label: '四级去重', desc: '生成阶段四级去重：精确匹配 → 语义去重(cosine>0.92) → 子集检测 → LLM 等价判定' },
      { label: '一致性检查', desc: 'ConsistencyChecker 对比历史生成任务：用例数方差、类型分布 Jaccard 相似度、语义相似度' },
      { label: '向量库时序', desc: '生成阶段不写入向量库（避免污染），审核后由 ReviewAgent 统一写入 Milvus' },
      { label: '存储分层', desc: 'PostgreSQL（关系型数据库）+ Milvus（向量数据库）+ MinIO（对象存储）+ etcd（元数据服务）+ Redis（缓存 + DLQ 死信队列）' },
      { label: 'L1 接入层防护', desc: 'JWT 认证 + RBAC 权限控制（require_admin/require_write 装饰器）+ 基于 IP 的 Token Bucket 限流（默认 60 RPM，超限 429）' },
      { label: 'L2 编排层防护', desc: 'LLM 调用断路器（三状态机：CLOSED/OPEN/HALF_OPEN）+ 指数退避全抖动重试 + 多提供商故障转移' },
      { label: 'L3 Agent 层防护', desc: 'ClarifyLoopGuard 澄清回环上限 3 轮 + ConsistencyChecker 一致性检查 + QualityGate 质量门禁' },
      { label: 'L4 数据层防护', desc: 'KB 静默跳过（KB 不可用时返回空，不阻断主流水线）+ DLQ 死信队列（主流水线异常写入 Redis，支持事后重试）+ 幂等守卫' },
    ],
    logic: [
      {
        step: 'DAG 编排',
        detail: 'LangGraph（LLM 应用编排框架）的 StateGraph 把节点编译为可执行 DAG。需求分析链路：parse（解析+SRR+FP拆分）→ select_channel（生成准备）→ clarify（澄清+KB补全）→ gate（质量门禁）。用例生成链路（需需求 confirmed）：impact（影响分析）→ generate（生成+四级去重）→ review（审核+一致性检查+向量库写入）。select_channel 按是否启用生成期澄清分支（已确认需求恒跳过），gate 按条件分支（通过 / 未通过回 clarify 重试）',
      },
      {
        step: '需求状态机',
        detail: 'draft（草稿，解析中或解析完成）→ pending_review（待确认，所有功能点已审核通过）→ confirmed（已确认，可生成用例）→ archived（已归档，写入知识库）。analyzing（分析中）为过渡态。只有 confirmed 状态的需求可以触发用例生成',
      },
      {
        step: '双写落库',
        detail: '需求归档时同时写入 PostgreSQL（关系型数据库，存结构化字段）与 Milvus（向量数据库，存 1024 维向量 + srr_json 全文），保证知识库检索能拿到完整 SRR 用于基线补全',
      },
      {
        step: '向量化',
        detail: 'bge-large-zh（中文文本向量化模型）将 SRR 文本编码为 1024 维向量，写入 Milvus 的 5 个集合：requirements（需求）/ test_cases（用例）/ patterns（测试模式）/ req_pats（需求模式）/ errors（错误案例）',
      },
      {
        step: '异步任务',
        detail: 'Celery（异步任务队列）处理归档时的向量计算与写入，避免阻塞接口返回。Redis 作为 Celery 的消息代理和缓存层',
      },
    ],
    config: [
      { key: '编排框架', value: 'LangGraph StateGraph', note: '节点 + 条件边编译为可执行图' },
      { key: '向量模型', value: 'bge-large-zh', note: '1024 维，本地部署' },
      { key: '向量后端', value: 'Milvus + MinIO + etcd', note: 'Milvus 计算，MinIO 存数据文件，etcd 存元数据' },
      { key: '异步队列', value: 'Celery + Redis', note: '归档向量计算异步化' },
      { key: '需求状态', value: 'draft / pending_review / confirmed / archived', note: '只有 confirmed 可生成用例' },
      { key: '去重策略', value: '四级（精确/语义/子集/LLM）', note: 'cosine > 0.92 视为语义重复' },
      { key: '限流', value: 'Token Bucket 60 RPM', note: '基于 IP，超限返回 429 + Retry-After' },
      { key: '断路器', value: '三状态机 + 指数退避', note: 'CLOSED/OPEN/HALF_OPEN，LLM 调用专属' },
      { key: 'DLQ 死信队列', value: 'Redis List，上限 10000 条', note: '主流水线 + 异步任务异常写入，TTL 7 天' },
      { key: 'KB 降级', value: '静默跳过 + 人工澄清兜底', note: 'KB 不可用时返回空，不阻断生成' },
    ],
    diagram: <DagPipelineDiagram />,
  },
  {
    id: 'requirements',
    icon: Upload,
    title: '需求分析与管理',
    subtitle: '上传 → 解析(SRR) → 澄清(含KB补全) → 功能点审核 → 确认完成 → 归档',
    color: 'text-blue-600 bg-blue-50',
    steps: [
      { title: '上传需求文档', desc: '在「需求管理」点击「上传需求」，填写需求名称、选择所属项目/版本与关联模块，上传 .md/.docx/.txt 原文后点击「上传并解析」，系统调用 POST /requirements/upload 进入解析 → 澄清 → 质量门禁流程。', img: 'requirements-upload.png' },
      { title: '查看需求列表', desc: '解析完成后在列表中查看需求，支持按项目 / status / priority 筛选与标题模糊搜索。需求状态：draft（草稿）/ pending_review（待确认）/ confirmed（已确认，可生成用例）/ archived（已归档）。', img: 'requirements-list.png' },
      { title: '查看需求详情', desc: '点击需求进入详情页，可查看结构化 SRR、功能点（含审核）、影响分析与追溯矩阵。详情页含多个 Tab：SRR 概览 / 功能点 / 功能点 SRR / 影响分析 / 追溯矩阵。', img: 'requirement-detail.png' },
      { title: '回答澄清问题', desc: '对待补充的需求进入「澄清」页，逐条回答系统基于知识库检索与维度检测生成的问题并提交。系统评估答案覆盖情况（v3.0.1）：充分回答即一轮收敛；跳过视为不关注；模糊/不完整仅对该问题追问。最多 3 轮。', img: 'requirement-clarify.png' },
      { title: '审核功能点', desc: '在需求详情「功能点 SRR」Tab 查看自动拆分的功能点，为每个功能点分配模块后点击「审核通过」。所有功能点审核通过后方可确认需求。', img: 'requirement-fp.png' },
      { title: '确认需求完成', desc: '所有功能点审核通过后，需求状态变为 pending_review（待确认）。在需求详情页点击「确认完成」按钮，需求状态变为 confirmed（已确认），此时可以生成测试用例。', img: 'requirement-detail.png' },
      { title: '归档需求', desc: '需求确认并生成用例后，可归档入库。归档时填写上线时间 go_live_date，同时写入 PostgreSQL 与 Milvus 向量库，供后续检索。', img: 'requirement-detail.png' },
    ],
    features: [
      { label: '上传需求', desc: '调用 POST /requirements/upload 传入 raw_text（原始需求文本，支持 .md / .docx / .txt 原文），系统自动解析为结构化 SRR JSON。' },
      { label: '自动解析', desc: 'ReqAgent（需求解析智能体）调用 LLM 将非结构化文本转为标准化需求格式（SRR JSON），同时自动拆分功能点（FP）。' },
      { label: '知识库补全', desc: 'ClarifyAgent 检索知识库（Milvus 向量库）中相似历史需求，自动补全缺失字段：问答匹配相似度 > 0.40，字段基线补全 > 0.85。' },
      { label: '人工澄清', desc: '系统动态生成需人工确认的问题（知识库查询、缺失维度、字段冲突），用户回答后重新生成 SRR。' },
      { label: '功能点审核', desc: '自动拆分的功能点需逐个审核通过（approved），可分配模块、AI 重生成子 SRR。所有 FP 审核通过后需求变为 pending_review。' },
      { label: '确认完成', desc: '需求状态 pending_review 时，点击「确认完成」变为 confirmed，只有 confirmed 状态的需求可以触发用例生成。' },
      { label: 'XMind 导出', desc: '需求详情页支持导出 XMind 脑图，以总体 SRR 和功能点 SRR 为层级结构展示。' },
      { label: '归档入库', desc: '需求完成后归档，填写上线时间 go_live_date，同时写入 PostgreSQL 与 Milvus，供后续检索。' },
      { label: '修改上线时间', desc: '已归档需求可随时修改上线时间，同步更新 Milvus 元数据，保证版本排序准确。' },
      { label: '搜索筛选', desc: '需求列表支持按 status / priority 筛选 + 标题模糊搜索，快速定位目标需求。' },
    ],
    terms: [
      { term: 'SRR', desc: 'Structured Requirement Representation（结构化需求表示）——系统内部统一的需求 JSON 格式，是需求解析、澄清、生成用例的共同载体。' },
      { term: 'FP', desc: 'Functional Point（功能点）——从原始需求文档按业务能力粒度拆分的子单元，每个 FP 含独立的子 SRR。' },
      { term: 'ReqAgent', desc: '需求解析智能体——负责把原始需求文本解析为 SRR JSON 并拆分功能点。' },
      { term: 'ClarifyAgent', desc: '需求澄清智能体——检测需求缺失 / 歧义 / 遗漏，结合知识库补全并生成澄清问题。' },
      { term: 'QualityGate', desc: '质量门禁——对澄清后的 SRR 做最终质量检查，达标才允许进入下一阶段。' },
      { term: 'Milvus', desc: '向量数据库——存储需求的向量化表示（Embedding），支撑语义检索与基线补全。' },
      { term: 'bge-large-zh', desc: '中文文本向量化模型——将 SRR 文本编码为 1024 维向量。' },
      { term: 'PostgreSQL', desc: '关系型数据库——存储需求 / 项目 / 模块 / 用户等结构化数据。' },
      { term: 'Celery', desc: '异步任务队列——归档时异步计算向量并写入 Milvus，避免阻塞接口返回。' },
      { term: 'product_id', desc: '产品 ID——需求归属的产品标识，用于知识库隔离（跨产品不互相补全）。' },
      { term: 'status', desc: '需求状态：draft（草稿）/ pending_review（待确认）/ confirmed（已确认，可生成用例）/ archived（已归档）。analyzing（分析中）为过渡态。' },
    ],
    logic: [
      {
        step: '1. 上传 → SRR 生成',
        detail: '用户提交 raw_text → POST /requirements/upload → ReqAgent.parse() 调用 LLM 将文本解析为 SRR JSON，含 overview / modules / boundary_conditions / happy_path / alternate_flows / business_rules / data_constraints / preconditions 等字段。同时自动拆分功能点（FP）。',
      },
      {
        step: '2. 知识库补全',
        detail: 'ClarifyAgent 调用 vector_store.search_similar_requirements() 检索 Milvus requirements 集合（仅 status=archived 已归档需求）→ 问答余弦相似度 > 0.40 保留 → LLM 二次校验 _verify_kb_answers() 过滤误匹配 → 命中的 baseline 块按字段基线补全阈值 > 0.85 自动补全到 SRR。',
      },
      {
        step: '3. 澄清问题生成',
        detail: '动态分析当前 SRR：① 知识库检索到的缺失字段 ② requirement_patterns 检测的缺失维度 ③ 字段冲突 → 经 LLM 过滤 → 生成 input_type 为 text / select / multi_select 的问题 → 用户回答 → POST /requirements/{id}/clarify 重新生成 SRR。',
      },
      {
        step: '3.1 多轮澄清收敛（v3.0.1）',
        detail: '提交答案后不再从零生成新问题，而是评估上一轮答案的覆盖情况（eval_answer_coverage）：答案为空（跳过）→ 视为不关注不再追问；答案充分覆盖问题核心 → 收敛；答案模糊 / 不完整 / 答非所问 → 仅对该问题追问并附追问理由（_followup_reason）。规则上**禁止发散**，正常 1 轮正确回答即完全收敛，最多 3 轮。',
      },
      {
        step: '4. 功能点拆分与审核',
        detail: 'ReqAgent._fp_extract_from_raw 在 SRR 解析后自动按业务能力粒度拆分（单功能 1-2 个 FP，复杂需求 6-12 个）→ 每个 FP 初始 pending_review → 用户在需求详情「功能点 SRR」Tab 分配模块 + 审核通过（approved）→ 所有 FP approved 后需求变为 pending_review（待确认）',
      },
      {
        step: '5. 确认需求完成',
        detail: '需求状态 pending_review 时 → POST /requirements/{id}/confirm → status 变为 confirmed → 只有 confirmed 状态的需求可以触发用例生成。前置条件：所有功能点已审核通过（approved）',
      },
      {
        step: '6. 重新生成',
        detail: 'POST /requirements/{id}/regenerate 传入 extra_notes 重新解析 → 操作记录存入 srr_json._clarify_history，跨重新解析 / 归档 / 重新生成永久保留。AI 重生成功能点 SRR 后状态回退 pending_review 需重新审核。',
      },
      {
        step: '7. 归档入库（异步双写）',
        detail: 'POST /requirements/{id}/archive 接收 go_live_date（YYYY-MM-DDTHH:MM）→ status=archived → 派发 Celery 计算 Embedding 并写入 Milvus 元数据：{product_id, modules, priority, status, go_live_date, project_version} → 接口即时返回，Celery Worker 异步完成向量写入。',
      },
    ],
    config: [
      { key: 'LLM 供应商', value: '系统设置 → LLM 配置', note: '支持 DeepSeek / OpenAI / 通义千问等多供应商主备' },
      { key: '解析提示词', value: '提示词管理 → req_agent', note: '可自定义解析规则和输出格式' },
      { key: 'KB 相似度阈值', value: '问答 0.40 / 基线 0.85', note: '问答为问句→文档；基线补全是字段→已归档 SRR 精确匹配' },
      { key: '澄清历史', value: 'srr_json._clarify_history', note: 'SRR JSON 内字段，永久保留跨操作不丢失' },
      { key: '需求状态', value: 'draft / pending_review / confirmed / archived', note: '草稿 / 待确认 / 已确认(可生成) / 已归档' },
      { key: 'FP 状态', value: 'pending_review / approved / archived', note: '待审核 / 已通过 / 已归档' },
      { key: '生成前置', value: 'confirmed', note: '只有 confirmed 状态的需求可生成用例' },
    ],
    diagram: <RequirementAnalysisDiagram />,
  },
  {
    id: 'incremental',
    icon: GitBranch,
    title: '增量需求与基线补全',
    subtitle: '优化需求参考历史版本自动补全',
    color: 'text-indigo-600 bg-indigo-50',
    steps: [
      { title: '先归档 V1 完整需求', desc: '先上传并归档原始完整需求（如「登录」），使其写入 Milvus，成为后续增量检索的基线来源。', img: 'requirements-list.png' },
      { title: '上传 V2 优化需求', desc: '上传仅描述增量优化的 V2 需求（如「登录功能优化需求 V2.0：增加图形验证码」），系统自动判定为增量需求并检索 V1。', img: 'requirement-detail.png' },
      { title: '确认基线自动补全', desc: '在需求详情查看自动继承的 happy_path / business_rules / data_constraints 等字段（写入 _baseline_context），无需人工重复录入。', img: 'requirement-detail.png' },
    ],
    features: [
      { label: '增量识别', desc: '需求文本含"本文档仅描述""完整上下文请参见"等信号 → _looks_like_incremental 判定为增量需求' },
      { label: '历史检索', desc: 'ClarifyAgent 在同 product_id 下检索已归档的历史需求，作为基线来源' },
      { label: '基线补全', desc: '缺失字段（happy_path/business_rules/data_constraints 等）从 V1 对应块提取，相似度 > 0.85 自动填充到 _baseline_context' },
      { label: '产品隔离', desc: '检索严格按 product_id 过滤，跨产品需求不会互相补全（兜底 PROD-DEFAULT）' },
    ],
    logic: [
      {
        step: '上传顺序（关键）',
        detail: '必须先 POST /requirements/upload 传入 V1 完整需求（解析后已写入 Milvus）→ 再传 V2 优化需求。V2 上传时 ClarifyAgent 才能检索到 V1 并触发基线补全',
      },
      {
        step: '增量标记',
        detail: 'ReqAgent._looks_like_incremental 命中启发式（"仅描述增量优化内容""完整上下文请参见 REQ-xxxx"）→ srr["_is_incremental"]=true，并生成 _baseline_gaps 描述缺失的字段',
      },
      {
        step: '基线查询生成',
        detail: '_generate_kb_queries 针对 _baseline_gaps 生成 baseline.* 查询，_is_baseline 标记透传给 LLM；命中历史 SRR 后 _extract_baseline_block 提取对应字段块',
      },
      {
        step: '合并到 V2',
        detail: '提取的基线块写入 V2 SRR 的 _baseline_context 字段，使优化需求自动继承完整上下文，无需人工重复录入',
      },
    ],
    config: [
      { key: '基线补全阈值', value: '0.85', note: '字段→已归档 SRR 匹配，比问答更精确' },
      { key: '增量信号', value: '"仅描述增量" / "请参见"', note: '命中即判定为增量需求' },
      { key: '产品隔离', value: 'product_id 强制过滤', note: '兜底 PROD-DEFAULT，跨产品不补全' },
    ],
    diagram: <IncrementalFlowDiagram />,
  },
  {
    id: 'generate',
    icon: FileText,
    title: '测试用例生成',
    subtitle: 'SRR → 去重 → 多维用例 → 一致性检查 → 审核 → 入库',
    color: 'text-green-600 bg-green-50',
    steps: [
      { title: '前置：确认需求', desc: '需求必须先经过「功能点审核」+「确认完成」(confirmed) 才能触发生成。在需求详情页审核所有功能点(approved)后，点击「确认完成」按钮，需求状态变为 confirmed。', img: 'requirement-detail.png' },
      { title: '进入用例生成', desc: '在「测试用例」页点击「生成用例」按钮，搜索并选择已确认(confirmed)的需求，系统基于 SRR 与测试模式 SSE 流式生成多维用例（功能/边界/异常/安全/性能）。', img: 'generate.png' },
      { title: '实时查看生成进度', desc: 'SSE 流式推送进度（百分比 + 阶段消息），生成中可看到当前阶段（解析SRR→检索模式→生成用例→去重→审核→一致性检查→入库）。', img: 'generate.png' },
      { title: '查看生成结果与质量评分', desc: '生成完成后在「测试用例」页查看用例，ReviewAgent 对每条用例评分（完整度/明确性/可执行性），置信度 < 0.6 的用例标记 rejected，≥ 0.6 标记 reviewed。', img: 'testcases.png' },
      { title: '在任务管理查看统计', desc: '生成任务可在「任务管理」查看状态、用例数、类型分布、审核通过/拦截数、平均置信度、一致性检查结果、耗时等统计信息。', img: 'tasks.png' },
    ],
    features: [
      { label: '前置条件', desc: '需求状态必须为 confirmed（确认完成）才能生成。前置链路：上传 → 解析 → 澄清 → 功能点审核(approved) → 确认完成(confirmed)' },
      { label: 'SSE 流式生成', desc: 'POST /generate 以 Server-Sent Events 流式推送进度，前端实时渲染阶段消息和百分比，无需阻塞等待' },
      { label: '多维用例', desc: 'GenAgent 生成 5 种类型用例：functional（功能）/ boundary（边界）/ exception（异常）/ security（安全）/ performance（性能）' },
      { label: '四级去重', desc: '精确匹配 → 语义去重(cosine > 0.92) → 子集检测 → LLM 等价判定，确保用例无冗余' },
      { label: '一致性检查', desc: 'ConsistencyChecker 对比历史生成任务：用例数方差、类型分布 Jaccard 相似度、语义相似度，异常时标记 is_inconsistent=true' },
      { label: '向量库时序', desc: '生成阶段不写入向量库（避免污染），审核后由 ReviewAgent 统一写入 Milvus test_cases 集合' },
      { label: '审核评分', desc: 'ReviewAgent 对每条用例评分（完整度/明确性/可执行性），置信度 < 0.6 标记 rejected，≥ 0.6 标记 reviewed' },
      { label: '影响分析', desc: '优化需求场景下，ImpactAnalysisAgent 基于模块依赖图谱 BFS 遍历识别受影响模块并推荐回归用例' },
      { label: '格式输出', desc: '支持 UTCF JSON / Markdown / Gherkin / pytest 多格式导出，XMind 格式支持列表视图与脑图编辑器视图' },
    ],
    logic: [
      {
        step: '1. SRR 加载与生成准备',
        detail: '从数据库加载已确认需求的 SRR JSON → select_channel（生成准备）构造环节开关：已确认需求跳过生成期澄清（澄清已在需求分析阶段完成），直接进入质量门禁；影响分析仅优化需求启用。需求分析阶段的 SRR 已包含知识库基线补全的上下文，生成阶段不再单独检索知识库',
      },
      {
        step: '2. 测试模式检索',
        detail: '从 Milvus patterns 集合检索与当前需求匹配的测试模式（种子模式 8 种：CRUD / 状态机 / 认证 / 输入校验 / 分页 / 并发 / 异步回调 / 数据一致性）→ GenAgent 参考模式的 test_dimensions 和 typical_cases 生成更贴合规范的用例',
      },
      {
        step: '3. GenAgent 生成用例',
        detail: 'GenAgent.generate() 基于 SRR JSON + 测试模式 → LLM 逐条生成用例（{title, type, priority, description, preconditions, steps, expected, tags, test_data, assertions}）→ 5 种类型：functional / boundary / exception / security / performance',
      },
      {
        step: '4. 四级去重策略',
        detail: 'Level 1 精确匹配（标题+步骤完全相同）→ Level 2 语义去重（bge-large-zh 向量化后 cosine similarity > 0.92 视为重复）→ Level 3 子集检测（一条用例的步骤是另一条的子集）→ Level 4 LLM 等价判定（让 LLM 判断两条用例是否测试同一场景）',
      },
      {
        step: '5. ReviewAgent 审核评分',
        detail: 'ReviewAgent 对每条用例评分（完整度 / 明确性 / 可执行性）→ 计算综合置信度 confidence → 置信度 < 0.6 标记 status=rejected，≥ 0.6 标记 status=reviewed → 生成 review_report（passed_cases / blocked_cases / avg_confidence）',
      },
      {
        step: '6. ConsistencyChecker 一致性检查',
        detail: '查询同一需求下历史 status=completed 的生成任务（排除当前任务和失败任务）→ 对比用例数量方差、类型分布 Jaccard 相似度、语义相似度 → 异常时标记 is_inconsistent=true → 结果存入 result_json.consistency_inconsistent',
      },
      {
        step: '7. 向量库写入（审核后统一写入）',
        detail: '生成阶段不写入向量库（避免低质量用例污染知识库）→ ReviewAgent 审核完成后，将 reviewed/approved 状态的用例统一写入 Milvus test_cases 集合 → 低置信度(rejected)用例先写入 PostgreSQL test_cases 表（status=rejected）再写入 error_cases 表',
      },
      {
        step: '8. 统计信息保存',
        detail: '生成完成后将统计信息写入 gen_tasks.result_json：case_count / type_dist / case_titles / review_passed / review_blocked / avg_confidence / consistency_inconsistent / duration_ms → 供下次生成时一致性检查对比使用',
      },
      {
        step: '9. 影响分析（优化需求场景）',
        detail: 'ImpactAnalysisAgent 基于模块依赖图谱（dependency_graph 表）BFS 遍历：直接模块（depth=1）→ 间接模块（depth=2+）→ 查找关联需求 → 推荐回归测试用例 → 前端可视化展示影响范围',
      },
    ],
    config: [
      { key: '生成 Prompt', value: '提示词管理 → gen_agent', note: '控制用例维度、格式、数量' },
      { key: '审核 Prompt', value: '提示词管理 → review_agent', note: '质量评分规则与置信度计算' },
      { key: '影响分析 Prompt', value: '提示词管理 → impact_agent', note: 'BFS 遍历深度和推荐策略' },
      { key: '去重阈值', value: 'cosine > 0.92', note: '语义去重相似度阈值' },
      { key: '置信度阈值', value: '0.6', note: '< 0.6 标记 rejected，≥ 0.6 标记 reviewed' },
      { key: '一致性检查', value: 'Jaccard > 0.5 + 数量方差 < 30%', note: '异常判定条件' },
    ],
  },
  {
    id: 'knowledge',
    icon: Database,
    title: '知识库',
    subtitle: 'Milvus 向量检索 + PostgreSQL 结构化存储',
    color: 'text-purple-600 bg-purple-50',
    steps: [
      { title: '查看知识库', desc: '在「知识库」查看已归档需求，绿色「已归档」= PostgreSQL 已存储，蓝色「已向量化」= Milvus 已索引；支持标题搜索与向量化状态筛选。', img: 'knowledge.png' },
      { title: '知识问答', desc: '在「知识问答」输入自然语言问题，系统基于 Milvus 检索 + LLM 流式输出回答，支持连续追问；可配置知识库专属 LLM。', img: 'knowledge-qa.png' },
    ],
    features: [
      { label: '归档入库', desc: '需求归档后写入 Milvus 向量库，同时 PostgreSQL 保留结构化数据，供后续检索增强' },
      { label: '知识问答', desc: '基于知识库的自然语言问答，支持 SSE 流式输出和追问' },
      { label: '状态指示', desc: '绿色"已归档"= PostgreSQL 已存储；蓝色"已向量化"= Milvus 已索引' },
      { label: '检索规则', desc: '同一功能检索出多个需求时，按上线时间靠后的为准（最新版本优先）' },
      { label: '版本标注', desc: '检索结果中标注来源项目的版本号，便于识别需求变更来源' },
      { label: '产品隔离', desc: '所有 add_* 方法强制写入 product_id，检索按 product_id 过滤，跨产品不串扰' },
      { label: 'KB 专属 LLM', desc: '知识库页面可配置专属 LLM 供应商，与主 LLM 独立，优化问答成本与质量' },
      { label: '搜索筛选', desc: 'KB 条目列表支持标题模糊搜索 + 向量化状态筛选（已向量化/未向量化）' },
      { label: '修改上线时间', desc: '每条已归档需求可点击日历图标修改上线时间，同步更新 Milvus 元数据' },
    ],
    logic: [
      {
        step: '系统架构',
        detail: 'Milvus（向量检索引擎）+ MinIO（Milvus 的 S3 兼容存储后端，存储向量数据文件）+ etcd（Milvus 元数据：集合结构、段信息、节点状态）+ PostgreSQL（结构化数据：需求/项目/模块/用户/模式）。MinIO/etcd 是 Milvus 的内部依赖，向量检索由 Milvus 提供',
      },
      {
        step: '归档入库流程（Celery 异步双写）',
        detail: '需求归档 → API 即时写入 PostgreSQL 并设置 status=archived → 派发 Celery 异步任务 → bge-large-zh 将 SRR 文本编码为 1024 维向量 → vector_store.add_requirement() 写入 Milvus requirements 集合 → 元数据含 product_id / modules / priority / status / go_live_date / project_version；Celery 不可用时降级为同步写入',
      },
      {
        step: '知识问答 SSE 流式',
        detail: 'POST /knowledge-qa/ask → bge-large-zh 编码问题 → Milvus 检索 top-K 相似需求 → 按 go_live_date 降序 + 相似度降序排序 → _build_qa_prompt() 组装上下文（含版本+日期标签）→ LLM 流式输出回答 → 前端 SSE 解析（lineBuffer 模式处理跨 chunk 的换行）',
      },
      {
        step: '去重规则',
        detail: '同一功能检索出多个需求时，按 go_live_date 降序排序（上线时间靠后的优先）→ answer_text 标注"（v2.1, 上线: 2026-07-11, 相似度 0.55）" → LLM 提示"以上线时间靠后的最新版本为准"',
      },
      {
        step: '双状态指示',
        detail: '绿色"已归档"= PostgreSQL requirements 表 status=archived；蓝色"已向量化"= Milvus 中存在该 req_id 的向量。琥珀色"未向量化"= 已归档但 Milvus 写入失败（kb_error 字段返回错误信息）',
      },
    ],
    config: [
      { key: 'Embedding 模型', value: 'bge-large-zh', note: '本地部署，基础镜像内置 /models 目录' },
      { key: 'Milvus 集合', value: 'requirements / test_cases / patterns / req_pats / errors', note: '共 5 个独立集合' },
      { key: 'MinIO + etcd', value: 'Milvus 存储后端', note: 'S3 兼容对象存储 + 元数据，非独立向量库' },
      { key: '诊断端点', value: 'GET /knowledge/diagnostics', note: '管理员可查看连接状态、集合数量' },
    ],
    diagram: <KbArchitectureDiagram />,
  },
  {
    id: 'correction_review',
    icon: ClipboardCheck,
    title: '纠正审核',
    subtitle: '人工纠错审核 + 部分纠正 + 模块筛选',
    color: 'text-rose-600 bg-rose-50',
    features: [
      { label: '纠正审核流程', desc: '用户在「知识问答」页提交的人工纠正内容不会直接进入向量库，而是进入待审核状态。审核员在「纠正审核」页面按模块和状态筛选，审核通过后纠正内容归档到向量库（支持整篇需求或单个功能点部分纠正），审核拒绝则不进入向量库。' },
      { label: '颗粒度选择', desc: '纠正支持两种颗粒度：整篇需求（requirement）替换整条需求向量；单个功能点（functional_point）仅替换该 FP 的向量，不影响其他功能点（部分纠正）。' },
      { label: '模块筛选', desc: '审核列表支持按关联模块筛选，快速定位待审核的纠正内容；同时支持按状态（待审核/已通过/已拒绝/全部）筛选。' },
      { label: '部分纠正机制', desc: '当向量库中某条需求描述了多个功能点，但只有其中一个功能点错误时，可选择"单个功能点"颗粒度纠正。审核通过后系统仅删除该FP的向量并重新嵌入纠正内容，不影响其他功能点的向量数据。' },
    ],
    steps: [
      { title: '纠正审核概述', desc: '用户在「知识问答」页提交的人工纠正内容不会直接进入向量库，而是进入待审核状态。审核员在「纠正审核」页面按模块和状态筛选，审核通过后纠正内容归档到向量库（支持整篇需求或单个功能点部分纠正），审核拒绝则不进入向量库。', img: 'correction-review.png' },
      { title: '提交纠正', desc: '用户在「知识问答」页点击纠错按钮 → 选择颗粒度（整篇需求/单个功能点）→ 填写关联模块和功能点ID（FP颗粒度时）→ 编辑纠正内容 → 点击"提交审核" → 状态变为 pending_review。', img: 'correction-review.png' },
      { title: '审核通过', desc: '点击"审核通过" → 填写审核备注（可选）→ 确认 → 系统根据颗粒度执行归档：整篇需求归档为新需求并嵌入向量库；单个功能点删除原FP向量并重新嵌入纠正内容（部分纠正）→ 状态变为 approved。', img: 'correction-review.png' },
      { title: '审核拒绝', desc: '点击"拒绝" → 填写拒绝原因（建议填写）→ 确认 → 纠正内容不进入向量库 → 状态变为 rejected。', img: 'correction-review.png' },
      { title: '部分纠正', desc: '在纠错弹窗中选择"单个功能点" → 填写功能点ID（如 FP-xxx）→ 编辑纠正内容。审核通过后系统调用 vector_store.delete_requirement(fp_id) 删除原FP向量 → vector_store.add_requirement(fp_id, fp_srr, metadata) 重新嵌入纠正内容，不影响其他功能点。', img: 'correction-review.png' },
    ],
    logic: [
      {
        step: '提交纠正',
        detail: '用户在「知识问答」页点击纠错按钮 → 选择颗粒度（整篇需求/单个功能点）→ 填写关联模块和功能点ID（FP颗粒度时）→ 编辑纠正内容 → 点击"提交审核" → 状态变为 pending_review',
      },
      {
        step: '查看审核列表',
        detail: '审核员进入「纠正审核」页面 → 按状态（待审核/已通过/已拒绝/全部）和模块筛选 → 查看原问题、原回答、纠正内容、纠正说明',
      },
      {
        step: '审核通过',
        detail: '点击"审核通过" → 填写审核备注（可选）→ 确认 → 系统根据颗粒度执行归档：整篇需求归档为新需求并嵌入向量库；单个功能点删除原FP向量并重新嵌入纠正内容（部分纠正）→ 状态变为 approved',
      },
      {
        step: '审核拒绝',
        detail: '点击"拒绝" → 填写拒绝原因（建议填写）→ 确认 → 纠正内容不进入向量库 → 状态变为 rejected',
      },
      {
        step: '部分纠正 - 选择颗粒度',
        detail: '在纠错弹窗中选择"单个功能点" → 填写功能点ID（如 FP-xxx）→ 编辑纠正内容',
      },
      {
        step: '部分纠正 - 审核通过执行',
        detail: '系统调用 vector_store.delete_requirement(fp_id) 删除原FP向量 → vector_store.add_requirement(fp_id, fp_srr, metadata) 重新嵌入纠正内容',
      },
    ],
    config: [
      { key: '纠正状态', value: 'none / pending_review / approved / rejected', note: '默认 none，提交后 pending_review' },
      { key: '颗粒度类型', value: 'requirement / functional_point', note: '整篇需求 / 单个功能点' },
      { key: '关联模块', value: '字符串', note: '用于按模块筛选审核列表' },
      { key: '审核权限', value: 'require_write', note: '审核通过/拒绝需要写权限' },
      { key: '审核人', value: '当前登录用户', note: '审核时记录 reviewed_by' },
    ],
    terms: [
      { term: 'pending_review', desc: '待审核状态，纠正内容已提交等待审核员审核' },
      { term: 'approved', desc: '已通过，纠正内容已归档到向量知识库' },
      { term: 'rejected', desc: '已拒绝，纠正内容未进入向量知识库' },
      { term: '部分纠正', desc: '仅替换单个功能点的向量数据，不影响其他功能点' },
      { term: '颗粒度', desc: '纠正的范围：整篇需求或单个功能点' },
    ],
  },
  {
    id: 'projects',
    icon: FolderTree,
    title: '项目与模块',
    subtitle: '标签页管理 + 版本号 + 依赖图谱',
    color: 'text-amber-600 bg-amber-50',
    steps: [
      { title: '管理项目与模块', desc: '在「项目与模块」分标签页管理项目（含语义化版本号，如 1.0 / 2.1.0）与功能模块，需求可关联多个模块。', img: 'projects-modules.png' },
      { title: '查看依赖图谱', desc: '在「依赖图谱」可视化模块 / 功能点依赖关系，支持拖拽编辑连线，并基于 BFS 进行影响分析。', img: 'dependency-graph.png' },
    ],
    features: [
      { label: '项目管理', desc: '按产品管理项目，每个项目含版本号（如 1.0、2.1.0），用于 KB 检索版本溯源' },
      { label: '模块管理', desc: '管理产品下的功能模块，需求可关联多个模块' },
      { label: '标签页切换', desc: '项目与模块分标签页展示，各自独立检索' },
      { label: '依赖图谱', desc: '可视化模块间依赖关系，支持拖拽编辑连线和 BFS 影响分析' },
      { label: '产品隔离', desc: '项目/需求均归属 product_id，KB 检索与用例生成按产品过滤' },
    ],
    logic: [
      {
        step: '标签页 + 独立搜索',
        detail: 'ProjectsModules.tsx 使用 activeTab 状态（"project" | "module"）切换显示 → projectSearch / moduleSearch 两个独立搜索状态 → useMemo 过滤：项目按 name/description/version 匹配，模块按 name/description 匹配 → 切换 Tab 时搜索互不影响',
      },
      {
        step: '项目版本号',
        detail: '创建项目时填写版本号（默认 1.0）→ 存入 Project.version 列（String(32)）→ 需求归档时查询关联项目版本 → 写入 Milvus 元数据 project_version → KB 检索结果标注"v{版本号}"，便于识别哪个版本新增或变更了需求',
      },
      {
        step: '依赖图谱',
        detail: 'DependencyGraph 页面可视化模块依赖关系 → 支持拖拽编辑连线 → 存入 module_dependencies 表 → ImpactAgent BFS 遍历该图谱计算影响范围（直接模块 depth=1，间接模块 depth=2+）',
      },
    ],
    config: [
      { key: '版本号格式', value: '语义化版本（如 1.0、2.1.0）', note: 'String(32)，默认 1.0' },
      { key: '版本号用途', value: 'KB 检索版本溯源', note: '归档时写入 Milvus project_version 字段' },
      { key: '产品隔离', value: 'product_id 全链路透传', note: 'add_* 方法强制写入，兜底 PROD-DEFAULT' },
    ],
  },
  {
    id: 'admin',
    icon: Settings,
    title: '管理后台',
    subtitle: '产品 / 用户 / 角色 / LLM / 提示词 / KB 诊断',
    color: 'text-red-600 bg-red-50',
    steps: [
      { title: '管理产品 / 用户 / 角色', desc: '在管理后台维护产品（可见性）、用户账号与角色权限，实现 RBAC 最小化授权。', img: 'admin-products.png' },
      { title: '配置 LLM 与提示词', desc: '在「系统设置」配置多供应商 LLM（主备模式）；在「提示词管理」维护 6 个 Agent 的 Prompt 模板，支持恢复默认。', img: 'admin-settings.png' },
      { title: '查看知识库诊断', desc: '在管理后台「知识库」查看 Milvus 连接状态、各集合实体数与 Embedding 模型状态，快速定位 KB 问题。', img: 'admin-kb.png' },
    ],
    features: [
      { label: '产品管理', desc: '创建/删除产品，配置产品可见性（指定用户可见）' },
      { label: '用户管理', desc: '创建用户、分配角色、管理权限' },
      { label: '角色管理', desc: '自定义角色和权限组合' },
      { label: '系统设置', desc: '配置 LLM 供应商、API Key、模型名称；配置自动持久化到数据库' },
      { label: '提示词管理', desc: '管理各 Agent 的 Prompt 模板，支持版本控制' },
      { label: '知识库管理', desc: '管理员可查看向量库诊断信息（Milvus 连接状态、集合数量）' },
      { label: 'KB 专属 LLM', desc: '可为知识库问答配置独立 LLM 供应商，与主 LLM 隔离，优化问答成本与质量' },
    ],
    logic: [
      {
        step: 'RBAC 权限模型',
        detail: 'User → Role → menus 权限列表。admin 角色关联全部菜单（DEFAULT_MENUS），user 角色关联基础菜单（dashboard/requirements/generate/testcases/knowledge/knowledge_qa/projects_modules）。Sidebar 根据 permissions 过滤导航项',
      },
      {
        step: 'LLM 多供应商配置',
        detail: 'Settings 页面支持配置多个 LLM 供应商（DeepSeek/OpenAI/通义千问等）→ 持久化到 PostgreSQL llm_providers 表 → 主供应商（is_primary=True）用于所有 Agent 调用 → 支持测试连接（返回延迟和状态）→ 备用供应商可手动切换',
      },
      {
        step: '提示词版本控制',
        detail: 'PromptManager 管理 6 个模块的 Prompt 模板（req_agent / clarify_agent / gen_agent / review_agent / impact_agent / knowledge_qa）→ 支持编辑、保存、恢复默认 → 修改后即时生效（下次 Agent 调用使用新 Prompt）→ knowledge_qa 模块含 QA_SYSTEM_PROMPT（主问答）和 CHAT_SYSTEM_PROMPT（追问）',
      },
      {
        step: 'KB 诊断面板',
        detail: 'AdminKnowledgeBase 页面调用 GET /knowledge/diagnostics → 返回 Milvus 连接状态（connected）、Embedding 模型状态（model_ready）、各集合 num_entities、架构说明（Milvus + MinIO + etcd + PostgreSQL 三层）→ 管理员可快速定位 KB 问题',
      },
    ],
    config: [
      { key: '默认管理员', value: 'admin / 888888', note: '首次启动 seed_admin_user() 创建' },
      { key: 'LLM 配置', value: '管理后台 → 系统设置', note: '多供应商 + 主备模式' },
      { key: 'Prompt 管理', value: '管理后台 → 提示词管理', note: '6 个模块模板，支持恢复默认' },
      { key: 'KB LLM 配置', value: '知识库页面 → KB LLM 选择器', note: 'GET/PUT /knowledge/kb-llm' },
    ],
  },
  {
    id: 'kb-rules',
    icon: Search,
    title: 'KB 检索规则',
    subtitle: '混合检索 + 阈值 + 二次校验 + 版本去重',
    color: 'text-indigo-600 bg-indigo-50',
    steps: [
      { title: '查看检索诊断面板', desc: '管理员在管理后台「知识库」诊断面板查看混合检索（向量 + BM25 + RRF + 重排）状态、集合数量与元数据，验证检索链路健康。', img: 'admin-kb.png' },
      { title: '理解检索规则', desc: '同一功能检索出多个需求时以上线时间靠后为准；问答相似度阈值 0.40、基线补全阈值 0.85；仅检索 status=archived；向量 / BM25 互为降级。', img: 'admin-kb.png' },
    ],
    features: [
      { label: '上线时间优先', desc: '当知识库中同一功能检索出两个内容不同的需求时，以上线时间靠后的为准' },
      { label: '版本溯源', desc: '检索结果标注来源项目版本号，清晰区分哪个版本新增或变更了需求' },
      { label: '问答相似度阈值', desc: '问句→文档语义匹配阈值为 0.40（bge-large-zh），低于此值不采纳' },
      { label: '基线补全阈值', desc: '字段→已归档 SRR 精确匹配阈值为 0.85，用于增量需求基线补全' },
      { label: '混合检索', desc: 'Milvus 向量检索 + PostgreSQL BM25 关键词检索 → RRF 融合 → CrossEncoder 重排，兼顾语义和精确召回' },
      { label: '二次校验', desc: 'LLM 对 KB 检索结果进行二次校验，过滤误匹配' },
      { label: '状态过滤', desc: '仅检索 status=archived 的已归档需求，草稿状态不参与检索' },
      { label: '降级策略', desc: 'KB 不可用时静默跳过、正常生成；向量/BM25 互为降级，重排失败回退 RRF 排序' },
    ],
    logic: [
      {
        step: '混合检索（向量 + BM25 + RRF + 重排）',
        detail: 'bge-large-zh 模型（本地部署）将问题编码为 1024 维向量 → Milvus collection.search() 向量检索 top-K → 同时 PostgreSQL tsvector BM25 关键词检索 → RRF（Reciprocal Rank Fusion, k=60）融合两路结果 → CrossEncoder（bge-reranker-base）对融合结果重排 → 返回最终 top-K（含 cosine 相似度）',
      },
      {
        step: '问答相似度阈值过滤',
        detail: '0.40 阈值 — bge-large-zh 的问句→文档 cosine 相似度典型范围为 0.40-0.65，0.85 会杀掉几乎所有有效结果。低于 0.40 的匹配被过滤 → 保留的结果进入 LLM 二次校验。注：基线补全（字段→SRR）使用独立的 0.85 阈值',
      },
      {
        step: 'LLM 二次校验',
        detail: '_verify_kb_answers() 将问题和 KB 检索结果交给 LLM → LLM 判断检索结果是否真的回答了问题 → 过滤误匹配（如关键词相似但语义无关的需求）→ 最终保留的 KB 答案展示给用户',
      },
      {
        step: '版本去重排序',
        detail: 'matched.sort(key=lambda m: m.metadata.go_live_date, reverse=True) → 上线时间靠后的优先 → answer_text 标注"（v{project_version}, 上线: {go_live_date}, 相似度 {score:.2f}）" → LLM 提示"以上线时间靠后的最新版本为准"',
      },
      {
        step: '状态过滤',
        detail: 'Milvus 检索时 expr="status == \\"archived\\""（注意 expr 必须是字符串，不能是 None，否则 DataTypeNotMatchException）→ 草稿状态（active/draft）的需求不参与检索',
      },
      {
        step: '降级策略（三层兜底）',
        detail: '第 1 层（hybrid_searcher 内部）：向量检索与 BM25 互为兜底，任一路失败仅 logger.warning，另一路继续；两路都失败返回空列表 []。第 2 层（ClarifyAgent 调用方）：外层再包 try/except 捕获所有异常，matched = []，不向 Prompt 注入诊断信息。第 3 层（KB 状态预检）：_safe_is_empty/_safe_is_product_first 异常时返回 (False, True) 标记 KB 不可用，避免误触发冷启动。核心原则：KB 是增强项不是阻断项，绝不因 KB 降级污染生成结果，人工澄清 3 轮兜底',
      },
    ],
    config: [
      { key: 'Embedding 模型', value: 'bge-large-zh', note: '1024 维向量，本地部署' },
      { key: 'Reranker 模型', value: 'bge-reranker-base', note: 'CrossEncoder 重排，sentence_transformers 加载' },
      { key: 'BM25 检索', value: 'PostgreSQL tsvector', note: 'zhparser 中文分词，不可用时降级 simple' },
      { key: 'RRF 融合', value: 'k=60', note: 'Reciprocal Rank Fusion 融合向量+BM25 结果' },
      { key: '问答阈值', value: '0.40', note: '问句→文档典型范围 0.40-0.65' },
      { key: '基线补全阈值', value: '0.85', note: '字段→已归档 SRR 精确匹配' },
      { key: '状态过滤', value: 'status=archived', note: 'Milvus expr 字符串' },
      { key: '排序规则', value: 'go_live_date DESC → similarity DESC', note: '同功能多版本时靠后时间优先' },
      { key: 'KB LLM', value: 'kb_settings 表 llm_provider', note: '知识库专属 LLM，与主 LLM 独立' },
      { key: 'KB 查询日志', value: 'srr_json._kb_query_logs', note: '顶层 key，记录每次检索的查询和结果' },
    ],
  },
  {
    id: 'functional-points',
    icon: Layers,
    title: '功能点管理',
    subtitle: '原始文档 → FP 拆分 → 子 SRR → 审核 → 用例',
    color: 'text-teal-600 bg-teal-50',
    features: [
      { label: '一体式提取', desc: 'ReqAgent 解析 SRR 后自动从原始文档拆分功能点（FP），每个 FP 含 name / fp_type / sub_srr / test_hints' },
      { label: 'FP 审核', desc: '用户在「功能点 SRR」Tab 审核通过（approved）后解锁用例生成；AI 重生成后回退 pending_review' },
      { label: '依赖关系', desc: '上传时自动提取 FP 间 5 种依赖：depends_on / data_flows / invariants / lifecycle_scenarios / conflicts_with' },
      { label: '依赖图谱', desc: '跨需求 FP 依赖可视化，「依赖图谱」页展示并支持影响分析' },
      { label: '影响分析', desc: '基于 FP 依赖 BFS 遍历传播变更影响（默认 3 层），返回受影响功能点与聚合模块' },
    ],
    logic: [
      {
        step: 'FP 拆分',
        detail: 'ReqAgent._fp_extract_from_raw 在 SRR 解析后自动触发，按业务能力粒度拆分（单功能 1-2 个 FP，复杂需求 6-12 个），输出 functional_points[] 与 dependencies[]，存入 functional_points / fp_dependencies 表',
      },
      {
        step: '审核闭环',
        detail: '每个 FP 初始 pending_review；用户在需求详情「功能点 SRR」Tab 审核通过 → approved，所有 FP 通过后解锁用例生成；AI 重生成（fp_regenerate）后状态回退 pending_review 需重新审核',
      },
      {
        step: '依赖与影响',
        detail: 'FP 依赖存入 fp_dependencies 表；ImpactAnalysis 基于 BFS 遍历（depends_on 反向传播、data_flows / lifecycle_scenarios 正向传播），默认深度 3 层，返回受影响 FP 与聚合模块列表',
      },
    ],
    config: [
      { key: 'FP 状态机', value: 'pending_review / approved / archived', note: '审核通过后方可生成用例' },
      { key: '依赖类型', value: 'depends_on / data_flows / invariants / lifecycle_scenarios / conflicts_with', note: '5 种功能点间依赖' },
      { key: 'BFS 深度', value: '默认 3 层', note: '影响分析传播上限' },
      { key: '拆分粒度', value: '业务能力级', note: '如「登录」= 1 个 FP' },
    ],
    steps: [
      { title: '查看功能点', desc: '在需求详情「功能点」/「功能点 SRR」Tab 按模块分组查看自动拆分的功能点及其子 SRR（flows / business_rules / constraints / acceptance_criteria）。', img: 'requirement-fp.png' },
      { title: '审核与 AI 重生成', desc: '对功能点审核通过（approved 后方可生成用例），或输入提示让 AI 重新生成子 SRR（重生成后回退待审核）。', img: 'requirement-fp.png' },
      { title: '查看功能点依赖图谱', desc: '在「依赖图谱」查看功能点间 depends_on / data_flows 等依赖关系与变更影响范围。', img: 'dependency-graph.png' },
    ],
  },
  {
    id: 'testcases',
    icon: TestTube,
    title: '测试用例管理',
    subtitle: '列表视图 / XMind 脑图 / 审核 / 编辑 / 多格式导出',
    color: 'text-cyan-600 bg-cyan-50',
    features: [
      { label: '双视图模式', desc: '列表视图（按功能点分组+表格展示步骤）与 XMind 编辑器视图（脑图形态，可拖拽编辑）自由切换' },
      { label: '用例详情查看', desc: '点击用例行或 Eye 图标按钮展开详情：描述、前置条件、测试步骤（表格形式：序号/操作/预期）' },
      { label: '审核操作', desc: '每条用例支持审核通过(CheckCircle)、驳回(XCircle)、编辑(Edit3)、删除(Trash2)四种快捷操作' },
      { label: '批量删除', desc: '按项目/需求/搜索条件批量删除用例，支持「按需求删除」和「按项目逐条删除」两种模式' },
      { label: '多维度筛选', desc: '支持按项目(可搜索下拉)、需求(下拉)、标题搜索三种筛选条件组合，分页每页 20 条' },
      { label: '功能点分组', desc: '列表视图按功能点(functional_point)自动分组展示，默认展开，可折叠/展开各组' },
      { label: '重新生成', desc: '筛选了具体需求时显示「重新生成」快捷按钮，一键对该需求重新生成用例' },
      { label: 'XMind 导出', desc: '支持 XMind 2020+/Zen（JSON 格式）和 XMind 8（XML 格式）两种导出格式，层级为 项目→需求→功能点→应用类型→用例，用例标题带优先级与类型标签，可勾选是否包含描述/步骤/前置条件' },
      { label: '备注即编辑', desc: 'XMind 编辑页右键用例节点选「编辑备注（文本）」，按格式改写备注即可修改优先级/类型/描述/前置条件/测试步骤；格式有误会逐行提示行号与原因并阻止保存，点「保存」后写入系统并同步用例列表' },
      { label: '多格式导出', desc: '支持 UTCF JSON / Markdown / Gherkin / pytest 多格式导出，对接既有测试框架' },
      { label: '状态可视化', desc: '用例状态用彩色标签展示：draft(草稿)/reviewed(已审核)/approved(已通过)/rejected(已驳回)，置信度用百分比+颜色标识' },
    ],
    logic: [
      {
        step: '用例存储结构',
        detail: '生成完成后用例批量写入 PostgreSQL test_cases 表，关联 req_id（需求ID）+ product_id（产品ID）+ gen_batch_id（生成批次）。case_json 字段存储完整 UTCF（统一测试用例格式）JSON，含 id/title/type/priority/description/tags/preconditions/steps/expected/test_data/assertions/cleanup/related_requirement/related_fp/source_pattern/gen_metadata/execution/confidence',
      },
      {
        step: '用例状态机',
        detail: 'draft（草稿，AI 生成初始状态）→ reviewed（已审核，置信度 ≥ 0.6）→ approved（人工通过）/ rejected（人工驳回 或 置信度 < 0.6）。用户可手动 approve/reject 覆盖 AI 审核结果',
      },
      {
        step: '列表视图交互',
        detail: '按 functional_point 分组 → 每组可折叠/展开 → 组内用例行点击或 Eye 按钮展开详情（描述+前置条件+步骤表格）→ 右侧操作区：类型标签/优先级标签/状态标签/置信度/审核通过/驳回/编辑/删除',
      },
      {
        step: 'XMind 编辑器视图',
        detail: '切换到 XMind 视图时加载全部用例（循环分页每页100条，最多5000条）→ XMindEditor 组件以脑图形式展示 → 支持拖拽编辑节点 → 保存前弹窗确认新增/更新/删除条数（删除会列出用例标题，避免误删整个需求分支）→ 保存后同步更新列表视图',
      },
      {
        step: '筛选与搜索',
        detail: '项目筛选(可搜索下拉，支持「未绑定项目」选项) → 需求筛选(下拉，按产品过滤) → 标题模糊搜索 → 组合筛选 + 分页（每页20条）→ 清除全部按钮一键重置',
      },
      {
        step: '编辑用例',
        detail: '点击 Edit3 按钮打开 CaseEditDialog 对话框 → 可编辑标题/类型/优先级/描述/前置条件/测试步骤/预期结果 → PUT /testcases/{id} 保存 → 两个视图同步更新',
      },
      {
        step: '审核反馈回流',
        detail: 'POST /testcases/{id}/feedback 记录 approve/reject + 评论 → 更新 status 字段 → feedback 字段存储审核意见 → 供后续模型效果分析与知识库优化使用',
      },
      {
        step: '追溯关系',
        detail: '每条用例 case_json.related_fp 记录来源功能点，case_json.related_requirement 记录来源需求，构建需求-用例追溯矩阵，计算覆盖率（已生成用例 / 需求功能点）',
      },
    ],
    config: [
      { key: '导出格式', value: 'UTCF JSON / Markdown / Gherkin / pytest / XMind', note: '覆盖主流测试框架与脑图工具' },
      { key: 'XMind 格式', value: 'xmind (JSON) / xmind8 (XML)', note: 'XMind 2020+/Zen 用 JSON，XMind 8 用 XML' },
      { key: '关联字段', value: 'req_id + product_id + gen_batch_id + related_fp', note: '支撑双向追溯与分组展示' },
      { key: '用例状态', value: 'draft / reviewed / approved / rejected', note: '4 种状态，支持人工覆盖 AI 审核' },
      { key: '优先级', value: 'P0 / P1 / P2 / P3', note: '与需求优先级保持一致' },
      { key: '用例类型', value: 'functional / boundary / exception / security / performance', note: '5 种类型覆盖主流测试维度' },
      { key: '分页', value: '列表视图每页 20 条，XMind 视图循环加载全部', note: 'XMind 视图最多 5000 条' },
    ],
    steps: [
      { title: '浏览测试用例', desc: '在「测试用例」按项目/需求/标题筛选查看生成的用例列表，按功能点分组展示，支持折叠/展开。', img: 'testcases.png' },
      { title: '查看用例详情', desc: '点击用例行或 Eye 图标按钮展开详情：描述、前置条件、测试步骤表格（序号/操作/预期）。', img: 'testcases.png' },
      { title: '审核与编辑用例', desc: '每条用例支持审核通过/驳回/编辑/删除四种操作。编辑可修改标题、步骤、预期等全部字段。', img: 'testcases.png' },
      { title: '切换 XMind 视图', desc: '点击「XMind 编辑器」切换到脑图视图，以功能点为根节点展示用例树，支持拖拽编辑。', img: 'testcases.png' },
      { title: '导出用例', desc: '选择 XMind 2020+/Zen 或 XMind 8 格式导出脑图，或通过 API 导出 UTCF/Markdown/Gherkin/pytest 格式。', img: 'testcases.png' },
      { title: '重新生成', desc: '筛选了具体需求时显示「重新生成」快捷按钮，一键对该需求重新生成用例（会追加新用例，不覆盖旧的）。', img: 'testcases.png' },
    ],
  },
  {
    id: 'stability',
    icon: ShieldCheck,
    title: '稳定性与运维',
    subtitle: 'L1-L4 四层防护 + DLQ 死信队列 + CI 持续集成',
    color: 'text-emerald-600 bg-emerald-50',
    features: [
      { label: 'L1 接入层防护', desc: 'JWT 认证（密钥启动校验，禁止弱默认值）+ RBAC 权限控制（require_admin/require_write 装饰器，20+ 写端点全覆盖）+ Token Bucket 限流（基于 IP，默认 60 RPM，超限返回 429 + Retry-After 头）' },
      { label: 'L2 编排层防护', desc: 'LLM 调用断路器（三状态机：CLOSED → OPEN → HALF_OPEN）+ 指数退避全抖动重试 + 多 LLM 提供商故障转移（主供应商故障自动切换备用）' },
      { label: 'L3 Agent 层防护', desc: 'ClarifyLoopGuard 澄清回环上限 3 轮（超过强制降级 optional 并 pending_review）+ ConsistencyChecker 一致性检查（用例数方差/类型分布 Jaccard/语义相似度）+ QualityGate 质量门禁' },
      { label: 'L4 数据层防护', desc: 'KB 静默跳过（KB 不可用时返回空，不阻断主流水线）+ DLQ 死信队列（主流水线异常写入 Redis，支持事后重试）+ 幂等守卫（防并发重复操作）' },
      { label: 'DLQ 死信队列', desc: 'Redis List 实现，上限 10000 条 + TTL 7 天防 OOM；覆盖 Celery 异步任务（embedding）和 orchestrator 主流水线两类异常；写入失败不阻断主流程' },
      { label: '错误可见性原则', desc: '关键步骤失败（ClarifyAgent/FP 保存/SRR 生成/FP 拆分）必须中断流水线 + 标记 failed + 携带 error 字段；非关键路径（一致性检查/覆盖率）用 logger.debug 跳过' },
      { label: 'CI 持续集成', desc: 'GitHub Actions：Python 3.11/3.12 矩阵 + py_compile 全量语法检查 + pytest + 前端 tsc 类型检查 + Vite 构建；防止语法回归与类型错误' },
      { label: '工程治理', desc: '一次性脚本归档到 scripts/legacy/，废弃设计稿归档到 archive/，根目录 .gitignore 防止调试残留再次入库' },
    ],
    logic: [
      {
        step: 'L1 接入层（认证 + 权限 + 限流）',
        detail: '请求到达 → JWT 认证（APP_SECRET_KEY 启动时 validate_security() 强校验，空值拒绝启动）→ CurrentUser 注入 request.state → require_admin/require_write 装饰器校验角色 → rate_limit_middleware 基于 IP 的 Token Bucket 限流（API_RPM_LIMIT=60）→ 超限返回 429 + Retry-After 头。/api/health 跳过限流',
      },
      {
        step: 'L2 编排层（断路器 + 重试 + 故障转移）',
        detail: 'LLM 调用经过 CircuitBreaker（三状态机）：CLOSED（正常）→ 连续失败 5 次跳 OPEN（快速失败，不等待）→ 30 秒后跳 HALF_OPEN（放 1 个探测请求）→ 成功回 CLOSED，失败回 OPEN。指数退避全抖动重试（base=1s, max=60s, jitter 随机化避免雪崩）。主 LLM 提供商故障自动切换备用',
      },
      {
        step: 'L3 Agent 层（回环上限 + 一致性检查）',
        detail: 'ClarifyLoopGuard 限制澄清回环最多 3 轮（防止死循环）→ 超过 3 轮未解决问题降级为 optional 并强制 pending_review → ConsistencyChecker 对比历史生成任务三维度：用例数方差（>20% 异常）、类型分布 Jaccard 相似度（<0.7 异常）、语义相似度（标题 Embedding cosine <0.7 异常）→ 异常标记 is_inconsistent=true 但不阻断',
      },
      {
        step: 'L4 数据层（KB 静默跳过 + DLQ）',
        detail: 'KB 静默跳过三层降级（详见「KB 检索规则」章节）→ KB 不可用时返回空，由人工澄清兜底。DLQ 死信队列：主流水线 except 分支调用 dlq.push(task_type="pipeline", ...) 写入 Redis List；Celery 异步任务重试 3 次耗尽后写入 dlq（task_type="embedding"）；DLQ 写入本身用 try/except 包裹，失败仅 logger.warning 不阻断',
      },
      {
        step: '错误可见性原则（v2.18 强化）',
        detail: '关键路径失败：ClarifyAgent 异常 / FP 保存失败 / SRR 生成失败 / FP 拆分失败 → 中断流水线 + 标记 task.status=failed + error 字段 + SSE error 事件。非关键路径失败：一致性检查 / FP 覆盖率 / 原始用例检索 → logger.debug 跳过，不影响主流程',
      },
      {
        step: 'CI 持续集成（GitHub Actions）',
        detail: '.github/workflows/test.yml：push/PR 触发 → 后端 Python 3.11/3.12 矩阵 → pip install + py_compile 全量语法检查 + pytest（允许失败，因依赖 PG/Redis/torch）→ 前端 Node 20 + npm ci + tsc -b 类型检查 + Vite build → 产物上传归档 7 天',
      },
      {
        step: '工程治理（v2.8.0 强化）',
        detail: '一次性脚本（_*.py/check_*.py/test_e2e*.py 等 59 个）归档到 scripts/legacy/；废弃设计稿（ai-test-redesign/_design_mockups）归档到 archive/；.gitignore 增加 clarify_*.txt/srr_dump.txt/install_*.env 等模式防止调试残留入库；根目录保持整洁',
      },
    ],
    config: [
      { key: 'JWT 密钥', value: 'APP_SECRET_KEY（空默认值）', note: '生产环境启动时 validate_security() 强校验' },
      { key: 'RBAC 角色', value: 'admin / user / viewer', note: 'require_admin / require_write / get_current_user' },
      { key: '限流', value: 'Token Bucket 60 RPM', note: 'API_RPM_LIMIT，基于 IP，超限 429' },
      { key: '断路器阈值', value: '失败 5 次 → OPEN', note: '30 秒后 HALF_OPEN 探测' },
      { key: '重试策略', value: '指数退避全抖动', note: 'base=1s, max=60s, jitter 随机化' },
      { key: 'DLQ 上限', value: '10000 条 + TTL 7 天', note: 'Redis List，LTRIM + EXPIRE 防 OOM' },
      { key: 'CI 矩阵', value: 'Python 3.11/3.12 + Node 20', note: 'GitHub Actions，push/PR 触发' },
      { key: '澄清回环上限', value: '3 轮', note: '超过强制降级 optional + pending_review' },
    ],
    terms: [
      { term: 'DLQ', desc: 'Dead Letter Queue（死信队列）— 主流水线或异步任务失败时将错误记录写入 Redis List，支持事后审计与手动重试。上限 10000 条，TTL 7 天。' },
      { term: 'CircuitBreaker', desc: '断路器 — 三状态机（CLOSED/OPEN/HALF_OPEN），连续失败达阈值后快速失败避免雪崩，恢复期放探测请求试探。' },
      { term: 'Token Bucket', desc: '令牌桶限流 — 基于 IP 的全局限流算法，按速率补充令牌，请求消耗令牌，桶空时返回 429。' },
      { term: 'ClarifyLoopGuard', desc: '澄清回环守卫 — 限制澄清流程最多 3 轮，防止死循环；超过后降级为 optional 强制 pending_review。' },
      { term: 'ConsistencyChecker', desc: '一致性检查器 — 对比历史生成任务的三维度（用例数方差/类型分布 Jaccard/语义相似度），异常时标记但不阻断。' },
      { term: 'QualityGate', desc: '质量门禁 — 对澄清后的 SRR 做完整性/歧义/遗漏检查，达标才允许进入下一阶段。' },
      { term: 'RRF', desc: 'Reciprocal Rank Fusion（倒数排名融合）— 融合向量检索与 BM25 检索结果的算法，k=60。' },
      { term: 'KB 静默跳过', desc: '知识库不可用时返回空列表，不抛异常、不阻断主流水线、不向 Prompt 注入诊断信息，由人工澄清兜底。' },
    ],
  },
]

/* ===================== 章节图标色映射（Apple 设计系统 CSS 变量） ===================== */
const SECTION_COLOR_STYLES: Record<string, { color: string; background: string }> = {
  'text-slate-600 bg-slate-50': { color: 'var(--foreground-secondary)', background: 'var(--background-muted)' },
  'text-blue-600 bg-blue-50': { color: 'var(--brand-500)', background: 'rgba(0, 122, 255, 0.08)' },
  'text-indigo-600 bg-indigo-50': { color: 'var(--brand-500)', background: 'rgba(0, 122, 255, 0.08)' },
  'text-green-600 bg-green-50': { color: 'var(--state-success)', background: 'var(--state-success-surface)' },
  'text-purple-600 bg-purple-50': { color: 'var(--brand-500)', background: 'rgba(0, 122, 255, 0.08)' },
  'text-amber-600 bg-amber-50': { color: 'var(--state-warning)', background: 'var(--state-warning-surface)' },
  'text-red-600 bg-red-50': { color: 'var(--state-error)', background: 'var(--state-error-surface)' },
  'text-teal-600 bg-teal-50': { color: 'var(--state-success)', background: 'var(--state-success-surface)' },
  'text-cyan-600 bg-cyan-50': { color: 'var(--brand-500)', background: 'rgba(0, 122, 255, 0.08)' },
  'text-emerald-600 bg-emerald-50': { color: 'var(--state-success)', background: 'var(--state-success-surface)' },
  'text-rose-600 bg-rose-50': { color: 'var(--state-error)', background: 'var(--state-error-surface)' },
}
const getSectionColorStyle = (color: string) => SECTION_COLOR_STYLES[color] || { color: 'var(--foreground-secondary)', background: 'var(--background-muted)' }

export default function UserManual() {
  const navigate = useNavigate()
  const [activeId, setActiveId] = useState(SECTIONS[0].id)
  const [version, setVersion] = useState('')

  const active = SECTIONS.find(s => s.id === activeId) || SECTIONS[0]
  const ActiveIcon = active.icon

  // 版本号统一取自后端 /api/health（app.__version__），与系统版本保持一致
  useEffect(() => {
    health().then(h => setVersion(h.version || '')).catch(() => {})
  }, [])

  return (
    <div className="flex gap-6 h-[calc(100vh-8rem)]">
      {/* 左侧功能菜单 */}
      <aside className="w-56 shrink-0 rounded-apple p-3 overflow-y-auto"
        style={{ background: 'var(--background)', border: '1px solid var(--border)', boxShadow: 'var(--shadow-apple-sm)' }}>
        <div className="flex items-center gap-2 px-2 py-2 mb-2">
          <BookOpen className="w-4 h-4" style={{ color: 'var(--brand-500)' }} />
          <span className="text-sm font-semibold" style={{ color: 'var(--foreground-secondary)' }}>功能导航</span>
        </div>
        <nav className="space-y-1">
          {SECTIONS.map(section => {
            const Icon = section.icon
            const isActive = section.id === activeId
            const activeStyle = getSectionColorStyle(section.color)
            return (
              <button
                key={section.id}
                onClick={() => setActiveId(section.id)}
                className="w-full flex items-center gap-2.5 px-3 py-2.5 rounded-xl text-sm font-medium transition-all duration-150 text-left"
                style={isActive
                  ? { background: 'rgba(0, 122, 255, 0.08)', color: 'var(--brand-600)', boxShadow: 'var(--shadow-apple-sm)' }
                  : { color: 'var(--foreground-tertiary)' }
                }
              >
                <div className="w-7 h-7 rounded-lg flex items-center justify-center shrink-0"
                  style={isActive
                    ? { background: activeStyle.background, color: activeStyle.color }
                    : { background: 'var(--background-muted)', color: 'var(--foreground-quaternary)' }
                  }>
                  <Icon className="w-3.5 h-3.5" />
                </div>
                <div className="min-w-0">
                  <div className="truncate">{section.title}</div>
                  <div className="text-[10px] truncate" style={{ color: 'var(--foreground-quaternary)' }}>{section.subtitle}</div>
                </div>
              </button>
            )
          })}
        </nav>

        <button
          onClick={() => navigate(-1)}
          className="w-full flex items-center gap-2 px-3 py-2.5 mt-4 rounded-xl text-sm transition-all"
          style={{ color: 'var(--foreground-tertiary)' }}
        >
          <ArrowLeft className="w-4 h-4" />
          返回
        </button>
      </aside>

      {/* 右侧内容区 */}
      <div className="flex-1 rounded-apple overflow-y-auto"
        style={{ background: 'var(--background)', border: '1px solid var(--border)', boxShadow: 'var(--shadow-apple-sm)' }}>
        {/* Section Header */}
        <div className="sticky top-0 border-b px-7 py-5 z-10"
          style={{ background: 'rgba(255, 255, 255, 0.95)', backdropFilter: 'blur(4px)', borderColor: 'var(--border)' }}>
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl flex items-center justify-center"
              style={{ background: getSectionColorStyle(active.color).background, color: getSectionColorStyle(active.color).color }}>
              <ActiveIcon className="w-5 h-5" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h1 className="text-xl font-bold" style={{ color: 'var(--foreground)' }}>{active.title}</h1>
                {version && (
                  <span className="px-2 py-0.5 rounded-md text-xs font-mono"
                    style={{ background: 'var(--background-muted)', color: 'var(--foreground-tertiary)' }}>v{version}</span>
                )}
              </div>
              <p className="text-xs mt-0.5" style={{ color: 'var(--foreground-quaternary)' }}>{active.subtitle}</p>
              {version && (
                <p className="text-[11px] mt-0.5" style={{ color: 'var(--foreground-quaternary)' }}>📌 用户手册 v{version} · 与系统版本保持一致</p>
              )}
            </div>
          </div>
        </div>

        <div className="px-7 py-6 space-y-8">
          {/* 功能说明 */}
          <section>
            <div className="flex items-center gap-2 mb-4">
              <Layers className="w-4 h-4" style={{ color: 'var(--brand-500)' }} />
              <h2 className="text-base font-semibold" style={{ color: 'var(--foreground-secondary)' }}>功能说明</h2>
            </div>
            <div className="space-y-2.5">
              {active.features.map((item, i) => (
                <div key={i} className="flex gap-3 text-sm p-2.5 rounded-lg transition-all" style={{ borderRadius: 'calc(var(--radius) * 0.5)' }}>
                  <span className="font-medium whitespace-nowrap min-w-[90px] shrink-0" style={{ color: 'var(--brand-500)' }}>{item.label}</span>
                  <span style={{ color: 'var(--foreground-secondary)' }}>{item.desc}</span>
                </div>
              ))}
            </div>
          </section>

          {/* 术语对照（英文标识 → 中文解释） */}
          {active.terms && active.terms.length > 0 && (
            <section>
              <div className="flex items-center gap-2 mb-4">
                <BookOpen className="w-4 h-4" style={{ color: 'var(--brand-500)' }} />
                <h2 className="text-base font-semibold" style={{ color: 'var(--foreground-secondary)' }}>术语对照（英文标识 → 中文解释）</h2>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
                {active.terms.map((t, i) => (
                  <div key={i} className="p-3 rounded-lg"
                    style={{ background: 'var(--background-muted)', border: '1px solid var(--border)', borderRadius: 'calc(var(--radius) * 0.5)' }}>
                    <div className="text-sm font-semibold font-mono" style={{ color: 'var(--brand-500)' }}>{t.term}</div>
                    <div className="text-xs leading-relaxed mt-1" style={{ color: 'var(--foreground-tertiary)' }}>{t.desc}</div>
                  </div>
                ))}
              </div>
            </section>
          )}

          {/* 操作步骤（截图 + 文字） */}
          {active.steps && active.steps.length > 0 && (
            <section>
              <div className="flex items-center gap-2 mb-4">
                <Image className="w-4 h-4" style={{ color: 'var(--brand-500)' }} />
                <h2 className="text-base font-semibold" style={{ color: 'var(--foreground-secondary)' }}>操作步骤</h2>
              </div>
              <div className="space-y-6">
                {active.steps.map((s, i) => (
                  <div key={i} className="rounded-xl border overflow-hidden" style={{ borderColor: 'var(--border)', borderRadius: 'calc(var(--radius) * 0.75)' }}>
                    <div className="flex items-center gap-2 px-4 py-3 border-b" style={{ background: 'rgba(242, 242, 247, 0.6)', borderColor: 'var(--border)' }}>
                      <span className="w-6 h-6 rounded-full text-white flex items-center justify-center text-xs font-bold shrink-0"
                        style={{ background: 'var(--brand-500)' }}>{i + 1}</span>
                      <span className="text-sm font-semibold" style={{ color: 'var(--foreground-secondary)' }}>{s.title}</span>
                    </div>
                    <div className="p-4 space-y-3">
                      <p className="text-sm leading-relaxed" style={{ color: 'var(--foreground-secondary)' }}>{s.desc}</p>
                      {s.img && (
                        <img
                          src={`/manual-imgs/${s.img}`}
                          alt={s.title}
                          loading="lazy"
                          className="w-full rounded-lg border"
                          style={{ borderColor: 'var(--border)', borderRadius: 'calc(var(--radius) * 0.5)' }}
                        />
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </section>
          )}

          {/* 架构图 */}
          {active.diagram && (
            <section>
              <div className="flex items-center gap-2 mb-4">
                <Workflow className="w-4 h-4" style={{ color: 'var(--brand-500)' }} />
                <h2 className="text-base font-semibold" style={{ color: 'var(--foreground-secondary)' }}>系统逻辑结构</h2>
              </div>
              <div className="rounded-xl border p-4"
                style={{ background: 'rgba(242, 242, 247, 0.6)', borderColor: 'var(--border)', borderRadius: 'calc(var(--radius) * 0.75)' }}>
                {active.diagram}
              </div>
            </section>
          )}

          {/* 实现逻辑 */}
          <section>
            <div className="flex items-center gap-2 mb-4">
              <Cpu className="w-4 h-4" style={{ color: 'var(--state-success)' }} />
              <h2 className="text-base font-semibold" style={{ color: 'var(--foreground-secondary)' }}>实现逻辑</h2>
            </div>
            <div className="space-y-3">
              {active.logic.map((item, i) => (
                <div key={i} className="p-4 rounded-xl"
                  style={{ background: 'var(--background-muted)', border: '1px solid var(--border)', borderRadius: 'calc(var(--radius) * 0.75)' }}>
                  <div className="flex items-center gap-2 mb-1.5">
                    <span className="w-6 h-6 rounded-full flex items-center justify-center text-xs font-bold shrink-0"
                      style={{ background: 'var(--state-success-surface)', color: 'var(--state-success)' }}>
                      {i + 1}
                    </span>
                    <span className="text-sm font-semibold" style={{ color: 'var(--foreground-secondary)' }}>{item.step}</span>
                  </div>
                  <p className="text-xs leading-relaxed ml-8" style={{ color: 'var(--foreground-tertiary)' }}>{item.detail}</p>
                </div>
              ))}
            </div>
          </section>

          {/* 关键配置 */}
          {active.config && active.config.length > 0 && (
            <section>
              <div className="flex items-center gap-2 mb-4">
                <Zap className="w-4 h-4" style={{ color: 'var(--state-warning)' }} />
                <h2 className="text-base font-semibold" style={{ color: 'var(--foreground-secondary)' }}>关键配置</h2>
              </div>
              <div className="overflow-hidden rounded-xl border" style={{ borderColor: 'var(--border)', borderRadius: 'calc(var(--radius) * 0.75)' }}>
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-xs" style={{ background: 'var(--background-muted)', color: 'var(--foreground-tertiary)' }}>
                      <th className="px-4 py-2.5 text-left font-medium">配置项</th>
                      <th className="px-4 py-2.5 text-left font-medium">值</th>
                      <th className="px-4 py-2.5 text-left font-medium">说明</th>
                    </tr>
                  </thead>
                  <tbody>
                    {active.config.map((item, i) => (
                      <tr key={i} style={{ borderBottom: i < active.config!.length - 1 ? '1px solid var(--background-muted)' : 'none' }}>
                        <td className="px-4 py-2.5 font-medium whitespace-nowrap" style={{ color: 'var(--foreground-secondary)' }}>{item.key}</td>
                        <td className="px-4 py-2.5 font-mono text-xs" style={{ color: 'var(--brand-500)' }}>{item.value}</td>
                        <td className="px-4 py-2.5 text-xs" style={{ color: 'var(--foreground-tertiary)' }}>{item.note || '-'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}
        </div>
      </div>
    </div>
  )
}
