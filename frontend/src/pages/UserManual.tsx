import { useState, useEffect } from 'react'
import type { ReactNode } from 'react'
import {
  Upload, FileText, Database, FolderTree, Settings, Search,
  BookOpen, ArrowLeft, Workflow, Cpu, GitBranch, ShieldCheck,
  Layers, Zap, Image, TestTube,
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
      {node(60, 40, 150, 44, '#E6F1FB', '#185FA5', 'parse', 'ReqAgent 解析')}
      {node(250, 40, 170, 44, '#EEEDFE', '#534AB7', 'select_channel', '通道选择')}
      {node(60, 140, 150, 44, '#FAEEDA', '#BA7517', 'clarify', 'ClarifyAgent')}
      {node(250, 140, 150, 44, '#FAECE7', '#993C1D', 'gate', 'QualityGate')}
      {node(450, 140, 150, 44, '#E1F5EE', '#0F6E56', 'impact', 'ImpactAgent')}
      {node(450, 230, 150, 44, '#E1F5EE', '#0F6E56', 'retrieve', 'KB 检索')}
      {node(450, 320, 150, 44, '#EAF3DE', '#3B6D11', 'generate', 'GenAgent')}
      {node(250, 320, 150, 44, '#FBEAF0', '#993556', 'review', 'ReviewAgent')}
      {node(60, 320, 100, 44, '#F1EFE8', '#5F5E5A', 'END', '')}
      {arrow(210, 62, 250, 62)}
      {arrow(282, 84, 165, 140)}
      {arrow(335, 84, 325, 140)}
      {arrow(210, 162, 250, 162)}
      {arrow(400, 162, 450, 162)}
      {arrow(250, 162, 212, 162, true)}
      {arrow(525, 184, 525, 230)}
      {arrow(525, 274, 525, 320)}
      {arrow(450, 342, 400, 342)}
      {arrow(250, 342, 160, 342)}
      <text x="222" y="104" fontSize={10} fill="#94a3b8">FULL</text>
      <text x="352" y="118" fontSize={10} fill="#94a3b8">FAST</text>
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
      { label: '流水线', desc: '上传 → 解析 → 澄清 → 质量门禁 → 影响分析 → KB 检索 → 生成 → 审核，由 LangGraph 编排' },
      { label: '通道选择', desc: 'select_channel 按需求复杂度分流：FULL 走澄清闭环，FAST 直接进质量门禁' },
      { label: '质量门禁', desc: 'gate 评估完整性；未通过且可重试则回到 clarify，达到上限强制通过' },
      { label: '存储分层', desc: 'PostgreSQL（结构化）+ Milvus（向量）+ MinIO（对象存储）+ etcd（元数据）' },
    ],
    logic: [
      {
        step: 'DAG 编排',
        detail: 'LangGraph StateGraph 编译，固定节点依次为 parse → select_channel → clarify → gate → impact → retrieve → generate → review。select_channel 条件分支（FULL/FAST），gate 条件分支（impact / clarify 重试）',
      },
      {
        step: '双写落库',
        detail: '需求归档时同时写入 PostgreSQL（结构化字段）与 Milvus（1024 维向量 + srr_json 全文），保证 KB 检索能拿到完整 SRR 用于基线补全',
      },
      {
        step: '向量化',
        detail: 'bge-large-zh 将 SRR 文本编码为 1024 维向量写入 Milvus 的 5 个集合：requirements / test_cases / patterns / req_pats / errors',
      },
    ],
    config: [
      { key: '编排框架', value: 'LangGraph StateGraph', note: '节点 + 条件边编译为可执行图' },
      { key: '向量模型', value: 'bge-large-zh', note: '1024 维，本地部署' },
      { key: '向量后端', value: 'Milvus + MinIO + etcd', note: 'Milvus 计算，MinIO 存数据文件，etcd 存元数据' },
    ],
    diagram: <DagPipelineDiagram />,
  },
  {
    id: 'requirements',
    icon: Upload,
    title: '需求管理',
    subtitle: '上传 → 解析 → KB 补全 → 澄清 → 归档',
    color: 'text-blue-600 bg-blue-50',
    steps: [
      { title: '上传需求文档', desc: '在「需求管理」点击「上传需求」，填写需求名称、选择所属项目/版本与关联模块，上传 .md/.docx/.txt 后点击「上传并解析」，系统进入 LLM 分析 → KB 检索 → 澄清 → 影响分析流水线。', img: 'requirements-upload.png' },
      { title: '查看需求列表', desc: '解析完成后在列表中查看需求，支持按项目/状态/优先级筛选与标题模糊搜索，快速定位目标需求。', img: 'requirements-list.png' },
      { title: '查看需求详情', desc: '点击需求进入详情页，可查看结构化 SRR、功能点、影响分析与追溯矩阵，并支持修改上线时间。', img: 'requirement-detail.png' },
      { title: '回答澄清问题', desc: '对待补充的需求进入「澄清」页，逐条回答系统基于 KB 检索与维度检测生成的问题并提交，系统据此重新生成更精确的 SRR。', img: 'requirement-clarify.png' },
    ],
    features: [
      { label: '上传需求', desc: 'POST /requirements/upload 传入 raw_text（.md/.docx 原文），系统自动解析为结构化 SRR JSON' },
      { label: '自动解析', desc: 'ReqAgent 调用 LLM 将非结构化文本转为标准化需求格式' },
      { label: 'KB 检索补全', desc: '自动检索知识库中相似历史需求，补全缺失字段（问答匹配 > 0.40，字段基线补全 > 0.85）' },
      { label: '人工澄清', desc: '系统动态生成需人工确认的问题（KB 查询、缺失维度、字段冲突），用户回答后重新生成 SRR' },
      { label: '重新生成', desc: '在澄清页面可补充备注并重新解析需求，操作记录保存在澄清历史中' },
      { label: '归档', desc: '需求完成后归档入库，需填写上线时间（精确到分钟），同时写入 PostgreSQL 与 Milvus，供后续 KB 检索' },
      { label: '修改上线时间', desc: '已归档需求可随时修改上线时间，同步更新 Milvus 元数据，保证版本排序准确' },
      { label: '搜索筛选', desc: '需求列表支持按状态（draft/active/archived）/优先级筛选 + 标题模糊搜索，快速定位目标需求' },
    ],
    logic: [
      {
        step: '文件上传 → SRR 生成',
        detail: '用户提交 raw_text → POST /requirements/upload → ReqAgent.parse() 调用 LLM 将文本解析为 SRR JSON，含 overview / modules / boundary_conditions / happy_path / alternate_flows / business_rules / data_constraints / preconditions 等字段 → 解析完成即调用 vector_store.add_requirement() 将需求写入 Milvus（此时 status 尚非 archived，仅建立向量索引）',
      },
      {
        step: 'KB 检索补全',
        detail: 'ClarifyAgent._query_kb_for_answers() 调用 vector_store.search_similar_requirements() 检索 Milvus requirements 集合（仅 status=archived），bge-large-zh 生成 embedding → 问答余弦相似度 > 0.40 的结果保留 → LLM _verify_kb_answers() 二次校验过滤误匹配 → 命中的 baseline block（happy_path/business_rules/data_constraints 等）按字段基线补全阈值 > 0.85 自动补全到 SRR',
      },
      {
        step: '人工澄清问题生成',
        detail: '动态分析当前 SRR：① KB 检索到的缺失字段 ② requirement_patterns.py 检测的缺失维度（对照 auth/crud/payment 等模式）③ 字段冲突（如 priority 不一致）→ 经 LLM 过滤（已在 SRR 中描述的问题跳过）→ 生成 input_type 为 text/select/multi_select 的问题 → 用户回答 → POST /requirements/{id}/clarify → 重新生成 SRR',
      },
      {
        step: '重新生成',
        detail: 'POST /requirements/{id}/regenerate — 即使 status=active 也可调用，传入 extra_notes 补充信息 → ReqAgent 重新解析 → 操作记录存入 srr_json._clarify_history 数组（{timestamp, action:"regenerate", ...}），跨 re-parse/finalize/regenerate 保留',
      },
      {
        step: '归档入库（Celery 异步双写）',
        detail: 'POST /requirements/{id}/archive — 接收 ArchiveRequest（含 go_live_date YYYY-MM-DDTHH:MM）→ 设置 status=archived → 查询项目 version → 派发 Celery 异步任务计算 Embedding 并写入 Milvus 元数据：{product_id, modules, priority, status, go_live_date, project_version} → API 即时返回（kb_task_queued=true），Celery Worker 异步完成向量写入',
      },
    ],
    config: [
      { key: 'LLM 供应商', value: '系统设置 → LLM 配置', note: '支持 DeepSeek/OpenAI/通义千问等多供应商主备' },
      { key: '解析 Prompt', value: '提示词管理 → req_agent', note: '可自定义解析规则和输出格式' },
      { key: 'KB 相似度阈值', value: '问答 0.40 / 基线 0.85', note: '问答为问句→文档典型范围；基线补全是字段→已归档 SRR 精确匹配' },
      { key: '澄清历史', value: 'srr_json._clarify_history', note: '永久保留，跨操作不丢失' },
    ],
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
        detail: '提取的基线块写入 V2 SRR 的 _baseline_context 与 additional_details，使优化需求自动继承完整上下文，无需人工重复录入',
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
    subtitle: 'SRR → 多维用例 → 审核 → 导出',
    color: 'text-green-600 bg-green-50',
    steps: [
      { title: '进入用例生成', desc: '选择已就绪（active）的需求进入「用例生成」页，确认 SRR 后点击「开始生成」，系统基于 SRR 与测试模式 SSE 流式生成多维用例（功能/边界/异常/安全）。', img: 'generate.png' },
      { title: '查看生成结果与质量评分', desc: '生成完成后逐条查看用例与 ReviewAgent 质量评分（完整度/明确性/可执行性），可逐条接受 / 拒绝 / 编辑。', img: 'generate.png' },
      { title: '在任务管理查看进度', desc: '生成任务可在顶部「任务管理」实时查看进度与状态（含失败/中断提示），无需阻塞等待。', img: 'tasks.png' },
    ],
    features: [
      { label: '生成用例', desc: '选择已澄清（active）的需求，AI 自动生成多维测试用例（功能/边界/异常/安全）' },
      { label: '审核编辑', desc: '生成后可逐条审核、编辑、接受或拒绝用例' },
      { label: '影响分析', desc: '优化需求场景下，自动识别受影响模块并推荐回归用例' },
      { label: '格式输出', desc: '支持 UTCF JSON / Markdown / Gherkin / pytest 多格式导出' },
      { label: '追溯矩阵', desc: '用例与需求双向追溯，覆盖率矩阵可视化' },
    ],
    logic: [
      {
        step: 'SSE 流式生成',
        detail: 'POST /generate (Server-Sent Events) → GenAgent.generate() 基于 SRR JSON 和检索到的测试模式 → 逐条流式输出用例（{title, steps, expected, priority, category}）→ 前端实时渲染 → 全部完成后批量存入 PostgreSQL test_cases 表',
      },
      {
        step: '测试模式复用',
        detail: '从 Milvus patterns 集合检索与当前需求匹配的测试模式（种子模式 8 种：CRUD / 状态机 / 认证 / 输入校验 / 分页 / 并发 / 异步回调 / 数据一致性）→ GenAgent 参考模式的 test_dimensions 和 typical_cases 生成更贴合规范的用例',
      },
      {
        step: '影响分析',
        detail: 'ImpactAgent 基于模块依赖图谱（dependency_graph 表）BFS 遍历：直接模块（depth=1）→ 间接模块（depth=2+）→ 查找关联需求 → 推荐回归测试用例 → 前端可视化展示影响范围',
      },
      {
        step: '用例审核',
        detail: 'ReviewAgent 对生成用例做质量评分（完整度 / 明确性 / 可执行性）→ 用户可逐条 accept/reject → POST /testcases/{id}/feedback 记录反馈 → 模式库 success_rate 自动更新',
      },
    ],
    config: [
      { key: '生成 Prompt', value: '提示词管理 → gen_agent', note: '控制用例维度、格式、数量' },
      { key: '审核 Prompt', value: '提示词管理 → review_agent', note: '质量评分规则' },
      { key: '影响分析 Prompt', value: '提示词管理 → impact_agent', note: 'BFS 遍历深度和推荐策略' },
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
        step: '降级策略',
        detail: 'KB 完全不可用时（Milvus 连接失败）→ 静默跳过 KB 检索，正常生成内容，人工澄清兜底 → 向量检索失败时仅用 BM25 → BM25 失败时仅用向量 → CrossEncoder 重排失败时回退 RRF 排序 → 不向用户注入诊断信息，不影响内容生成质量',
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
    subtitle: '浏览 / 审核 / 编辑 / 多格式导出',
    color: 'text-cyan-600 bg-cyan-50',
    features: [
      { label: '用例浏览', desc: '按需求 / 模块 / 状态筛选查看生成的测试用例，支持标题搜索' },
      { label: '审核编辑', desc: '逐条查看用例（标题/步骤/预期/优先级/类别），可编辑、接受或拒绝' },
      { label: '追溯矩阵', desc: '用例与需求双向追溯，覆盖率矩阵可视化' },
      { label: '多格式导出', desc: '支持 UTCF JSON / Markdown / Gherkin / pytest 多格式导出，对接既有测试框架' },
    ],
    logic: [
      {
        step: '用例存储',
        detail: '生成完成后用例批量写入 PostgreSQL test_cases 表，关联 requirement_id 与 module_id，支持按需求 / 模块检索与状态过滤',
      },
      {
        step: '追溯关系',
        detail: '每条用例记录来源需求与功能点，构建需求-用例追溯矩阵，计算覆盖率（已生成用例 / 需求功能点）',
      },
      {
        step: '多格式导出',
        detail: '前端按选中格式（UTCF / Markdown / Gherkin / pytest）将用例序列化导出，便于直接导入测试框架执行',
      },
    ],
    config: [
      { key: '导出格式', value: 'UTCF JSON / Markdown / Gherkin / pytest', note: '覆盖主流测试框架' },
      { key: '关联字段', value: 'requirement_id + module_id + fp_id', note: '支撑双向追溯' },
      { key: '优先级联动', value: 'P0-P3', note: '与需求优先级保持一致' },
    ],
    steps: [
      { title: '浏览测试用例', desc: '在「测试用例」按需求 / 模块 / 状态筛选查看生成的用例列表，快速定位目标用例。', img: 'testcases.png' },
      { title: '审核与编辑用例', desc: '逐条查看用例的标题、步骤、预期与优先级，可编辑内容或接受 / 拒绝。', img: 'testcases.png' },
      { title: '多格式导出', desc: '选择 UTCF JSON / Markdown / Gherkin / pytest 等格式导出用例，直接对接既有测试框架。', img: 'testcases.png' },
    ],
  },
]

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
      <aside className="w-56 shrink-0 bg-white rounded-2xl border border-slate-100 shadow-sm p-3 overflow-y-auto">
        <div className="flex items-center gap-2 px-2 py-2 mb-2">
          <BookOpen className="w-4 h-4 text-primary-500" />
          <span className="text-sm font-semibold text-slate-700">功能导航</span>
        </div>
        <nav className="space-y-1">
          {SECTIONS.map(section => {
            const Icon = section.icon
            const isActive = section.id === activeId
            return (
              <button
                key={section.id}
                onClick={() => setActiveId(section.id)}
                className={`w-full flex items-center gap-2.5 px-3 py-2.5 rounded-xl text-sm font-medium transition-all duration-150 text-left ${
                  isActive
                    ? 'bg-gradient-to-r from-primary-50 to-primary-50/50 text-primary-700 shadow-sm'
                    : 'text-slate-500 hover:bg-slate-50 hover:text-slate-700'
                }`}
              >
                <div className={`w-7 h-7 rounded-lg flex items-center justify-center shrink-0 ${isActive ? section.color : 'bg-slate-100 text-slate-400'}`}>
                  <Icon className="w-3.5 h-3.5" />
                </div>
                <div className="min-w-0">
                  <div className="truncate">{section.title}</div>
                  <div className="text-[10px] text-slate-400 truncate">{section.subtitle}</div>
                </div>
              </button>
            )
          })}
        </nav>

        <button
          onClick={() => navigate(-1)}
          className="w-full flex items-center gap-2 px-3 py-2.5 mt-4 rounded-xl text-sm text-slate-500 hover:bg-slate-50 transition-all"
        >
          <ArrowLeft className="w-4 h-4" />
          返回
        </button>
      </aside>

      {/* 右侧内容区 */}
      <div className="flex-1 bg-white rounded-2xl border border-slate-100 shadow-sm overflow-y-auto">
        {/* Section Header */}
        <div className="sticky top-0 bg-white/95 backdrop-blur-sm border-b border-slate-100 px-7 py-5 z-10">
          <div className="flex items-center gap-3">
            <div className={`w-10 h-10 rounded-xl flex items-center justify-center ${active.color}`}>
              <ActiveIcon className="w-5 h-5" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h1 className="text-xl font-bold text-slate-800">{active.title}</h1>
                {version && (
                  <span className="px-2 py-0.5 rounded-md bg-slate-100 text-slate-500 text-xs font-mono">v{version}</span>
                )}
              </div>
              <p className="text-xs text-slate-400 mt-0.5">{active.subtitle}</p>
              {version && (
                <p className="text-[11px] text-slate-300 mt-0.5">📌 用户手册 v{version} · 与系统版本保持一致</p>
              )}
            </div>
          </div>
        </div>

        <div className="px-7 py-6 space-y-8">
          {/* 功能说明 */}
          <section>
            <div className="flex items-center gap-2 mb-4">
              <Layers className="w-4 h-4 text-primary-500" />
              <h2 className="text-base font-semibold text-slate-700">功能说明</h2>
            </div>
            <div className="space-y-2.5">
              {active.features.map((item, i) => (
                <div key={i} className="flex gap-3 text-sm p-2.5 rounded-lg hover:bg-slate-50 transition-all">
                  <span className="text-primary-500 font-medium whitespace-nowrap min-w-[90px] shrink-0">{item.label}</span>
                  <span className="text-slate-600">{item.desc}</span>
                </div>
              ))}
            </div>
          </section>

          {/* 操作步骤（截图 + 文字） */}
          {active.steps && active.steps.length > 0 && (
            <section>
              <div className="flex items-center gap-2 mb-4">
                <Image className="w-4 h-4 text-primary-500" />
                <h2 className="text-base font-semibold text-slate-700">操作步骤</h2>
              </div>
              <div className="space-y-6">
                {active.steps.map((s, i) => (
                  <div key={i} className="rounded-xl border border-slate-100 overflow-hidden">
                    <div className="flex items-center gap-2 px-4 py-3 bg-slate-50/60 border-b border-slate-100">
                      <span className="w-6 h-6 rounded-full bg-primary-500 text-white flex items-center justify-center text-xs font-bold shrink-0">{i + 1}</span>
                      <span className="text-sm font-semibold text-slate-700">{s.title}</span>
                    </div>
                    <div className="p-4 space-y-3">
                      <p className="text-sm text-slate-600 leading-relaxed">{s.desc}</p>
                      {s.img && (
                        <img
                          src={`/manual-imgs/${s.img}`}
                          alt={s.title}
                          loading="lazy"
                          className="w-full rounded-lg border border-slate-100"
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
                <Workflow className="w-4 h-4 text-primary-500" />
                <h2 className="text-base font-semibold text-slate-700">系统逻辑结构</h2>
              </div>
              <div className="rounded-xl border border-slate-100 bg-slate-50/60 p-4">
                {active.diagram}
              </div>
            </section>
          )}

          {/* 实现逻辑 */}
          <section>
            <div className="flex items-center gap-2 mb-4">
              <Cpu className="w-4 h-4 text-green-500" />
              <h2 className="text-base font-semibold text-slate-700">实现逻辑</h2>
            </div>
            <div className="space-y-3">
              {active.logic.map((item, i) => (
                <div key={i} className="p-4 rounded-xl bg-slate-50 border border-slate-100">
                  <div className="flex items-center gap-2 mb-1.5">
                    <span className="w-6 h-6 rounded-full bg-green-100 text-green-600 flex items-center justify-center text-xs font-bold shrink-0">
                      {i + 1}
                    </span>
                    <span className="text-sm font-semibold text-slate-700">{item.step}</span>
                  </div>
                  <p className="text-xs text-slate-500 leading-relaxed ml-8">{item.detail}</p>
                </div>
              ))}
            </div>
          </section>

          {/* 关键配置 */}
          {active.config && active.config.length > 0 && (
            <section>
              <div className="flex items-center gap-2 mb-4">
                <Zap className="w-4 h-4 text-amber-500" />
                <h2 className="text-base font-semibold text-slate-700">关键配置</h2>
              </div>
              <div className="overflow-hidden rounded-xl border border-slate-100">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="bg-slate-50 text-slate-500 text-xs">
                      <th className="px-4 py-2.5 text-left font-medium">配置项</th>
                      <th className="px-4 py-2.5 text-left font-medium">值</th>
                      <th className="px-4 py-2.5 text-left font-medium">说明</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-50">
                    {active.config.map((item, i) => (
                      <tr key={i} className="hover:bg-slate-50/50">
                        <td className="px-4 py-2.5 text-slate-700 font-medium whitespace-nowrap">{item.key}</td>
                        <td className="px-4 py-2.5 text-primary-600 font-mono text-xs">{item.value}</td>
                        <td className="px-4 py-2.5 text-slate-500 text-xs">{item.note || '-'}</td>
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
