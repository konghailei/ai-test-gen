import { useState, useEffect } from 'react'
import { Database, Server, Activity, BookOpen, HardDrive, Network, Boxes, Info, CheckCircle2, XCircle, AlertTriangle, RefreshCw } from 'lucide-react'
import { knowledge, health } from '../api/client'
import type { KnowledgeStats, TestPattern, HealthStatus, KbDiagnostics } from '../types'

export default function AdminKnowledgeBase() {
  const [stats, setStats] = useState<KnowledgeStats | null>(null)
  const [healthStatus, setHealth] = useState<HealthStatus | null>(null)
  const [diag, setDiag] = useState<KbDiagnostics | null>(null)
  const [patterns, setPatterns] = useState<TestPattern[]>([])
  const [category, setCategory] = useState('')
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)

  const loadAll = async () => {
    try {
      const [s, h, d] = await Promise.all([knowledge.stats(), health(), knowledge.diagnostics().catch(() => null)])
      setStats(s); setHealth(h as any); setDiag(d as any)
    } catch {}
  }

  const loadPatterns = async (cat?: string) => {
    try { const r = await knowledge.patterns(cat || undefined); setPatterns((r as any).items || []) } catch {}
  }

  useEffect(() => {
    Promise.all([loadAll(), loadPatterns()]).finally(() => setLoading(false))
  }, [])

  const refresh = async () => {
    setRefreshing(true)
    await loadAll()
    setRefreshing(false)
  }

  const dbLabel = 'PostgreSQL 16'
  const vectorLabel = healthStatus?.vector_store === 'milvus' ? 'Milvus Standalone' : '未知向量库后端'
  const modelLabel = healthStatus?.vector_store === 'milvus' ? 'bge-large-zh-v1.5 (1024维)' : '未知嵌入模型'

  const categories = ['', 'crud', 'auth', 'state_machine', 'validation', 'workflow', 'concurrency', 'async', 'consistency']
  const catLabels: Record<string, string> = { '': '全部', crud: 'CRUD', auth: '认证授权', state_machine: '状态机', validation: '输入校验', workflow: '工作流', concurrency: '并发', async: '异步回调', consistency: '数据一致性' }

  // Collection 中文名映射
  const collLabels: Record<string, string> = {
    requirements: '需求向量',
    test_cases: '用例向量',
    patterns: '测试模式向量',
    errors: '错误案例向量',
  }

  if (loading) return <div className="p-6">加载中...</div>

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="page-title flex items-center gap-2">
            <Database className="w-6 h-6 text-primary-500" /> 知识库管理
          </h1>
          <p className="text-sm text-slate-500 mt-1">向量库配置、健康诊断、统计信息与测试模式管理</p>
        </div>
        <button onClick={refresh} disabled={refreshing} className="btn-ghost text-xs flex items-center gap-1">
          <RefreshCw className={`w-3 h-3 ${refreshing ? 'animate-spin' : ''}`} /> 刷新诊断
        </button>
      </div>

      {/* 系统警告 */}
      {diag?.warnings && diag.warnings.length > 0 && (
        <div className="p-4 rounded-xl bg-amber-50 border border-amber-200 space-y-2">
          <div className="flex items-center gap-2 text-amber-700">
            <AlertTriangle className="w-5 h-5" />
            <span className="text-sm font-semibold">系统警告（{diag.warnings.length} 条）</span>
          </div>
          {diag.warnings.map((w, i) => (
            <p key={i} className="text-xs text-amber-600 leading-relaxed ml-7">{w}</p>
          ))}
        </div>
      )}

      {/* 统计 */}
      {stats && (
        <div className="grid grid-cols-4 gap-4">
          {[
            ['需求', stats.total_requirements, 'text-blue-600'],
            ['测试用例', stats.total_test_cases, 'text-accent-500'],
            ['测试模式', stats.total_patterns, 'text-emerald-500'],
            ['生成批次', stats.total_batches, 'text-amber-500'],
          ].map(([label, val, color]) => (
            <div key={label as string} className="card text-center">
              <p className={`text-2xl font-bold ${color}`}>{val}</p>
              <p className="text-xs text-slate-400">{label}</p>
            </div>
          ))}
        </div>
      )}

      {/* 向量库健康诊断 */}
      <div className="card">
        <h2 className="section-title flex items-center gap-2 mb-4">
          <Activity className="w-4 h-4 text-accent-500" /> 向量库健康诊断
        </h2>
        <div className="grid grid-cols-3 gap-4 mb-4">
          {/* 连接状态 */}
          <div className="p-3 bg-slate-50/50 rounded-xl">
            <div className="flex items-center gap-2 mb-1">
              <Network className="w-3.5 h-3.5 text-slate-400" />
              <span className="text-xs text-slate-500">Milvus 连接</span>
            </div>
            {diag?.vector_db.connected || healthStatus?.vector_db?.connected ? (
              <span className="flex items-center gap-1 text-sm font-medium text-emerald-600">
                <CheckCircle2 className="w-3.5 h-3.5" /> 已连接
              </span>
            ) : (
              <span className="flex items-center gap-1 text-sm font-medium text-red-500">
                <XCircle className="w-3.5 h-3.5" /> 未连接
              </span>
            )}
          </div>
          {/* 嵌入模型 */}
          <div className="p-3 bg-slate-50/50 rounded-xl">
            <div className="flex items-center gap-2 mb-1">
              <Boxes className="w-3.5 h-3.5 text-slate-400" />
              <span className="text-xs text-slate-500">嵌入模型</span>
            </div>
            {(diag?.vector_db.model_ready ?? healthStatus?.vector_db?.model_ready) ? (
              <span className="flex items-center gap-1 text-sm font-medium text-emerald-600">
                <CheckCircle2 className="w-3.5 h-3.5" /> 就绪
              </span>
            ) : (
              <span className="flex items-center gap-1 text-sm font-medium text-red-500">
                <XCircle className="w-3.5 h-3.5" /> 未就绪
              </span>
            )}
            <p className="text-[10px] text-slate-400 mt-0.5">{modelLabel}</p>
          </div>
          {/* 冷启动 */}
          <div className="p-3 bg-slate-50/50 rounded-xl">
            <div className="flex items-center gap-2 mb-1">
              <Database className="w-3.5 h-3.5 text-slate-400" />
              <span className="text-xs text-slate-500">知识库状态</span>
            </div>
            {healthStatus?.kb_empty ? (
              <span className="flex items-center gap-1 text-sm font-medium text-amber-600">
                <AlertTriangle className="w-3.5 h-3.5" /> 冷启动（空）
              </span>
            ) : (
              <span className="flex items-center gap-1 text-sm font-medium text-emerald-600">
                <CheckCircle2 className="w-3.5 h-3.5" /> 已有数据
              </span>
            )}
          </div>
        </div>

        {/* Collection 计数 */}
        <div className="mb-3">
          <p className="text-xs text-slate-500 mb-2">Collection 向量计数</p>
          <div className="grid grid-cols-4 gap-3">
            {Object.entries(diag?.vector_db.collections || healthStatus?.vector_db?.collections || {}).map(([key, val]) => {
              const num = typeof val === 'number' ? val : val.num_entities
              const desc = typeof val === 'object' && val !== null ? val.description : ''
              const hasError = typeof val === 'object' && val !== null && val.error
              return (
                <div key={key} className="p-3 bg-slate-50/50 rounded-xl">
                  <p className="text-[10px] text-slate-400">{collLabels[key] || key}</p>
                  <p className={`text-lg font-bold ${num < 0 ? 'text-red-500' : 'text-slate-700'}`}>{num < 0 ? '—' : num}</p>
                  {desc && <p className="text-[10px] text-slate-400 mt-0.5">{desc}</p>}
                  {hasError && (
                    <p className="text-[10px] text-red-400 mt-0.5" title={val.error}>
                      <AlertTriangle className="w-2.5 h-2.5 inline" /> 错误
                    </p>
                  )}
                </div>
              )
            })}
          </div>
        </div>

        {/* 错误信息 */}
        {(diag?.vector_db.error || healthStatus?.vector_db?.error) && (
          <div className="p-3 bg-red-50 rounded-xl text-xs text-red-600 flex items-start gap-2">
            <AlertTriangle className="w-3.5 h-3.5 mt-0.5 flex-shrink-0" />
            <span>{diag?.vector_db.error || healthStatus?.vector_db?.error}</span>
          </div>
        )}
      </div>

      {/* 系统配置 */}
      <div className="grid grid-cols-2 gap-4">
        <div className="card">
          <h2 className="section-title flex items-center gap-2 mb-3">
            <Database className="w-4 h-4 text-primary-500" /> 关系数据库
          </h2>
          <div className="space-y-2">
            <Row label="引擎" value={dbLabel} />
            <Row label="LLM" value={`${healthStatus?.llm_provider || '-'} ${healthStatus?.llm_configured ? '(已配置)' : '(未配置)'}`} />
            <Row label="版本" value={`v${healthStatus?.version || '-'}`} />
          </div>
        </div>
        <div className="card">
          <h2 className="section-title flex items-center gap-2 mb-3">
            <Server className="w-4 h-4 text-accent-500" /> 向量数据库
          </h2>
          <div className="space-y-2">
            <Row label="引擎" value={vectorLabel} />
            <Row label="嵌入模型" value={modelLabel} />
            <Row label="状态" value={
              diag?.vector_db.connected || healthStatus?.vector_db?.connected
                ? <span className="text-emerald-600 text-xs font-medium">运行中</span>
                : <span className="text-red-500 text-xs font-medium">未连接</span>
            } />
          </div>
        </div>
      </div>

      {/* 知识库架构说明 */}
      <div className="card">
        <h2 className="section-title flex items-center gap-2 mb-3">
          <Info className="w-4 h-4 text-primary-500" /> 知识库架构
        </h2>
        <div className="grid grid-cols-2 gap-3">
          <ArchRow
            icon={<Database className="w-4 h-4 text-blue-500" />}
            name="PostgreSQL"
            role="关系数据库"
            desc="存储需求/用例/模式元数据与 SRR JSON"
            port="5432"
          />
          <ArchRow
            icon={<Server className="w-4 h-4 text-accent-500" />}
            name="Milvus"
            role="向量数据库"
            desc="向量检索引擎，存储 1024 维语义向量"
            port="19530"
          />
          <ArchRow
            icon={<HardDrive className="w-4 h-4 text-emerald-500" />}
            name="MinIO"
            role="对象存储"
            desc="Milvus 的存储后端，存储向量数据文件（非独立 KB 管理器）"
            port="9001"
          />
          <ArchRow
            icon={<Network className="w-4 h-4 text-amber-500" />}
            name="etcd"
            role="协调服务"
            desc="Milvus 的元数据管理与协调"
            port="2379"
          />
        </div>
        <div className="mt-3 p-3 bg-blue-50/50 rounded-xl text-xs text-blue-700 flex items-start gap-2">
          <Info className="w-3.5 h-3.5 mt-0.5 flex-shrink-0" />
          <div>
            <p className="font-medium">关于 MinIO</p>
            <p className="mt-0.5 text-blue-600">MinIO 是 S3 兼容的对象存储，作为 Milvus 的持久化存储后端使用，负责存储向量数据文件。它不是独立的向量知识库管理器，向量检索能力由 Milvus 提供。</p>
          </div>
        </div>
      </div>

      {/* 测试模式库 */}
      <div className="card">
        <h2 className="section-title flex items-center gap-2 mb-4">
          <BookOpen className="w-4 h-4 text-emerald-500" /> 测试模式库
        </h2>
        <div className="flex flex-wrap gap-1.5 mb-4">
          {categories.map(c => (
            <button key={c} onClick={() => { setCategory(c); loadPatterns(c) }}
              className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-all ${
                category === c ? 'bg-primary-100 text-primary-700' : 'bg-slate-50 text-slate-500 hover:bg-slate-100'
              }`}>
              {catLabels[c] || c}
            </button>
          ))}
        </div>
        <div className="grid grid-cols-2 gap-3">
          {patterns.map(p => (
            <div key={p.id} className="p-4 bg-slate-50/50 rounded-xl">
              <p className="text-sm font-semibold text-slate-700">
                {String((p.pattern_json as any)?.pattern_name ?? p.name)}
              </p>
              <span className="badge bg-slate-100 text-slate-500 mt-1">{p.category}</span>
              <div className="flex items-center gap-4 text-xs text-slate-400 mt-2">
                <span>使用 {p.usage_count} 次</span>
                <span>成功率 {((p.success_rate || 0) * 100).toFixed(0)}%</span>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return <div className="flex items-center justify-between text-sm"><span className="text-slate-500">{label}</span><span className="font-medium text-slate-700">{value}</span></div>
}

function ArchRow({ icon, name, role, desc, port }: {
  icon: React.ReactNode
  name: string
  role: string
  desc: string
  port: string
}) {
  return (
    <div className="p-3 bg-slate-50/50 rounded-xl">
      <div className="flex items-center gap-2 mb-1">
        {icon}
        <span className="text-sm font-semibold text-slate-700">{name}</span>
        <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-200 text-slate-600">{role}</span>
        <span className="text-[10px] text-slate-400 ml-auto">:{port}</span>
      </div>
      <p className="text-xs text-slate-500">{desc}</p>
    </div>
  )
}
