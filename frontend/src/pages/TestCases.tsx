import { useState, useEffect } from 'react'
import { TestTube, Search, Filter, CheckCircle2, XCircle, ThumbsUp, ThumbsDown, Loader2, Trash2, Eye } from 'lucide-react'
import { testCases as tcApi, requirements as reqApi } from '../api/client'
import type { TestCase, Requirement } from '../types'
import { formatDate, priorityBadge, statusBadge, confidenceColor, CASE_STATUS_LABELS } from '../utils'

export default function TestCases() {
  const [cases, setCases] = useState<TestCase[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(true)
  const [filterReq, setFilterReq] = useState('')
  const [reqs, setReqs] = useState<Requirement[]>([])
  const [selectedCase, setSelectedCase] = useState<TestCase | null>(null)
  const [feedbackLoading, setFeedbackLoading] = useState<string | null>(null)

  useEffect(() => {
    reqApi.list(1, 100).then(r => setReqs(r.items || [])).catch(() => {})
  }, [])

  const loadCases = () => {
    setLoading(true)
    tcApi.list(filterReq || undefined, page).then(r => {
      setCases(r.items || [])
      setTotal(r.total || 0)
    }).catch(() => {}).finally(() => setLoading(false))
  }

  useEffect(() => { loadCases() }, [page, filterReq])

  const handleFeedback = async (caseId: string, accepted: boolean) => {
    setFeedbackLoading(caseId)
    try {
      await tcApi.feedback(caseId, accepted)
      loadCases()
    } catch (e: any) {
      alert('操作失败: ' + e.message)
    } finally {
      setFeedbackLoading(null)
    }
  }

  const handleDelete = async (caseId: string) => {
    if (!confirm('确认删除此用例？')) return
    try {
      await tcApi.delete(caseId)
      loadCases()
    } catch {}
  }

  const totalPages = Math.ceil(total / 20)

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="page-title">测试用例</h1>
          <p className="text-sm text-slate-500 mt-1">{total} 个用例</p>
        </div>
      </div>

      {/* Filters */}
      <div className="card py-3 px-5">
        <div className="flex items-center gap-3">
          <Filter className="w-4 h-4 text-slate-400" />
          <select value={filterReq} onChange={e => { setFilterReq(e.target.value); setPage(1) }}
            className="select-field max-w-xs text-sm">
            <option value="">全部需求</option>
            {reqs.map(r => <option key={r.id} value={r.id}>{r.id} — {r.title}</option>)}
          </select>
        </div>
      </div>

      {/* Table */}
      <div className="card p-0 overflow-hidden">
        {loading ? (
          <div className="p-6 space-y-4">{[1,2,3,4,5].map(i => <div key={i} className="skeleton h-14" />)}</div>
        ) : cases.length === 0 ? (
          <div className="text-center py-16">
            <TestTube className="w-12 h-12 mx-auto mb-3 text-slate-300" />
            <p className="text-slate-500 font-medium">暂无用例</p>
            <p className="text-sm text-slate-400 mt-1">选择需求并生成测试用例</p>
          </div>
        ) : (
          <div>
            <table className="w-full">
              <thead>
                <tr className="border-b border-slate-100 bg-slate-50/50">
                  <th className="text-left text-xs font-semibold text-slate-500 uppercase tracking-wider px-5 py-3.5">用例 ID</th>
                  <th className="text-left text-xs font-semibold text-slate-500 uppercase tracking-wider px-5 py-3.5">标题</th>
                  <th className="text-center text-xs font-semibold text-slate-500 uppercase tracking-wider px-5 py-3.5">类型</th>
                  <th className="text-center text-xs font-semibold text-slate-500 uppercase tracking-wider px-5 py-3.5">优先级</th>
                  <th className="text-center text-xs font-semibold text-slate-500 uppercase tracking-wider px-5 py-3.5">状态</th>
                  <th className="text-center text-xs font-semibold text-slate-500 uppercase tracking-wider px-5 py-3.5">置信度</th>
                  <th className="text-right text-xs font-semibold text-slate-500 uppercase tracking-wider px-5 py-3.5">创建时间</th>
                  <th className="text-right px-5 py-3.5 w-24"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-50">
                {cases.map((c) => (
                  <tr key={c.id} className="hover:bg-slate-50/50 transition-colors">
                    <td className="px-5 py-3">
                      <span className="text-xs font-mono text-slate-400">{c.id}</span>
                    </td>
                    <td className="px-5 py-3">
                      <p className="text-sm font-medium text-slate-700 truncate max-w-[250px]">{c.title}</p>
                    </td>
                    <td className="px-5 py-3 text-center">
                      <span className="badge bg-blue-50 text-blue-600">{c.type}</span>
                    </td>
                    <td className="px-5 py-3 text-center">
                      <span className={priorityBadge(c.priority)}>{c.priority}</span>
                    </td>
                    <td className="px-5 py-3 text-center">
                      <span className={statusBadge(c.status)}>{CASE_STATUS_LABELS[c.status] || c.status}</span>
                    </td>
                    <td className="px-5 py-3 text-center">
                      <span className={`text-xs font-mono font-medium ${confidenceColor(c.confidence)}`}>
                        {(c.confidence * 100).toFixed(0)}%
                      </span>
                    </td>
                    <td className="px-5 py-3 text-right">
                      <span className="text-xs text-slate-400">{formatDate(c.created_at)}</span>
                    </td>
                    <td className="px-5 py-3 text-right">
                      <div className="flex items-center justify-end gap-0.5">
                        <button onClick={() => setSelectedCase(c)} className="btn-ghost p-1.5" title="查看">
                          <Eye className="w-3.5 h-3.5" />
                        </button>
                        {c.status !== 'approved' && (
                          <>
                            <button onClick={() => handleFeedback(c.id, true)} disabled={feedbackLoading === c.id}
                              className="btn-ghost p-1.5 text-success-500 hover:text-success-600" title="采纳">
                              {feedbackLoading === c.id ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <ThumbsUp className="w-3.5 h-3.5" />}
                            </button>
                            <button onClick={() => handleFeedback(c.id, false)} disabled={feedbackLoading === c.id}
                              className="btn-ghost p-1.5 text-red-400 hover:text-red-500" title="驳回">
                              <ThumbsDown className="w-3.5 h-3.5" />
                            </button>
                          </>
                        )}
                        <button onClick={() => handleDelete(c.id)} className="btn-ghost p-1.5 text-slate-400 hover:text-red-500" title="删除">
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {/* Pagination */}
            {totalPages > 1 && (
              <div className="flex items-center justify-between px-5 py-3 border-t border-slate-100 bg-slate-50/30">
                <span className="text-xs text-slate-400">共 {total} 条</span>
                <div className="flex items-center gap-1">
                  <button disabled={page <= 1} onClick={() => setPage(page - 1)} className="btn-ghost text-xs py-1 disabled:opacity-30">上一页</button>
                  <span className="text-xs text-slate-500 px-2">{page}/{totalPages}</span>
                  <button disabled={page >= totalPages} onClick={() => setPage(page + 1)} className="btn-ghost text-xs py-1 disabled:opacity-30">下一页</button>
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Detail Modal */}
      {selectedCase && (
        <div className="fixed inset-0 z-50 bg-black/30 backdrop-blur-sm flex items-center justify-center p-4" onClick={() => setSelectedCase(null)}>
          <div className="bg-white rounded-2xl shadow-xl max-w-2xl w-full p-6 animate-slide-up max-h-[90vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between mb-4">
              <div>
                <h2 className="text-lg font-semibold text-slate-800">{selectedCase.title}</h2>
                <p className="text-xs font-mono text-slate-400 mt-0.5">{selectedCase.id}</p>
              </div>
              <div className="flex items-center gap-2">
                <span className={priorityBadge(selectedCase.priority)}>{selectedCase.priority}</span>
                <span className={statusBadge(selectedCase.status)}>{CASE_STATUS_LABELS[selectedCase.status] || selectedCase.status}</span>
              </div>
            </div>
            <div className="grid grid-cols-4 gap-3 mb-4">
              <div className="text-center p-2 bg-slate-50 rounded-lg">
                <p className="text-xs text-slate-400">类型</p>
                <p className="text-sm font-medium text-slate-700">{selectedCase.type}</p>
              </div>
              <div className="text-center p-2 bg-slate-50 rounded-lg">
                <p className="text-xs text-slate-400">置信度</p>
                <p className={`text-sm font-bold ${confidenceColor(selectedCase.confidence)}`}>{(selectedCase.confidence * 100).toFixed(0)}%</p>
              </div>
              <div className="text-center p-2 bg-slate-50 rounded-lg">
                <p className="text-xs text-slate-400">需求</p>
                <p className="text-xs font-mono text-slate-600 truncate max-w-[100px] mx-auto">{selectedCase.req_id}</p>
              </div>
              <div className="text-center p-2 bg-slate-50 rounded-lg">
                <p className="text-xs text-slate-400">创建</p>
                <p className="text-xs text-slate-600">{formatDate(selectedCase.created_at)}</p>
              </div>
            </div>
            {selectedCase.case_json && (
              <pre className="text-xs text-slate-600 bg-slate-50 rounded-xl p-4 overflow-x-auto max-h-60">
                {JSON.stringify(selectedCase.case_json, null, 2)}
              </pre>
            )}
            <div className="flex justify-end mt-4">
              <button onClick={() => setSelectedCase(null)} className="btn-secondary">关闭</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
