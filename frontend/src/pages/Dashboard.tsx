import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import {
  FileText, TestTube, Database, Activity, Layers,
  ArrowRight, Play, Sparkles, AlertCircle, ChevronRight
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
    { label: '需求总数', value: stats?.total_requirements ?? '-', icon: FileText, href: '/requirements' },
    { label: '测试用例', value: stats?.total_test_cases ?? '-', icon: TestTube, href: '/testcases' },
    { label: '测试模式', value: stats?.total_patterns ?? '-', icon: Layers, href: '/knowledge' },
    { label: '生成批次', value: stats?.total_batches ?? '-', icon: Activity, href: '/generate' },
  ]

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-4">
        <div>
          <h1 className="page-title">仪表盘</h1>
          <p className="text-sm mt-1" style={{ color: 'var(--foreground-tertiary)' }}>
            {selectedProductName ? `${selectedProductName} · ` : ''}概览与快速操作
          </p>
        </div>
        <Link to="/generate" className="btn-primary">
          <Play className="w-4 h-4" /> 开始生成
        </Link>
      </div>

      {/* Cold start banner */}
      {stats?.kb_empty && (
        <div
          className="flex items-start gap-4 animate-slide-up"
          style={{
            padding: 20,
            borderRadius: 'var(--radius)',
            background: '#e8f2ff',
            border: '1px solid #cfe5ff',
          }}
        >
          <Sparkles className="w-6 h-6 flex-shrink-0 mt-0.5" style={{ color: 'var(--brand-500)' }} />
          <div>
            <h3 className="font-semibold text-sm" style={{ color: 'var(--brand-700)' }}>冷启动模式 — 知识库为空</h3>
            <p className="text-sm mt-1" style={{ color: 'var(--brand-600)' }}>
              当前知识库尚无数据，系统已自动切换到冷启动模式。上传第一个需求后，ClarifyAgent 会生成深度澄清问题，确保需求质量。首个需求完成后将自动入库，成为后续需求的参考基准。
            </p>
          </div>
        </div>
      )}

      {/* Not configured */}
      {healthStatus && !healthStatus.llm_configured && (
        <div
          className="flex items-start gap-4 animate-slide-up"
          style={{
            padding: 20,
            borderRadius: 'var(--radius)',
            background: 'var(--state-warning-surface)',
            border: '1px solid #ffe0b3',
          }}
        >
          <AlertCircle className="w-6 h-6 flex-shrink-0 mt-0.5" style={{ color: 'var(--state-warning)' }} />
          <div className="flex-1">
            <h3 className="font-semibold text-sm" style={{ color: 'var(--state-warning)' }}>LLM 未配置</h3>
            <p className="text-sm mt-1" style={{ color: 'var(--state-warning)', opacity: 0.8 }}>
              请先在系统设置中配置 DeepSeek 或 OpenAI 的 API Key，否则所有 Agent 将无法调用 LLM。
            </p>
            <Link to="/admin/settings" className="inline-flex items-center gap-1 text-sm font-medium mt-2" style={{ color: 'var(--state-warning)' }}>
              前往设置 <ArrowRight className="w-3.5 h-3.5" />
            </Link>
          </div>
        </div>
      )}

      {/* Stat cards — Apple 风格：圆形图标 + 大数字 */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        {cards.map((card) => (
          <Link
            key={card.label}
            to={card.href}
            className="card card-hover group"
            style={{ padding: 24 }}
          >
            <div className="flex items-center gap-2.5 mb-4">
              <div
                className="flex items-center justify-center flex-shrink-0"
                style={{
                  width: 32, height: 32, borderRadius: '50%',
                  background: 'var(--background-muted)',
                  color: 'var(--foreground)',
                }}
              >
                <card.icon className="w-4 h-4" />
              </div>
              <span className="text-[13px]" style={{ color: 'var(--foreground-tertiary)' }}>{card.label}</span>
            </div>
            <p className="m-0 mb-2" style={{ fontSize: 32, fontWeight: 700, color: 'var(--foreground)', lineHeight: 1.1, letterSpacing: '-0.02em' }}>
              {card.value}
            </p>
            <div className="flex items-center gap-1 text-xs font-medium" style={{ color: 'var(--brand-500)' }}>
              查看详情 <ChevronRight className="w-3 h-3" />
            </div>
          </Link>
        ))}
      </div>

      {/* Recent activity */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Recent requirements */}
        <div className="card" style={{ padding: 0 }}>
          <div
            className="flex items-center justify-between"
            style={{ padding: '20px 24px', borderBottom: '1px solid var(--border)' }}
          >
            <h2 className="section-title flex items-center gap-2 m-0" style={{ fontSize: 16 }}>
              <FileText className="w-4 h-4" style={{ color: 'var(--brand-500)' }} /> 最近需求
            </h2>
            <Link to="/requirements" className="text-[13px] font-medium" style={{ color: 'var(--brand-500)' }}>
              查看全部
            </Link>
          </div>
          <div style={{ padding: '8px' }}>
            {loading ? (
              <div className="space-y-2 p-4">{[1, 2, 3].map(i => <div key={i} className="skeleton h-12" />)}</div>
            ) : recentReqs.length === 0 ? (
              <div className="text-center py-8" style={{ color: 'var(--foreground-tertiary)' }}>
                <FileText className="w-8 h-8 mx-auto mb-2 opacity-50" />
                <p className="text-sm">暂无需求数据</p>
                <Link to="/requirements" className="text-xs mt-2 inline-block" style={{ color: 'var(--brand-500)' }}>上传第一个需求</Link>
              </div>
            ) : (
              <div className="space-y-1">
                {recentReqs.map((r) => (
                  <Link
                    key={r.id}
                    to={`/requirements/${r.id}`}
                    className="flex items-center justify-between transition-colors group"
                    style={{ padding: '10px 12px', borderRadius: 'calc(var(--radius) * 0.4)' }}
                    onMouseEnter={(e) => { e.currentTarget.style.background = 'var(--background-muted)' }}
                    onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent' }}
                  >
                    <div className="flex-1 min-w-0">
                      <p className="text-sm font-medium truncate" style={{ color: 'var(--foreground)' }}>{r.title}</p>
                      <p className="text-xs mt-0.5" style={{ color: 'var(--foreground-tertiary)' }}>{r.id} · {formatDate(r.created_at)}</p>
                    </div>
                    <span className={priorityBadge(r.priority)}>{r.priority}</span>
                  </Link>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* Recent test cases */}
        <div className="card" style={{ padding: 0 }}>
          <div
            className="flex items-center justify-between"
            style={{ padding: '20px 24px', borderBottom: '1px solid var(--border)' }}
          >
            <h2 className="section-title flex items-center gap-2 m-0" style={{ fontSize: 16 }}>
              <TestTube className="w-4 h-4" style={{ color: 'var(--brand-500)' }} /> 最近用例
            </h2>
            <Link to="/testcases" className="text-[13px] font-medium" style={{ color: 'var(--brand-500)' }}>
              查看全部
            </Link>
          </div>
          <div style={{ padding: '8px' }}>
            {loading ? (
              <div className="space-y-2 p-4">{[1, 2, 3].map(i => <div key={i} className="skeleton h-12" />)}</div>
            ) : recentCases.length === 0 ? (
              <div className="text-center py-8" style={{ color: 'var(--foreground-tertiary)' }}>
                <TestTube className="w-8 h-8 mx-auto mb-2 opacity-50" />
                <p className="text-sm">暂无用例数据</p>
                <Link to="/generate" className="text-xs mt-2 inline-block" style={{ color: 'var(--brand-500)' }}>生成测试用例</Link>
              </div>
            ) : (
              <div className="space-y-1">
                {recentCases.map((c) => (
                  <div
                    key={c.id}
                    className="flex items-center justify-between transition-colors"
                    style={{ padding: '10px 12px', borderRadius: 'calc(var(--radius) * 0.4)' }}
                    onMouseEnter={(e) => { e.currentTarget.style.background = 'var(--background-muted)' }}
                    onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent' }}
                  >
                    <div className="flex-1 min-w-0">
                      <p className="text-sm font-medium truncate" style={{ color: 'var(--foreground)' }}>{c.title}</p>
                      <p className="text-xs mt-0.5" style={{ color: 'var(--foreground-tertiary)' }}>{c.id} · {c.type}</p>
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
    </div>
  )
}
