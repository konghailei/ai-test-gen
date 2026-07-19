import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { 
  FileText, TestTube, Database, Activity, TrendingUp, Layers,
  ArrowRight, Play, Sparkles, AlertCircle, CheckCircle2
} from 'lucide-react'
import { knowledge, health, requirements, testCases } from '../api/client'
import { useProduct } from '../store/ProductContext'
import type { KnowledgeStats, HealthStatus, Requirement, TestCase } from '../types'
import { formatDate, priorityBadge, statusBadge, CASE_STATUS_LABELS } from '../utils'

export default function Dashboard() {
  const { selectedProductId, selectedProductName } = useProduct()
  const [stats, setStats] = useState<KnowledgeStats | null>(null)
  const [healthStatus, setHealth] = useState<HealthStatus | null>(null)
  const [recentReqs, setRecentReqs] = useState<Requirement[]>([])
  const [recentCases, setRecentCases] = useState<TestCase[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    Promise.all([
      knowledge.stats(selectedProductId),
      health(),
      requirements.list(1, 5, selectedProductId),
      testCases.list(undefined, undefined, undefined, 1, 5, selectedProductId),
    ]).then(([s, h, r, c]) => {
      setStats(s)
      setHealth(h)
      setRecentReqs(r.items || [])
      setRecentCases(c.items || [])
    }).catch(() => {}).finally(() => setLoading(false))
  }, [selectedProductId])

  const cards = [
    { label: '需求总数', value: stats?.total_requirements ?? '-', icon: FileText, color: 'from-primary-500 to-primary-600', href: '/requirements' },
    { label: '测试用例', value: stats?.total_test_cases ?? '-', icon: TestTube, color: 'from-accent-500 to-accent-600', href: '/testcases' },
    { label: '测试模式', value: stats?.total_patterns ?? '-', icon: Layers, color: 'from-emerald-500 to-emerald-600', href: '/knowledge' },
    { label: '生成批次', value: stats?.total_batches ?? '-', icon: Activity, color: 'from-amber-500 to-amber-600', href: '/generate' },
  ]

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="page-title">仪表盘</h1>
          <p className="text-sm text-slate-500 mt-1">AI测试用例生成系统 概览与快速操作</p>
        </div>
        <Link to="/generate" className="btn-primary">
          <Play className="w-4 h-4" /> 开始生成
        </Link>
      </div>

      {/* Cold start banner */}
      {stats?.kb_empty && (
        <div className="bg-gradient-to-r from-blue-50 to-indigo-50 border border-blue-100 rounded-2xl p-5 flex items-start gap-4">
          <Sparkles className="w-6 h-6 text-blue-500 flex-shrink-0 mt-0.5" />
          <div>
            <h3 className="font-semibold text-blue-800 text-sm">冷启动模式 — 知识库为空</h3>
            <p className="text-sm text-blue-600/80 mt-1">当前知识库尚无数据，系统已自动切换到冷启动模式。上传第一个需求后，ClarifyAgent 会生成深度澄清问题，确保需求质量。首个需求完成后将自动入库，成为后续需求的参考基准。</p>
          </div>
        </div>
      )}

      {/* Not configured */}
      {healthStatus && !healthStatus.llm_configured && (
        <div className="bg-gradient-to-r from-amber-50 to-orange-50 border border-amber-100 rounded-2xl p-5 flex items-start gap-4">
          <AlertCircle className="w-6 h-6 text-amber-500 flex-shrink-0 mt-0.5" />
          <div className="flex-1">
            <h3 className="font-semibold text-amber-800 text-sm">LLM 未配置</h3>
            <p className="text-sm text-amber-600/80 mt-1">请先在系统设置中配置 DeepSeek 或 OpenAI 的 API Key，否则所有 Agent 将无法调用 LLM。</p>
            <Link to="/settings" className="inline-flex items-center gap-1 text-sm font-medium text-amber-700 mt-2 hover:text-amber-800">
              前往设置 <ArrowRight className="w-3.5 h-3.5" />
            </Link>
          </div>
        </div>
      )}

      {/* Stat cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        {cards.map((card) => (
          <Link key={card.label} to={card.href} className="card card-hover group">
            <div className="flex items-start justify-between">
              <div>
                <p className="text-sm text-slate-400">{card.label}</p>
                <p className="text-3xl font-bold text-slate-800 mt-1">{card.value}</p>
              </div>
              <div className={`w-10 h-10 rounded-xl bg-gradient-to-br ${card.color} flex items-center justify-center shadow-sm group-hover:scale-110 transition-transform`}>
                <card.icon className="w-5 h-5 text-white" />
              </div>
            </div>
            <div className="mt-3 flex items-center gap-1 text-xs text-primary-500 opacity-0 group-hover:opacity-100 transition-opacity">
              查看详情 <ArrowRight className="w-3 h-3" />
            </div>
          </Link>
        ))}
      </div>

      {/* Recent activity */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Recent requirements */}
        <div className="card">
          <div className="flex items-center justify-between mb-4">
            <h2 className="section-title flex items-center gap-2">
              <FileText className="w-4 h-4 text-primary-500" /> 最近需求
            </h2>
            <Link to="/requirements" className="text-xs text-primary-500 hover:text-primary-600 font-medium">查看全部</Link>
          </div>
          {loading ? (
            <div className="space-y-3">{[1,2,3].map(i => <div key={i} className="skeleton h-12" />)}</div>
          ) : recentReqs.length === 0 ? (
            <div className="text-center py-8 text-slate-400">
              <FileText className="w-8 h-8 mx-auto mb-2 opacity-50" />
              <p className="text-sm">暂无需求数据</p>
              <Link to="/requirements" className="text-xs text-primary-500 mt-2 inline-block">上传第一个需求</Link>
            </div>
          ) : (
            <div className="space-y-2">
              {recentReqs.map((r) => (
                <Link key={r.id} to={`/requirements/${r.id}`} className="flex items-center justify-between p-3 rounded-xl hover:bg-slate-50 transition-colors group">
                  <div className="flex-1 min-w-0">
                    <p className="text-sm font-medium text-slate-700 truncate">{r.title}</p>
                    <p className="text-xs text-slate-400 mt-0.5">{r.id} · {formatDate(r.created_at)}</p>
                  </div>
                  <span className={priorityBadge(r.priority)}>{r.priority}</span>
                </Link>
              ))}
            </div>
          )}
        </div>

        {/* Recent test cases */}
        <div className="card">
          <div className="flex items-center justify-between mb-4">
            <h2 className="section-title flex items-center gap-2">
              <TestTube className="w-4 h-4 text-accent-500" /> 最近用例
            </h2>
            <Link to="/testcases" className="text-xs text-primary-500 hover:text-primary-600 font-medium">查看全部</Link>
          </div>
          {loading ? (
            <div className="space-y-3">{[1,2,3].map(i => <div key={i} className="skeleton h-12" />)}</div>
          ) : recentCases.length === 0 ? (
            <div className="text-center py-8 text-slate-400">
              <TestTube className="w-8 h-8 mx-auto mb-2 opacity-50" />
              <p className="text-sm">暂无用例数据</p>
              <Link to="/generate" className="text-xs text-primary-500 mt-2 inline-block">生成测试用例</Link>
            </div>
          ) : (
            <div className="space-y-2">
              {recentCases.map((c) => (
                <div key={c.id} className="flex items-center justify-between p-3 rounded-xl hover:bg-slate-50 transition-colors">
                  <div className="flex-1 min-w-0">
                    <p className="text-sm font-medium text-slate-700 truncate">{c.title}</p>
                    <p className="text-xs text-slate-400 mt-0.5">{c.id} · {c.type}</p>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className={priorityBadge(c.priority)}>{c.priority}</span>
                    <span className={statusBadge(c.status)}>{CASE_STATUS_LABELS[c.status] || c.status}</span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
