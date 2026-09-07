import { useState, useEffect, useCallback } from 'react'
import {
  ClipboardList, Loader2, ChevronLeft, ChevronRight,
  FileText, TestTube, CheckCircle2, XCircle, AlertTriangle,
  Info, Clock, X, Layers, ThumbsUp, ThumbsDown, Undo2,
  Sparkles, History, RotateCcw,
} from 'lucide-react'
import { reviewReportsApi } from '../api/client'
import { useProduct } from '../store/ProductContext'
import type {
  ReviewReportSummary, ReviewReportDetail, DimensionScore, ReportIssue,
  EvolutionTarget, EvolutionResult, EvolutionHistoryItem,
} from '../types'
import LoadingSpinner from '../components/LoadingSpinner'

// ============================================================================
// 工具与常量
// ============================================================================

function formatDateTime(iso?: string | null): string {
  if (!iso) return ''
  try {
    const d = new Date(iso)
    const pad = (n: number) => String(n).padStart(2, '0')
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
  } catch {
    return iso.slice(0, 16).replace('T', ' ')
  }
}

/** 维度中文名（case 6 维度 + srr 3 维度） */
const DIM_LABELS: Record<string, string> = {
  completeness: '完整性',
  deduplication: '去重',
  executability: '可执行性',
  consistency: '一致性',
  traceability: '追溯性',
  coverage: '覆盖率',
  gate_compliance: '门禁合规',
  requirement_completeness: '需求完整性',
  clarity: '清晰度',
}

/** 维度分颜色：<0.6 红 / <0.8 黄 / 其余绿 */
function scoreColor(score: number): string {
  if (score < 0.6) return 'var(--state-error)'
  if (score < 0.8) return 'var(--state-warning)'
  return 'var(--state-success)'
}

const SEVERITY_STYLE: Record<string, { bg: string; fg: string; label: string }> = {
  blocker: { bg: 'var(--state-error-surface)', fg: 'var(--state-error)', label: '阻断' },
  warning: { bg: 'var(--state-warning-surface)', fg: 'var(--state-warning)', label: '警告' },
  info: { bg: 'var(--background-muted)', fg: 'var(--foreground-tertiary)', label: '提示' },
}

/** 采纳状态徽标 */
const ADOPTION_STYLE: Record<string, { bg: string; fg: string; label: string }> = {
  accepted: { bg: 'var(--state-success-surface)', fg: 'var(--state-success)', label: '已采纳' },
  rejected: { bg: 'var(--state-error-surface)', fg: 'var(--state-error)', label: '已驳回' },
  pending: { bg: 'var(--background-muted)', fg: 'var(--foreground-tertiary)', label: '待处理' },
}

const TYPE_TABS = [
  { value: '', label: '全部' },
  { value: 'srr', label: '需求评审' },
  { value: 'case', label: '用例评审' },
]

// ============================================================================
// 维度置信度条
// ============================================================================

function DimensionBars({ scores }: { scores: Record<string, DimensionScore> }) {
  const entries = Object.entries(scores || {})
  if (!entries.length) {
    return <span style={{ color: 'var(--foreground-tertiary)', fontSize: 12 }}>暂无维度数据</span>
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      {entries.map(([dim, v]) => {
        const pct = Math.round((v?.score ?? 0) * 100)
        return (
          <div key={dim} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12 }}>
            <span style={{ width: 76, flexShrink: 0, color: 'var(--foreground-secondary)' }}>
              {DIM_LABELS[dim] || dim}
            </span>
            <div style={{ flex: 1, height: 6, borderRadius: 3, background: 'var(--background-muted)', overflow: 'hidden' }}>
              <div style={{ width: `${pct}%`, height: '100%', background: scoreColor(v?.score ?? 0) }} />
            </div>
            <span style={{ width: 36, textAlign: 'right', fontVariantNumeric: 'tabular-nums', color: scoreColor(v?.score ?? 0) }}>
              {pct}%
            </span>
            {v?.issue_count > 0 && (
              <span style={{ color: 'var(--state-warning)', fontSize: 11 }} title={`${v.issue_count} 个问题`}>
                {v.issue_count}项
              </span>
            )}
          </div>
        )
      })}
    </div>
  )
}

// ============================================================================
// 详情抽屉（含 P2 采纳交互）
// ============================================================================

function DetailDrawer({ report, onClose, onAdopted }: {
  report: ReviewReportDetail
  onClose: () => void
  onAdopted?: () => void
}) {
  const issues = report.issues || []
  const summary = report.summary || {}
  const pointCoverage = summary.point_coverage
  const caseReport = report.report_type === 'case'

  // 采纳状态（本地编辑态，提交后刷新）
  const [decisions, setDecisions] = useState<Record<string, { accepted: boolean; comment: string }>>({})
  const [submitting, setSubmitting] = useState(false)
  const [toast, setToast] = useState<{ type: 'success' | 'error'; msg: string } | null>(null)

  const pendingOps = Object.entries(decisions)
  const hasChanges = pendingOps.length > 0

  const setDecision = (issueId: string, accepted: boolean | null) => {
    setDecisions((prev) => {
      const next = { ...prev }
      if (accepted === null) {
        delete next[issueId]
      } else {
        next[issueId] = { accepted, comment: prev[issueId]?.comment || '' }
      }
      return next
    })
  }

  const setComment = (issueId: string, comment: string) => {
    setDecisions((prev) => {
      const cur = prev[issueId]
      if (!cur) return prev
      return { ...prev, [issueId]: { ...cur, comment } }
    })
  }

  const submit = async () => {
    if (!hasChanges) return
    setSubmitting(true)
    try {
      const items = pendingOps.map(([issue_id, d]) => ({
        issue_id,
        accepted: d.accepted,
        comment: d.comment || undefined,
      }))
      const resp = await reviewReportsApi.adoptIssues(report.id, items)
      setToast({ type: 'success', msg: `已处理 ${resp.updated} 条意见` })
      setDecisions({})
      // 1.2s 后刷新详情（让 toast 可见）
      setTimeout(async () => {
        try {
          const d = await reviewReportsApi.get(report.id)
          // 原地更新：用回调让父组件刷新列表统计
          Object.assign(report, d)
          setToast(null)
          onAdopted?.()
        } catch { /* 静默 */ }
      }, 1200)
    } catch (e: any) {
      setToast({ type: 'error', msg: e?.message || '提交失败' })
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div
      style={{
        position: 'fixed', inset: 0, zIndex: 50, display: 'flex',
        justifyContent: 'flex-end', background: 'rgba(0,0,0,0.35)',
      }}
      onClick={onClose}
    >
      <div
        style={{
          width: 'min(720px, 92vw)', height: '100%', background: 'var(--background)',
          borderLeft: '1px solid var(--border)', display: 'flex', flexDirection: 'column',
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* 头部 */}
        <div style={{ padding: '16px 20px', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'flex-start', gap: 10 }}>
          <div style={{ flex: 1 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
              {caseReport ? <TestTube size={16} /> : <FileText size={16} />}
              <h3 style={{ margin: 0, fontSize: 15 }}>{report.req_title || report.req_id}</h3>
              <span style={{ fontSize: 12, color: 'var(--foreground-tertiary)' }}>
                {caseReport ? '用例评审报告' : '需求评审报告'}
              </span>
            </div>
            <div style={{ fontSize: 12, color: 'var(--foreground-tertiary)', display: 'flex', gap: 12, flexWrap: 'wrap' }}>
              <span>{report.id}</span>
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                <Clock size={12} />{formatDateTime(report.created_at)}
              </span>
              {report.gen_batch_id && <span>批次 {report.gen_batch_id}</span>}
              {report.task_id && <span>任务 {report.task_id}</span>}
            </div>
          </div>
          <button onClick={onClose} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--foreground-tertiary)' }}>
            <X size={18} />
          </button>
        </div>

        {/* 内容 */}
        <div style={{ flex: 1, overflowY: 'auto', padding: 20, display: 'flex', flexDirection: 'column', gap: 20 }}>
          {/* 概览 */}
          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
            <div style={{ padding: '10px 16px', borderRadius: 8, background: 'var(--background-muted)', minWidth: 100 }}>
              <div style={{ fontSize: 11, color: 'var(--foreground-tertiary)' }}>总体置信度</div>
              <div style={{ fontSize: 20, fontWeight: 600, color: scoreColor(report.overall_confidence) }}>
                {(report.overall_confidence * 100).toFixed(1)}%
              </div>
            </div>
            <div style={{ padding: '10px 16px', borderRadius: 8, background: 'var(--background-muted)', minWidth: 100 }}>
              <div style={{ fontSize: 11, color: 'var(--foreground-tertiary)' }}>评审结论</div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontWeight: 600, color: report.passed ? 'var(--state-success)' : 'var(--state-warning)' }}>
                {report.passed ? <CheckCircle2 size={16} /> : <AlertTriangle size={16} />}
                {report.passed ? '通过' : '需关注'}
              </div>
            </div>
            <div style={{ padding: '10px 16px', borderRadius: 8, background: 'var(--background-muted)', minWidth: 100 }}>
              <div style={{ fontSize: 11, color: 'var(--foreground-tertiary)' }}>问题数</div>
              <div style={{ fontSize: 20, fontWeight: 600 }}>{report.issue_count}</div>
            </div>
            {caseReport && (
              <div style={{ padding: '10px 16px', borderRadius: 8, background: 'var(--background-muted)', minWidth: 100 }}>
                <div style={{ fontSize: 11, color: 'var(--foreground-tertiary)' }}>用例数</div>
                <div style={{ fontSize: 20, fontWeight: 600 }}>{summary.total_cases ?? '-'}</div>
              </div>
            )}
            {caseReport && pointCoverage?.total_points > 0 && (
              <div style={{ padding: '10px 16px', borderRadius: 8, background: 'var(--background-muted)', minWidth: 100 }}>
                <div style={{ fontSize: 11, color: 'var(--foreground-tertiary)' }}>要点覆盖</div>
                <div style={{ fontSize: 20, fontWeight: 600 }}>
                  {pointCoverage.covered_points}/{pointCoverage.total_points}
                </div>
              </div>
            )}
          </div>

          {/* 维度置信度 */}
          <section>
            <h4 style={{ margin: '0 0 10px', fontSize: 13, display: 'flex', alignItems: 'center', gap: 6 }}>
              <Layers size={14} />各维度置信度
            </h4>
            <DimensionBars scores={report.dimension_scores || {}} />
          </section>

          {/* 问题列表（含采纳操作） */}
          <section>
            <h4 style={{ margin: '0 0 10px', fontSize: 13, display: 'flex', alignItems: 'center', gap: 8 }}>
              评审意见（{issues.length}）
              {issues.some((i: ReportIssue) => i.adoption_status === 'accepted') && (
                <span style={{ fontSize: 11, padding: '1px 8px', borderRadius: 10, background: 'var(--state-success-surface)', color: 'var(--state-success)' }}>
                  已采纳 {issues.filter((i: ReportIssue) => i.adoption_status === 'accepted').length}
                </span>
              )}
              {issues.some((i: ReportIssue) => i.adoption_status === 'rejected') && (
                <span style={{ fontSize: 11, padding: '1px 8px', borderRadius: 10, background: 'var(--state-error-surface)', color: 'var(--state-error)' }}>
                  已驳回 {issues.filter((i: ReportIssue) => i.adoption_status === 'rejected').length}
                </span>
              )}
            </h4>
            {issues.length === 0 ? (
              <div style={{ padding: 16, borderRadius: 8, background: 'var(--background-muted)', color: 'var(--foreground-tertiary)', fontSize: 13 }}>
                无评审意见
              </div>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                {issues.map((iss: ReportIssue) => {
                  const st = SEVERITY_STYLE[iss.severity] || SEVERITY_STYLE.info
                  const ad = ADOPTION_STYLE[iss.adoption_status || 'pending'] || ADOPTION_STYLE.pending
                  const local = decisions[iss.issue_id]
                  // 显示状态：本地编辑优先，其次已提交状态
                  const showAccepted = local ? local.accepted : iss.adoption_status === 'accepted'
                  const showRejected = local ? !local.accepted : iss.adoption_status === 'rejected'
                  const showPending = !local && (iss.adoption_status || 'pending') === 'pending'
                  return (
                    <div
                      key={iss.issue_id}
                      style={{
                        padding: '10px 12px', borderRadius: 8,
                        border: `1px solid ${local ? 'var(--brand-500)' : 'var(--border)'}`,
                      }}
                    >
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4, flexWrap: 'wrap' }}>
                        <span style={{ padding: '1px 8px', borderRadius: 10, fontSize: 11, background: st.bg, color: st.fg }}>
                          {st.label}
                        </span>
                        <span style={{ fontSize: 11, color: 'var(--foreground-tertiary)' }}>
                          {DIM_LABELS[iss.dimension] || iss.dimension}
                        </span>
                        {iss.case_id && (
                          <span style={{ fontSize: 11, color: 'var(--foreground-tertiary)' }}>{iss.case_id}</span>
                        )}
                        <span style={{ flex: 1 }} />
                        {showPending && (
                          <span style={{ fontSize: 11, padding: '1px 8px', borderRadius: 10, background: ad.bg, color: ad.fg }}>
                            {ad.label}
                          </span>
                        )}
                        {showAccepted && (
                          <span style={{ fontSize: 11, padding: '1px 8px', borderRadius: 10, background: 'var(--state-success-surface)', color: 'var(--state-success)', display: 'inline-flex', alignItems: 'center', gap: 3 }}>
                            <ThumbsUp size={10} />{local ? '将采纳' : ad.label}
                          </span>
                        )}
                        {showRejected && (
                          <span style={{ fontSize: 11, padding: '1px 8px', borderRadius: 10, background: 'var(--state-error-surface)', color: 'var(--state-error)', display: 'inline-flex', alignItems: 'center', gap: 3 }}>
                            <ThumbsDown size={10} />{local ? '将驳回' : ad.label}
                          </span>
                        )}
                      </div>
                      <div style={{ fontSize: 13, marginBottom: iss.suggestion ? 4 : 0 }}>{iss.message}</div>
                      {iss.suggestion && (
                        <div style={{ fontSize: 12, color: 'var(--foreground-tertiary)', marginBottom: 6 }}>建议：{iss.suggestion}</div>
                      )}
                      {/* 已提交的人工意见 */}
                      {iss.adoption_comment && !local && (
                        <div style={{ fontSize: 12, padding: '6px 8px', borderRadius: 6, background: 'var(--background-muted)', marginBottom: 6 }}>
                          人工意见：{iss.adoption_comment}
                          {iss.adopted_by && <span style={{ color: 'var(--foreground-tertiary)' }}>（{iss.adopted_by}）</span>}
                        </div>
                      )}
                      {/* 操作按钮 */}
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 4 }}>
                        <button
                          onClick={() => setDecision(iss.issue_id, true)}
                          style={{
                            display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 12,
                            padding: '3px 10px', borderRadius: 6, cursor: 'pointer',
                            border: `1px solid ${showAccepted ? 'var(--state-success)' : 'var(--border)'}`,
                            background: showAccepted ? 'var(--state-success-surface)' : 'var(--background)',
                            color: showAccepted ? 'var(--state-success)' : 'var(--foreground-secondary)',
                          }}
                        >
                          <ThumbsUp size={12} />采纳
                        </button>
                        <button
                          onClick={() => setDecision(iss.issue_id, false)}
                          style={{
                            display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 12,
                            padding: '3px 10px', borderRadius: 6, cursor: 'pointer',
                            border: `1px solid ${showRejected ? 'var(--state-error)' : 'var(--border)'}`,
                            background: showRejected ? 'var(--state-error-surface)' : 'var(--background)',
                            color: showRejected ? 'var(--state-error)' : 'var(--foreground-secondary)',
                          }}
                        >
                          <ThumbsDown size={12} />驳回
                        </button>
                        {local && (
                          <button
                            onClick={() => setDecision(iss.issue_id, null)}
                            title="撤销本次操作"
                            style={{
                              display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 12,
                              padding: '3px 10px', borderRadius: 6, cursor: 'pointer',
                              border: '1px solid var(--border)', background: 'var(--background)',
                              color: 'var(--foreground-tertiary)',
                            }}
                          >
                            <Undo2 size={12} />撤销
                          </button>
                        )}
                      </div>
                      {/* 人工意见输入（仅已做出本地决定时显示） */}
                      {local && (
                        <input
                          value={local.comment}
                          onChange={(e) => setComment(iss.issue_id, e.target.value)}
                          placeholder="补充意见（可选：采纳理由 / 驳回原因 / 改进建议）"
                          style={{
                            width: '100%', marginTop: 8, padding: '6px 10px', fontSize: 12,
                            borderRadius: 6, border: '1px solid var(--border)',
                            background: 'var(--background)', color: 'var(--foreground)',
                            boxSizing: 'border-box',
                          }}
                        />
                      )}
                    </div>
                  )
                })}
              </div>
            )}
          </section>
        </div>

        {/* 底部提交栏 */}
        {issues.length > 0 && (
          <div style={{
            padding: '12px 20px', borderTop: '1px solid var(--border)',
            display: 'flex', alignItems: 'center', gap: 12,
          }}>
            {toast && (
              <span style={{ fontSize: 13, color: toast.type === 'success' ? 'var(--state-success)' : 'var(--state-error)' }}>
                {toast.msg}
              </span>
            )}
            {!toast && (
              <span style={{ fontSize: 12, color: 'var(--foreground-tertiary)' }}>
                {hasChanges ? `已选择 ${pendingOps.length} 条意见待提交` : '选择意见后采纳/驳回，可补充人工意见'}
              </span>
            )}
            <span style={{ flex: 1 }} />
            <button
              onClick={submit}
              disabled={!hasChanges || submitting}
              style={{
                padding: '7px 20px', borderRadius: 8, fontSize: 13, fontWeight: 500, cursor: hasChanges && !submitting ? 'pointer' : 'default',
                background: hasChanges && !submitting ? 'var(--brand-500)' : 'var(--background-muted)',
                color: hasChanges && !submitting ? '#ffffff' : 'var(--foreground-tertiary)',
                border: 'none',
                display: 'inline-flex', alignItems: 'center', gap: 6,
              }}
            >
              {submitting && <Loader2 size={13} className="animate-spin" />}
              提交采纳（{pendingOps.length}）
            </button>
          </div>
        )}
      </div>
    </div>
  )
}

// ============================================================================
// P3 整合抽屉：多报告已采纳意见 + 人工意见 → LLM 整合 → 提示词新版本
// ============================================================================

function EvolutionDrawer({ onClose }: { onClose: () => void }) {
  const { selectedProductId } = useProduct()

  const [tab, setTab] = useState<'run' | 'history'>('run')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  // 发起整合
  const [reports, setReports] = useState<ReviewReportSummary[]>([])
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [targets, setTargets] = useState<EvolutionTarget[]>([])
  const [targetId, setTargetId] = useState('gen_agent.SYSTEM_PROMPT')
  const [manualNotes, setManualNotes] = useState('')
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState<EvolutionResult | null>(null)

  // 整合历史
  const [history, setHistory] = useState<EvolutionHistoryItem[]>([])

  const loadAll = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const [rep, tg, hist] = await Promise.all([
        reviewReportsApi.list({ product_id: selectedProductId || undefined, page: 1, page_size: 100 }),
        reviewReportsApi.evolutionTargets(),
        reviewReportsApi.evolutionHistory({ product_id: selectedProductId || undefined, limit: 50 }),
      ])
      setReports(rep.items || [])
      setTargets(tg.items || [])
      setHistory(hist.items || [])
    } catch (e: any) {
      setError(e?.message || '加载失败')
    } finally {
      setLoading(false)
    }
  }, [selectedProductId])

  useEffect(() => { loadAll() }, [loadAll])

  // 只展示有已采纳意见的报告
  const consumable = reports.filter((r) => (r.accepted_count || 0) > 0)
  const selectedList = consumable.filter((r) => selected.has(r.id))
  const totalAccepted = selectedList.reduce((s, r) => s + (r.accepted_count || 0), 0)
  const selectedTypes = new Set(selectedList.map((r) => r.report_type))
  // 目标下拉：按选中报告类型匹配推荐（未匹配全部显示）
  const visibleTargets = selectedTypes.size
    ? targets.filter((t) => [...selectedTypes].some((rt) => t.report_types.includes(rt)))
    : targets

  const toggle = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const run = async () => {
    if (!selected.size || !selectedProductId) return
    setRunning(true)
    setError('')
    setResult(null)
    try {
      const r = await reviewReportsApi.runEvolution({
        product_id: selectedProductId,
        report_ids: [...selected],
        target_prompt_id: targetId,
        manual_notes: manualNotes || undefined,
      })
      setResult(r)
      // 刷新历史
      try {
        const hist = await reviewReportsApi.evolutionHistory({ product_id: selectedProductId, limit: 50 })
        setHistory(hist.items || [])
      } catch { /* 静默 */ }
    } catch (e: any) {
      setError(e?.message || '整合失败')
    } finally {
      setRunning(false)
    }
  }

  const currentTarget = targets.find((t) => t.prompt_id === targetId)

  return (
    <div
      style={{
        position: 'fixed', inset: 0, zIndex: 50, display: 'flex',
        justifyContent: 'flex-end', background: 'rgba(0,0,0,0.35)',
      }}
      onClick={onClose}
    >
      <div
        style={{
          width: 'min(680px, 92vw)', height: '100%', background: 'var(--background)',
          borderLeft: '1px solid var(--border)', display: 'flex', flexDirection: 'column',
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* 头部 */}
        <div style={{ padding: '16px 20px', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', gap: 10 }}>
          <Sparkles size={17} style={{ color: 'var(--brand-500)' }} />
          <div style={{ flex: 1 }}>
            <h3 style={{ margin: 0, fontSize: 15 }}>提示词进化</h3>
            <div style={{ fontSize: 12, color: 'var(--foreground-tertiary)' }}>
              多报告已采纳意见 + 人工意见 → LLM 整合 → 生成提示词新版本（支持回退）
            </div>
          </div>
          <button onClick={onClose} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--foreground-tertiary)' }}>
            <X size={18} />
          </button>
        </div>

        {/* Tab */}
        <div style={{ padding: '12px 20px 0', display: 'flex', gap: 8 }}>
          {([['run', '发起整合'], ['history', '整合历史']] as const).map(([v, label]) => (
            <button
              key={v}
              onClick={() => setTab(v)}
              style={{
                padding: '6px 14px', borderRadius: 8, fontSize: 13, cursor: 'pointer',
                display: 'inline-flex', alignItems: 'center', gap: 5,
                border: '1px solid ' + (tab === v ? 'var(--brand-500)' : 'var(--border)'),
                background: tab === v ? 'var(--brand-500)' : 'var(--background)',
                color: tab === v ? '#ffffff' : 'var(--foreground)',
              }}
            >
              {v === 'run' ? <Sparkles size={13} /> : <History size={13} />}{label}
            </button>
          ))}
        </div>

        {/* 内容 */}
        <div style={{ flex: 1, overflowY: 'auto', padding: 16 }}>
          {loading && <LoadingSpinner text="加载中..." />}
          {error && (
            <div style={{ padding: 12, borderRadius: 8, background: 'var(--state-error-surface)', color: 'var(--state-error)', marginBottom: 12, fontSize: 13 }}>
              {error}
            </div>
          )}

          {!loading && tab === 'run' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
              {/* 整合结果 */}
              {result && (
                <div style={{ padding: 14, borderRadius: 10, border: '1px solid var(--state-success)', background: 'var(--state-success-surface)' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8, color: 'var(--state-success)', fontWeight: 600, fontSize: 13 }}>
                    <CheckCircle2 size={15} />整合完成
                  </div>
                  <div style={{ fontSize: 13, marginBottom: 4 }}>
                    {result.report_count} 份报告的 <b>{result.accepted_count}</b> 条已采纳意见已沉淀至
                    <b> {result.target_prompt_id}</b>
                  </div>
                  {result.integration_summary && (
                    <div style={{ fontSize: 12, color: 'var(--foreground-secondary)', marginBottom: 8 }}>
                      {result.integration_summary}
                    </div>
                  )}
                  <div style={{ fontSize: 12, color: 'var(--foreground-tertiary)', marginBottom: 8, whiteSpace: 'pre-wrap', maxHeight: 200, overflowY: 'auto', padding: '8px 10px', borderRadius: 6, background: 'var(--background)' }}>
                    {result.new_section}
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--foreground-tertiary)' }}>
                    <RotateCcw size={12} />
                    如有问题可回退：提示词版本 #{result.prompt_history_id}（管理后台 → 提示词管理 → 历史）
                  </div>
                </div>
              )}

              {/* 1. 报告多选 */}
              <section>
                <h4 style={{ margin: '0 0 8px', fontSize: 13 }}>
                  ① 选择报告（{consumable.length} 份含已采纳意见）
                    {selected.size > 0 && (
                    <span style={{ marginLeft: 8, fontSize: 12, color: 'var(--state-success)' }}>
                      已选 {selected.size} 份 / 共 {totalAccepted} 条意见
                    </span>
                  )}
                </h4>
                {consumable.length === 0 ? (
                  <div style={{ padding: 14, borderRadius: 8, background: 'var(--background-muted)', color: 'var(--foreground-tertiary)', fontSize: 13 }}>
                    当前产品暂无含已采纳意见的报告。请先在报告详情中采纳评审意见。
                  </div>
                ) : (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 260, overflowY: 'auto' }}>
                    {consumable.map((r) => {
                      const checked = selected.has(r.id)
                      return (
                        <label
                          key={r.id}
                          style={{
                            display: 'flex', alignItems: 'center', gap: 10, padding: '8px 12px',
                            borderRadius: 8, border: `1px solid ${checked ? 'var(--brand-500)' : 'var(--border)'}`,
                            cursor: 'pointer', fontSize: 13, background: checked ? 'var(--background-muted)' : 'var(--background)',
                          }}
                        >
                          <input type="checkbox" checked={checked} onChange={() => toggle(r.id)} style={{ accentColor: 'var(--brand-500)' }} />
                          {r.report_type === 'case' ? <TestTube size={13} /> : <FileText size={13} />}
                          <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                            {r.req_title || r.req_id}
                          </span>
                          <span style={{ fontSize: 11, color: 'var(--foreground-tertiary)' }}>
                            {r.report_type === 'case' ? '用例' : '需求'}评审 · {formatDateTime(r.created_at)}
                          </span>
                          <span style={{ fontSize: 11, padding: '1px 8px', borderRadius: 10, background: 'var(--state-success-surface)', color: 'var(--state-success)', flexShrink: 0 }}>
                            已采纳 {r.accepted_count}
                          </span>
                        </label>
                      )
                    })}
                  </div>
                )}
              </section>

              {/* 2. 目标提示词 */}
              <section>
                <h4 style={{ margin: '0 0 8px', fontSize: 13 }}>② 目标提示词</h4>
                <select
                  value={targetId}
                  onChange={(e) => setTargetId(e.target.value)}
                  style={{
                    width: '100%', padding: '8px 12px', fontSize: 13, borderRadius: 8,
                    border: '1px solid var(--border)', background: 'var(--background)', color: 'var(--foreground)',
                    boxSizing: 'border-box',
                  }}
                >
                  {(visibleTargets.length ? visibleTargets : targets).map((t) => (
                    <option key={t.prompt_id} value={t.prompt_id}>
                      {t.label}（{t.prompt_id}）
                    </option>
                  ))}
                </select>
                {currentTarget && (
                  <div style={{ fontSize: 12, color: 'var(--foreground-tertiary)', marginTop: 6 }}>
                    {currentTarget.description}（历史版本 {currentTarget.history_count} 个）
                  </div>
                )}
              </section>

              {/* 3. 人工补充意见 */}
              <section>
                <h4 style={{ margin: '0 0 8px', fontSize: 13 }}>③ 人工补充意见（最高优先级，可选）</h4>
                <textarea
                  value={manualNotes}
                  onChange={(e) => setManualNotes(e.target.value)}
                  placeholder="例如：预期结果必须描述可观测的业务状态（UI 提示 / API 响应），禁止使用'系统内部处理'类表述"
                  rows={3}
                  style={{
                    width: '100%', padding: '8px 12px', fontSize: 13, borderRadius: 8,
                    border: '1px solid var(--border)', background: 'var(--background)', color: 'var(--foreground)',
                    boxSizing: 'border-box', resize: 'vertical',
                  }}
                />
              </section>

              <div style={{ fontSize: 12, color: 'var(--foreground-tertiary)' }}>
                整合方式：LLM 将选中意见与既有沉淀规则合并去重，追加到目标提示词末尾的
                "历史评审沉淀规则"区段；提示词主体不变，每次整合自动记录版本，可在管理后台一键回退。
              </div>
            </div>
          )}

          {!loading && tab === 'history' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              {history.length === 0 ? (
                <div style={{ padding: 24, textAlign: 'center', borderRadius: 10, background: 'var(--background-muted)', color: 'var(--foreground-tertiary)', fontSize: 13 }}>
                  暂无整合记录
                </div>
              ) : history.map((h) => (
                <div key={h.id} style={{ padding: '10px 12px', borderRadius: 8, border: '1px solid var(--border)' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4, flexWrap: 'wrap', fontSize: 12 }}>
                    <span style={{ padding: '1px 8px', borderRadius: 10, background: 'var(--background-muted)', color: 'var(--foreground-tertiary)' }}>
                      {h.target_prompt_id}
                    </span>
                    <span style={{ color: 'var(--state-success)' }}>{h.accepted_count} 条意见</span>
                    <span style={{ color: 'var(--foreground-tertiary)' }}>{h.report_ids.length} 份报告</span>
                    <span style={{ flex: 1 }} />
                    <span style={{ color: 'var(--foreground-tertiary)' }}>{formatDateTime(h.created_at)}</span>
                    <span style={{ color: 'var(--foreground-tertiary)' }}>{h.created_by}</span>
                  </div>
                  <div style={{ fontSize: 13 }}>{h.integration_summary || '（无摘要）'}</div>
                  {h.manual_notes && (
                    <div style={{ fontSize: 12, color: 'var(--foreground-tertiary)', marginTop: 4 }}>
                      人工意见：{h.manual_notes}
                    </div>
                  )}
                  <div style={{ fontSize: 11, color: 'var(--foreground-tertiary)', marginTop: 4 }}>
                    版本锚点 #{h.prompt_history_id}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* 底部执行栏 */}
        {tab === 'run' && (
          <div style={{
            padding: '12px 20px', borderTop: '1px solid var(--border)',
            display: 'flex', alignItems: 'center', gap: 12,
          }}>
            <span style={{ fontSize: 12, color: 'var(--foreground-tertiary)' }}>
              {running ? 'LLM 整合中，请稍候（约 10-60 秒）...' :
                selected.size === 0 ? '请先选择报告' : `将整合 ${totalAccepted} 条已采纳意见`}
            </span>
            <span style={{ flex: 1 }} />
            <button
              onClick={run}
              disabled={selected.size === 0 || running || !selectedProductId}
              style={{
                padding: '8px 22px', borderRadius: 8, fontSize: 13, fontWeight: 500,
                cursor: selected.size > 0 && !running ? 'pointer' : 'default',
                background: selected.size > 0 && !running ? 'var(--brand-500)' : 'var(--background-muted)',
                color: selected.size > 0 && !running ? '#ffffff' : 'var(--foreground-tertiary)',
                border: 'none', display: 'inline-flex', alignItems: 'center', gap: 6,
              }}
            >
              {running ? <Loader2 size={14} className="animate-spin" /> : <Sparkles size={14} />}
              {running ? '整合中...' : '执行整合'}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}

// ============================================================================
// 主组件
// ============================================================================

export default function ReviewReports() {
  const { selectedProductId } = useProduct()

  const [typeFilter, setTypeFilter] = useState('')
  const [page, setPage] = useState(1)
  const pageSize = 20

  const [reports, setReports] = useState<ReviewReportSummary[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const [detail, setDetail] = useState<ReviewReportDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [evolutionOpen, setEvolutionOpen] = useState(false)

  const totalPages = Math.max(1, Math.ceil(total / pageSize))

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const resp = await reviewReportsApi.list({
        product_id: selectedProductId || undefined,
        report_type: typeFilter || undefined,
        page,
        page_size: pageSize,
      })
      setReports(resp.items || [])
      setTotal(resp.total || 0)
    } catch (e: any) {
      if (!e?._aborted) {
        setError(e?.message || '加载失败')
        setReports([])
        setTotal(0)
      }
    } finally {
      setLoading(false)
    }
  }, [selectedProductId, typeFilter, page])

  useEffect(() => { load() }, [load])
  useEffect(() => { setPage(1) }, [typeFilter, selectedProductId])

  const openDetail = async (id: string) => {
    setDetailLoading(true)
    try {
      const d = await reviewReportsApi.get(id)
      setDetail(d)
    } catch (e: any) {
      setError(e?.message || '加载报告详情失败')
    } finally {
      setDetailLoading(false)
    }
  }

  return (
    <div style={{ padding: 24, maxWidth: 1200, margin: '0 auto' }}>
      {/* 标题 */}
      <div style={{ marginBottom: 20, display: 'flex', alignItems: 'center', gap: 10 }}>
        <ClipboardList size={22} />
        <div style={{ flex: 1 }}>
          <h2 style={{ margin: 0, fontSize: 18 }}>评审报告</h2>
          <p style={{ margin: 0, fontSize: 12, color: 'var(--foreground-tertiary)' }}>
            需求评审与用例评审报告，按维度给出置信度参考，辅助人工审核
          </p>
        </div>
        <button
          onClick={() => setEvolutionOpen(true)}
          title="将已采纳的评审意见整合为提示词新版本"
          style={{
            display: 'inline-flex', alignItems: 'center', gap: 6, padding: '8px 16px',
            borderRadius: 8, fontSize: 13, fontWeight: 500, cursor: 'pointer',
            border: '1px solid var(--brand-500)', background: 'var(--background)',
            color: 'var(--brand-500)',
          }}
        >
          <Sparkles size={14} />提示词进化
        </button>
      </div>

      {/* 类型筛选 */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 16 }}>
        {TYPE_TABS.map((t) => (
          <button
            key={t.value}
            onClick={() => setTypeFilter(t.value)}
            style={{
              padding: '6px 14px', borderRadius: 8, fontSize: 13, cursor: 'pointer',
              border: '1px solid ' + (typeFilter === t.value ? 'var(--brand-500)' : 'var(--border)'),
              background: typeFilter === t.value ? 'var(--brand-500)' : 'var(--background)',
              color: typeFilter === t.value ? '#ffffff' : 'var(--foreground)',
            }}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* 错误提示 */}
      {error && (
        <div style={{ padding: 12, borderRadius: 8, background: 'var(--state-error-surface)', color: 'var(--state-error)', marginBottom: 16, fontSize: 13 }}>
          {error}
        </div>
      )}

      {/* 加载中 */}
      {loading && <LoadingSpinner text="加载评审报告..." />}

      {/* 空态 */}
      {!loading && reports.length === 0 && !error && (
        <div style={{ padding: 48, textAlign: 'center', borderRadius: 12, background: 'var(--background-muted)', color: 'var(--foreground-tertiary)' }}>
          <ClipboardList size={36} style={{ marginBottom: 12, opacity: 0.5 }} />
          <div style={{ fontSize: 14 }}>暂无评审报告</div>
          <div style={{ fontSize: 12, marginTop: 4 }}>需求分析或用例生成完成后，评审报告将自动归档到这里</div>
        </div>
      )}

      {/* 报告列表 */}
      {!loading && reports.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {reports.map((r) => (
            <div
              key={r.id}
              onClick={() => openDetail(r.id)}
              style={{
                padding: '14px 16px', borderRadius: 10, border: '1px solid var(--border)',
                cursor: 'pointer', background: 'var(--background)',
              }}
              onMouseEnter={(e) => (e.currentTarget.style.borderColor = 'var(--brand-500)')}
              onMouseLeave={(e) => (e.currentTarget.style.borderColor = 'var(--border)')}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
                {r.report_type === 'case' ? <TestTube size={15} /> : <FileText size={15} />}
                <span style={{ fontWeight: 500, fontSize: 14, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {r.req_title || r.req_id}
                </span>
                <span style={{ fontSize: 11, padding: '1px 8px', borderRadius: 10, background: 'var(--background-muted)', color: 'var(--foreground-tertiary)' }}>
                  {r.report_type === 'case' ? '用例评审' : '需求评审'}
                </span>
                <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 12 }}>
                  {r.passed
                    ? <CheckCircle2 size={14} style={{ color: 'var(--state-success)' }} />
                    : <XCircle size={14} style={{ color: 'var(--state-warning)' }} />}
                  <span style={{ color: scoreColor(r.overall_confidence), fontWeight: 600, fontVariantNumeric: 'tabular-nums' }}>
                    {(r.overall_confidence * 100).toFixed(0)}%
                  </span>
                </span>
                {r.issue_count > 0 && (
                  <span style={{ fontSize: 11, padding: '1px 8px', borderRadius: 10, background: 'var(--state-warning-surface)', color: 'var(--state-warning)' }}>
                    {r.issue_count} 个问题
                  </span>
                )}
                <span style={{ fontSize: 11, color: 'var(--foreground-tertiary)', whiteSpace: 'nowrap' }}>
                  {formatDateTime(r.created_at)}
                </span>
              </div>
              <DimensionBars scores={r.dimension_scores || {}} />
            </div>
          ))}
        </div>
      )}

      {/* 分页 */}
      {!loading && total > pageSize && (
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 12, marginTop: 20 }}>
          <button
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page <= 1}
            style={{ background: 'none', border: '1px solid var(--border)', borderRadius: 6, padding: '4px 8px', cursor: page <= 1 ? 'default' : 'pointer', color: page <= 1 ? 'var(--foreground-tertiary)' : 'var(--foreground)' }}
          >
            <ChevronLeft size={16} />
          </button>
          <span style={{ fontSize: 13, color: 'var(--foreground-secondary)' }}>{page} / {totalPages}</span>
          <button
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            disabled={page >= totalPages}
            style={{ background: 'none', border: '1px solid var(--border)', borderRadius: 6, padding: '4px 8px', cursor: page >= totalPages ? 'default' : 'pointer', color: page >= totalPages ? 'var(--foreground-tertiary)' : 'var(--foreground)' }}
          >
            <ChevronRight size={16} />
          </button>
        </div>
      )}

      {/* 详情加载中 */}
      {detailLoading && (
        <div style={{ position: 'fixed', top: 20, right: 20, zIndex: 60, background: 'var(--background)', border: '1px solid var(--border)', borderRadius: 8, padding: '8px 14px', display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
          <Loader2 size={14} className="animate-spin" />加载报告详情...
        </div>
      )}

      {/* 详情抽屉（采纳提交后刷新列表统计） */}
      {detail && (
        <DetailDrawer
          report={detail}
          onClose={() => setDetail(null)}
          onAdopted={load}
        />
      )}

      {/* P3 整合抽屉 */}
      {evolutionOpen && <EvolutionDrawer onClose={() => setEvolutionOpen(false)} />}
    </div>
  )
}
