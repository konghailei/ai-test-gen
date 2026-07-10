import { useState, useEffect } from 'react'
import { Database, Server, Activity, BookOpen } from 'lucide-react'
import { knowledge, health } from '../api/client'
import type { KnowledgeStats, TestPattern, HealthStatus } from '../types'

export default function AdminKnowledgeBase() {
  const [stats, setStats] = useState<KnowledgeStats | null>(null)
  const [healthStatus, setHealth] = useState<HealthStatus & { db_type?: string; vector_store?: string } | null>(null)
  const [patterns, setPatterns] = useState<TestPattern[]>([])
  const [category, setCategory] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    Promise.all([knowledge.stats(), health(), knowledge.patterns()]).then(([s, h, p]) => {
      setStats(s); setHealth(h as any); setPatterns((p as any).items || [])
    }).finally(() => setLoading(false))
  }, [])

  const loadPatterns = async (cat?: string) => {
    try { const r = await knowledge.patterns(cat || undefined); setPatterns((r as any).items || []) } catch {}
  }

  const dbLabel = healthStatus?.db_type === 'postgresql' ? 'PostgreSQL 16' : 'SQLite'
  const vectorLabel = healthStatus?.vector_store === 'milvus' ? 'Milvus Standalone' : 'ChromaDB'
  const modelLabel = healthStatus?.vector_store === 'milvus' ? 'bge-large-zh-v1.5 (1024维)' : 'ONNX all-MiniLM-L6-v2'

  const categories = ['', 'crud', 'auth', 'state_machine', 'validation', 'workflow', 'concurrency', 'async', 'consistency']
  const catLabels: Record<string, string> = { '': '全部', crud: 'CRUD', auth: '认证授权', state_machine: '状态机', validation: '输入校验', workflow: '工作流', concurrency: '并发', async: '异步回调', consistency: '数据一致性' }

  if (loading) return <div className="p-6">加载中...</div>

  return (
    <div className="space-y-6">
      <div>
        <h1 className="page-title flex items-center gap-2">
          <Database className="w-6 h-6 text-primary-500" /> 知识库
        </h1>
        <p className="text-sm text-slate-500 mt-1">向量库配置、统计信息与测试模式管理</p>
      </div>

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
            <Row label="状态" value={<span className="badge bg-success-50 text-success-600">运行中</span>} />
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
