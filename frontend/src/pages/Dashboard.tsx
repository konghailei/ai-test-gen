import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import {
  FileText, TestTube, Database, Activity, Layers,
  ArrowRight, Play, Sparkles, AlertCircle, ChevronRight,
  Cpu, Gauge, Users, Timer
} from 'lucide-react'
import { knowledge, health, requirements, testCases, observability } from '../api/client'
import { useProduct } from '../store/ProductContext'
import type { KnowledgeStats, HealthStatus, Requirement, TestCase, SystemResources } from '../types'
import { formatDate, priorityBadge, statusBadge, CASE_STATUS_LABELS } from '../utils'

/** 资源监控轮询间隔（ms）——10s 一刷，与 AdminDashboard 的出箱巡检同量级 */
const RESOURCE_POLL_MS = 10000

/** 按使用率取语义色：<60 正常 / 60-85 警告 / ≥85 危险 */
function usageColor(pct: number | null | undefined): string {
  if (pct == null) return 'var(--foreground-tertiary)'
  if (pct >= 85) return 'var(--state-error)'
  if (pct >= 60) return 'var(--state-warning)'
  return 'var(--state-success)'
}

/** 通用进度条（细条，Apple 风格） */
function Meter({ label, value, display, color, hint }: {
  label: string; value: number | null | undefined; display: string; color: string; hint?: string
}) {
  const pct = value == null ? 0 : Math.max(0, Math.min(100, value))
  return (
    <div>
      <div className="flex items-center justify-between mb-1.5">
        <span className="text-[13px]" style={{ color: 'var(--foreground-tertiary)' }}>{label}</span>
        <span className="text-[13px] font-semibold tabular-nums" style={{ color }}>{display}</span>
      </div>
      <div style={{ height: 6, borderRadius: 3, background: 'var(--background-muted)', overflow: 'hidden' }}>
        <div style={{
          width: `${pct}%`, height: '100%', borderRadius: 3, background: color,
          transition: 'width .4s ease',
        }} />
      </div>
      {hint && (
        <p className="text-[11px] mt-1" style={{ color: 'var(--foreground-tertiary)' }}>{hint}</p>
      )}
    </div>
  )
}

export default function Dashboard() {
  const { selectedProductId, selectedProductName } = useProduct()
  const [stats, setStats] = useState<KnowledgeStats | null>(null)
  const [healthStatus, setHealth] = useState<HealthStatus | null>(null)
  const [recentReqs, setRecentReqs] = useState<Requirement[]>([])
  const [recentCases, setRecentCases] = useState<TestCase[]>([])
  const [loading, setLoading] = useState(true)
  // v4.0.2：服务器资源与并发生成负载
  const [res, setRes] = useState<SystemResources | null>(null)

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

  // 资源监控：独立轮询，失败静默（探活/业务数据不应被监控拖垮）
  useEffect(() => {
    let alive = true
    const load = () => observability.systemResources()
      .then(d => { if (alive) setRes(d) })
      .catch(() => {})
    load()
    const t = setInterval(load, RESOURCE_POLL_MS)
    return () => { alive = false; clearInterval(t) }
  }, [])

  const cards = [
    { label: '需求总数', value: stats?.total_requirements ?? '-', icon: FileText, href: '/projects' },
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

      {/* v4.0.2：服务器资源与并发生成负载。
          系统是平台（多人同时点生成），并发度按 CPU 核数动态收敛——
          这里把"为什么我的生成在排队"变成一眼可见，而不是让用户干等。 */}
      <div className="card" style={{ padding: 24 }}>
        <div className="flex items-center justify-between flex-wrap gap-2 mb-5">
          <h2 className="section-title flex items-center gap-2 m-0" style={{ fontSize: 16 }}>
            <Gauge className="w-4 h-4" style={{ color: 'var(--brand-500)' }} /> 服务器资源与生成并发
          </h2>
          <span className="text-xs tabular-nums" style={{ color: 'var(--foreground-tertiary)' }}>
            {res?.sampled_at ? `采样于 ${res.sampled_at.slice(11, 19)}` : '加载中…'}
          </span>
        </div>

        {!res ? (
          <div className="space-y-3">{[1, 2, 3].map(i => <div key={i} className="skeleton h-10" />)}</div>
        ) : (
          <>
            {/* 资源紧张提示：并发已满 / CPU 打满 / 内存偏低 */}
            {(res.generation.waiting > 0
              || (res.cpu.usage_percent_avg ?? 0) >= 85
              || (res.memory.available_ratio ?? 1) < 0.15) && (
              <div
                className="flex items-start gap-3 mb-5"
                style={{
                  padding: '12px 16px', borderRadius: 'calc(var(--radius) * 0.6)',
                  background: 'var(--state-warning-surface)', border: '1px solid #ffe0b3',
                }}
              >
                <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5" style={{ color: 'var(--state-warning)' }} />
                <p className="text-[13px] m-0" style={{ color: 'var(--state-warning)' }}>
                  {res.generation.waiting > 0
                    ? `当前有 ${res.generation.waiting} 个生成任务在排队等待资源释放；`
                    : ''}
                  {(res.cpu.usage_percent_avg ?? 0) >= 85 ? 'CPU 已接近饱和，新任务的并发度会自动下调；' : ''}
                  {(res.memory.available_ratio ?? 1) < 0.15 ? '可用内存偏低，并发已压到最低以防 OOM。' : ''}
                  {' 稍后再试，或先取消进行中的任务。'}
                </p>
              </div>
            )}

            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-x-6 gap-y-5">
              <Meter
                label="CPU 使用率"
                value={res.cpu.usage_percent_avg}
                display={res.cpu.usage_percent_avg == null ? '—' : `${res.cpu.usage_percent_avg}%`}
                color={usageColor(res.cpu.usage_percent_avg)}
                hint={`${res.cpu.physical_cores} 物理核 / ${res.cpu.logical_cores} 逻辑核`}
              />
              <Meter
                label="内存可用"
                value={res.memory.available_ratio == null ? null : res.memory.available_ratio * 100}
                display={res.memory.available_ratio == null
                  ? `${res.memory.available_mb} MB`
                  : `${res.memory.available_mb} / ${res.memory.total_mb} MB`}
                color={usageColor(res.memory.available_ratio == null
                  ? null : (1 - res.memory.available_ratio) * 100)}
                hint={res.memory.source === 'cgroup' ? '容器内存上限' : '宿主机内存'}
              />
              <Meter
                label="系统负载（1 分钟）"
                value={res.cpu.load_1m == null ? null : (res.cpu.load_1m / Math.max(1, res.cpu.logical_cores)) * 100}
                display={res.cpu.load_1m == null ? '—' : res.cpu.load_1m.toFixed(2)}
                color={usageColor(res.cpu.load_1m == null
                  ? null : (res.cpu.load_1m / Math.max(1, res.cpu.logical_cores)) * 100)}
                hint={`5 分钟 ${res.cpu.load_5m ?? '—'} · 15 分钟 ${res.cpu.load_15m ?? '—'}`}
              />
              {/* 并发生成：数字卡片而非进度条（"2 / 3"比百分比直观） */}
              <div>
                <div className="flex items-center justify-between mb-1.5">
                  <span className="text-[13px]" style={{ color: 'var(--foreground-tertiary)' }}>并发生成任务</span>
                  <span
                    className="text-[13px] font-semibold tabular-nums"
                    style={{ color: res.generation.waiting > 0 ? 'var(--state-warning)' : 'var(--foreground)' }}
                  >
                    {res.generation.active} / {res.generation.ceiling}
                  </span>
                </div>
                <div style={{ height: 6, borderRadius: 3, background: 'var(--background-muted)', overflow: 'hidden' }}>
                  <div style={{
                    width: `${Math.min(100, (res.generation.active / Math.max(1, res.generation.ceiling)) * 100)}%`,
                    height: '100%', borderRadius: 3,
                    background: res.generation.waiting > 0 ? 'var(--state-warning)' : 'var(--brand-500)',
                    transition: 'width .4s ease',
                  }} />
                </div>
                <p className="text-[11px] mt-1" style={{ color: 'var(--foreground-tertiary)' }}>
                  {res.generation.waiting > 0
                    ? `排队等待 ${res.generation.waiting} 个 · `
                    : ''}
                  推理份额 {res.inference_slots} 核（本地模型推理预算）
                </p>
              </div>
            </div>

            {/* 底部一行小指标：把 S1 治理后的两块指标也透出来，
                "生成期会不会又卡"从此是可见的而不是靠感觉 */}
            <div
              className="flex items-center flex-wrap gap-x-6 gap-y-2 mt-5 pt-4 text-[12px]"
              style={{ borderTop: '1px solid var(--border)', color: 'var(--foreground-tertiary)' }}
            >
              <span className="flex items-center gap-1.5">
                <Activity className="w-3.5 h-3.5" />
                事件循环最大卡顿{' '}
                <b className="tabular-nums" style={{ color: 'var(--foreground)' }}>
                  {res.event_loop?.max_lag_ms ?? '—'} ms
                </b>
                {res.event_loop && (res.event_loop.lag_error_count ?? 0) > 0 && (
                  <span style={{ color: 'var(--state-error)' }}>
                    （error 级 {res.event_loop.lag_error_count} 次）
                  </span>
                )}
              </span>
              <span className="flex items-center gap-1.5">
                <Cpu className="w-3.5 h-3.5" />
                LLM 执行线程{' '}
                <b style={{ color: res.llm_executor?.isolated_loop ? 'var(--state-success)' : 'var(--state-warning)' }}>
                  {res.llm_executor?.isolated_loop ? '已隔离' : '未隔离'}
                </b>
              </span>
              <span className="flex items-center gap-1.5">
                <Users className="w-3.5 h-3.5" />
                累计准入 {res.generation.admitted_total} · 拒绝 {res.generation.rejected_total}
              </span>
              {res.generation.max_wait_ms > 0 && (
                <span className="flex items-center gap-1.5">
                  <Timer className="w-3.5 h-3.5" />
                  最长排队 {Math.round(res.generation.max_wait_ms / 1000)} s
                </span>
              )}
            </div>
          </>
        )}
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
            <Link to="/projects" className="text-[13px] font-medium" style={{ color: 'var(--brand-500)' }}>
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
                <Link to="/projects" className="text-xs mt-2 inline-block" style={{ color: 'var(--brand-500)' }}>上传第一个需求</Link>
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
