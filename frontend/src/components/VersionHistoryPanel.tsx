import { useState, useEffect } from 'react'
import { History, Loader2, ChevronRight, ChevronDown, X, FileText, RefreshCw } from 'lucide-react'
import { requirements as reqApi } from '../api/client'
import type { ReqVersionSummary, ReqVersionDetail } from '../types'

/** 需求历史版本面板：展示重新分析前的 SRR 快照 */
export default function VersionHistoryPanel({ reqId }: { reqId: string }) {
  const [versions, setVersions] = useState<ReqVersionSummary[]>([])
  const [loading, setLoading] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const [viewing, setViewing] = useState<ReqVersionDetail | null>(null)
  const [viewLoading, setViewLoading] = useState(false)
  const [error, setError] = useState('')

  const load = async () => {
    setLoading(true)
    setError('')
    try {
      const r = await reqApi.listVersions(reqId)
      setVersions(r.items || [])
    } catch (e: any) {
      setError(e.message || '加载历史版本失败')
    } finally { setLoading(false) }
  }

  useEffect(() => {
    if (expanded && versions.length === 0 && !loading) load()
  }, [expanded])

  const viewVersion = async (idx: number) => {
    setViewLoading(true)
    setError('')
    try {
      const r = await reqApi.getVersion(reqId, idx)
      setViewing(r)
    } catch (e: any) {
      setError(e.message || '加载版本详情失败')
    } finally { setViewLoading(false) }
  }

  /** 紧凑展示 SRR 关键字段 */
  const renderSRRPreview = (srr: Record<string, unknown>) => {
    if (!srr || Object.keys(srr).length === 0) {
      return <p className="text-xs text-slate-400 py-2">该版本无 SRR 数据</p>
    }
    const req = (srr.requirement as Record<string, unknown>) || {}
    const flows = (srr.flows as Record<string, unknown>) || {}
    const happyPath = (flows.happy_path as { steps?: unknown[] }) || {}
    const altFlows = (flows.alternate_flows as unknown[]) || []
    const businessRules = (srr.business_rules as unknown[]) || []
    const boundary = (srr.boundary_conditions as unknown[]) || []
    const dataConstraints = (srr.data_constraints as unknown[]) || []
    const acceptance = (srr.acceptance_criteria as unknown[]) || []
    const preconditions = (srr.preconditions as unknown[]) || []

    return (
      <div className="space-y-3 text-xs">
        <div className="bg-slate-50 rounded-lg p-3">
          <p className="font-semibold text-slate-700 mb-1">需求标题</p>
          <p className="text-slate-600">{String(req.title || '—')}</p>
          {req.summary ? <p className="text-slate-500 mt-1">{String(req.summary)}</p> : null}
          <div className="flex gap-3 mt-2 text-[10px] text-slate-400">
            <span>优先级: {String(req.priority || '—')}</span>
            <span>类型: {String(req.type || '—')}</span>
          </div>
        </div>

        {happyPath.steps && Array.isArray(happyPath.steps) && happyPath.steps.length > 0 && (
          <div>
            <p className="font-semibold text-slate-700 mb-1">主流程 ({happyPath.steps.length} 步)</p>
            <ol className="list-decimal list-inside space-y-0.5 text-slate-600">
              {happyPath.steps.map((s, i) => {
                const step = s as { action?: string; expected?: string }
                return <li key={i}>{step.action || ''} → {step.expected || ''}</li>
              })}
            </ol>
          </div>
        )}

        {altFlows.length > 0 && (
          <div>
            <p className="font-semibold text-slate-700 mb-1">异常分支 ({altFlows.length})</p>
            <ul className="list-disc list-inside text-slate-600 space-y-0.5">
              {altFlows.slice(0, 5).map((f, i) => {
                const flow = f as { name?: string; description?: string }
                return <li key={i}>{flow.name || flow.description || `分支 ${i + 1}`}</li>
              })}
              {altFlows.length > 5 && <li className="text-slate-400">... 共 {altFlows.length} 个</li>}
            </ul>
          </div>
        )}

        {businessRules.length > 0 && (
          <div>
            <p className="font-semibold text-slate-700 mb-1">业务规则 ({businessRules.length})</p>
            <ul className="list-disc list-inside text-slate-600 space-y-0.5">
              {businessRules.slice(0, 5).map((r, i) => <li key={i}>{String((r as { rule?: string; description?: string }).rule || (r as { description?: string }).description || r)}</li>)}
              {businessRules.length > 5 && <li className="text-slate-400">... 共 {businessRules.length} 条</li>}
            </ul>
          </div>
        )}

        {boundary.length > 0 && (
          <div>
            <p className="font-semibold text-slate-700 mb-1">边界条件 ({boundary.length})</p>
            <ul className="list-disc list-inside text-slate-600 space-y-0.5">
              {boundary.slice(0, 5).map((b, i) => <li key={i}>{String((b as { condition?: string; description?: string }).condition || (b as { description?: string }).description || b)}</li>)}
              {boundary.length > 5 && <li className="text-slate-400">... 共 {boundary.length} 条</li>}
            </ul>
          </div>
        )}

        {dataConstraints.length > 0 && (
          <div>
            <p className="font-semibold text-slate-700 mb-1">数据约束 ({dataConstraints.length})</p>
            <ul className="list-disc list-inside text-slate-600 space-y-0.5">
              {dataConstraints.slice(0, 5).map((d, i) => {
                const c = d as { field?: string; constraint?: string; description?: string }
                return <li key={i}>{c.field ? `${c.field}: ` : ''}{c.constraint || c.description || JSON.stringify(d)}</li>
              })}
              {dataConstraints.length > 5 && <li className="text-slate-400">... 共 {dataConstraints.length} 条</li>}
            </ul>
          </div>
        )}

        {acceptance.length > 0 && (
          <div>
            <p className="font-semibold text-slate-700 mb-1">验收标准 ({acceptance.length})</p>
            <ul className="list-disc list-inside text-slate-600 space-y-0.5">
              {acceptance.slice(0, 5).map((a, i) => <li key={i}>{String((a as { criteria?: string; description?: string }).criteria || (a as { description?: string }).description || a)}</li>)}
              {acceptance.length > 5 && <li className="text-slate-400">... 共 {acceptance.length} 条</li>}
            </ul>
          </div>
        )}

        {preconditions.length > 0 && (
          <div>
            <p className="font-semibold text-slate-700 mb-1">前置条件 ({preconditions.length})</p>
            <ul className="list-disc list-inside text-slate-600 space-y-0.5">
              {preconditions.slice(0, 5).map((p, i) => <li key={i}>{String((p as { condition?: string; description?: string }).condition || (p as { description?: string }).description || p)}</li>)}
            </ul>
          </div>
        )}
      </div>
    )
  }

  return (
    <div className="card p-4">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-between text-left"
      >
        <div className="flex items-center gap-2">
          <History className="w-4 h-4 text-violet-500" />
          <span className="text-sm font-semibold text-slate-700">历史版本管理</span>
          {versions.length > 0 && (
            <span className="text-xs px-1.5 py-0.5 bg-violet-50 text-violet-700 rounded font-mono">
              共 {versions.length} 个版本
            </span>
          )}
        </div>
        {expanded ? <ChevronDown className="w-4 h-4 text-slate-400" /> : <ChevronRight className="w-4 h-4 text-slate-400" />}
      </button>

      {expanded && (
        <div className="mt-3 space-y-2">
          <p className="text-xs text-slate-500">
            每次重新需求分析前，系统会把原有需求分析结果完整快照保存为历史版本，便于回溯对比。
          </p>

          {loading ? (
            <div className="flex items-center justify-center py-4"><Loader2 className="w-5 h-5 animate-spin text-slate-400" /></div>
          ) : error ? (
            <div className="p-3 bg-red-50 rounded-lg text-xs text-red-600 flex items-center justify-between">
              <span>{error}</span>
              <button onClick={load} className="text-red-500 hover:text-red-700"><RefreshCw className="w-3.5 h-3.5" /></button>
            </div>
          ) : versions.length === 0 ? (
            <p className="text-xs text-slate-400 py-3 text-center bg-slate-50 rounded-lg">
              暂无历史版本（首次需求分析或未进行过重新分析）
            </p>
          ) : (
            <div className="space-y-1.5">
              {versions.map(v => (
                <div key={v.version_index} className="flex items-center justify-between p-2.5 rounded-lg border border-slate-100 hover:border-violet-200 hover:bg-violet-50/30 transition-colors">
                  <div className="flex items-center gap-2.5 min-w-0 flex-1">
                    <span className="text-xs font-mono px-1.5 py-0.5 bg-violet-100 text-violet-700 rounded">v{v.version_index}</span>
                    <div className="min-w-0">
                      <p className="text-sm font-medium text-slate-700 truncate">{v.title}</p>
                      <p className="text-[10px] text-slate-400">
                        {v.snapshot_at} · 触发: {v.trigger === 'regenerate' ? '重新分析' : v.trigger}
                        {v.extra_notes ? ` · 补充: ${v.extra_notes.slice(0, 60)}${v.extra_notes.length > 60 ? '...' : ''}` : ''}
                      </p>
                    </div>
                  </div>
                  <button
                    onClick={() => viewVersion(v.version_index)}
                    className="btn-ghost p-1.5 text-slate-400 hover:text-violet-600 hover:bg-violet-50"
                    title="查看版本快照"
                  >
                    <FileText className="w-3.5 h-3.5" />
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* 历史版本详情弹窗 */}
      {viewing && (
        <div className="fixed inset-0 z-50 bg-black/30 backdrop-blur-sm flex items-center justify-center p-4" onClick={() => setViewing(null)}>
          <div className="bg-white rounded-2xl shadow-xl max-w-2xl w-full max-h-[85vh] overflow-y-auto p-6 animate-slide-up" onClick={e => e.stopPropagation()}>
            <div className="flex items-start justify-between mb-4 sticky top-0 bg-white pb-3 border-b border-slate-100">
              <div>
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-sm font-mono px-2 py-0.5 bg-violet-100 text-violet-700 rounded">v{viewing.version_index}</span>
                  <h2 className="text-lg font-semibold text-slate-800 truncate">{viewing.title}</h2>
                </div>
                <p className="text-xs text-slate-400">
                  快照时间: {viewing.snapshot_at} · 触发: {viewing.trigger === 'regenerate' ? '重新分析' : viewing.trigger}
                </p>
                {viewing.extra_notes && (
                  <p className="text-xs text-slate-500 mt-1 bg-amber-50 px-2 py-1 rounded">
                    用户补充: {viewing.extra_notes}
                  </p>
                )}
              </div>
              <button onClick={() => setViewing(null)} className="btn-ghost p-1.5"><X className="w-4 h-4" /></button>
            </div>

            {viewLoading ? (
              <div className="flex items-center justify-center py-8"><Loader2 className="w-6 h-6 animate-spin text-slate-400" /></div>
            ) : (
              renderSRRPreview(viewing.srr)
            )}
          </div>
        </div>
      )}
    </div>
  )
}
