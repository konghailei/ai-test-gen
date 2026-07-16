import { useState, useEffect } from 'react'
import { TestTube, Trash2, Eye, ThumbsUp, ThumbsDown, Loader2, Play, Sparkles, RefreshCw, Filter, X } from 'lucide-react'
import { testCases as tcApi, requirements as reqApi, generateCases } from '../api/client'
import { useProduct } from '../store/ProductContext'
import type { TestCase, Requirement, SSEEvent } from '../types'
import { formatDate, priorityBadge, statusBadge, confidenceColor, CASE_STATUS_LABELS } from '../utils'
import { caseTypeLabel } from '../utils/labels'

export default function TestCases() {
  const { selectedProductId } = useProduct()
  const [cases, setCases] = useState<TestCase[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(true)
  const [filterReq, setFilterReq] = useState('')
  const [reqs, setReqs] = useState<Requirement[]>([])
  const [selectedCase, setSelectedCase] = useState<TestCase | null>(null)

  // ---- 生成状态 ----
  const [showGenerate, setShowGenerate] = useState(false)
  const [genReqId, setGenReqId] = useState('')
  const [generating, setGenerating] = useState(false)
  const [genStage, setGenStage] = useState('')
  const [genProgress, setGenProgress] = useState(0)
  const [genMessage, setGenMessage] = useState('')
  const [genDone, setGenDone] = useState(false)

  useEffect(() => {
    reqApi.list(1, 100, selectedProductId || undefined).then(r => setReqs(r.items || [])).catch(() => {})
  }, [selectedProductId])

  const loadCases = () => {
    setLoading(true)
    tcApi.list(filterReq || undefined, page, 20, selectedProductId || undefined).then(r => {
      setCases(r.items || [])
      setTotal(r.total || 0)
    }).catch(() => {}).finally(() => setLoading(false))
  }

  useEffect(() => { loadCases() }, [page, filterReq, selectedProductId])

  const handleDelete = async (caseId: string) => {
    if (!confirm('确认删除此用例？')) return
    try { await tcApi.delete(caseId); loadCases() } catch (e: any) { alert('删除失败: ' + e.message) }
  }

  // 按需求批量删除
  const handleBatchDelete = async (reqId: string) => {
    const reqName = reqs.find(r => r.id === reqId)?.title || reqId
    if (!confirm(`确认删除需求「${reqName}」的所有用例？此操作不可撤销。`)) return
    try {
      await tcApi.deleteByRequirement(reqId)
      loadCases()
    } catch (e: any) { alert('批量删除失败: ' + e.message) }
  }

  // 开始生成用例
  const handleGenerate = () => {
    if (!genReqId) return
    setGenerating(true)
    setGenStage('')
    setGenProgress(0)
    setGenMessage('正在启动...')
    setGenDone(false)

    const ctrl = generateCases(
      genReqId,
      (event: any) => {
        const e = event as SSEEvent
        if (e.stage) setGenStage(e.stage)
        if (e.progress !== undefined) setGenProgress(e.progress)
        if (e.message) setGenMessage(e.message)
      },
      (err) => {
        setGenerating(false)
        setGenMessage('生成失败: ' + err)
      },
      () => {
        setGenerating(false)
        setGenDone(true)
        setGenProgress(100)
        loadCases()
      },
    )
    // 5 分钟超时
    setTimeout(() => { if (!ctrl.signal.aborted) ctrl.abort() }, 300000)
  }

  const totalPages = Math.ceil(total / 20)

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="page-title">测试用例</h1>
          <p className="text-sm text-slate-500 mt-1">{total} 个用例</p>
        </div>
        <button onClick={() => setShowGenerate(!showGenerate)}
          className="btn-primary flex items-center gap-2">
          <Sparkles className="w-4 h-4" />
          {showGenerate ? '收起' : '生成用例'}
        </button>
      </div>

      {/* 生成面板 */}
      {showGenerate && (
        <div className="card p-5 space-y-4">
          <h3 className="text-sm font-semibold text-slate-700 flex items-center gap-2">
            <Play className="w-4 h-4 text-primary-500" /> 生成测试用例
          </h3>
          <div className="flex items-end gap-3">
            <div className="flex-1">
              <label className="text-xs text-slate-500 block mb-1">选择需求</label>
              <select value={genReqId} onChange={e => setGenReqId(e.target.value)}
                className="select-field w-full text-sm" disabled={generating}>
                <option value="">-- 请选择需求 --</option>
                {reqs.filter(r => r.status === 'active').map(r => (
                  <option key={r.id} value={r.id}>{r.id} — {r.title}</option>
                ))}
              </select>
            </div>
            <button onClick={handleGenerate} disabled={generating || !genReqId}
              className="btn-primary flex items-center gap-2 disabled:opacity-50">
              {generating ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
              {generating ? '生成中...' : '生成'}
            </button>
            {genReqId && (
              <button onClick={() => handleBatchDelete(genReqId)}
                className="btn-secondary text-red-500 flex items-center gap-2 text-sm"
                title={`删除需求 ${genReqId} 的所有用例`}>
                <Trash2 className="w-4 h-4" /> 清空
              </button>
            )}
          </div>
          {(generating || genDone) && (
            <div>
              <div className="flex items-center justify-between text-xs text-slate-500 mb-1">
                <span>{genMessage}</span>
                <span>{genProgress}%</span>
              </div>
              <div className="h-2 bg-slate-100 rounded-full overflow-hidden">
                <div className={`h-full rounded-full transition-all duration-500 ${genDone ? 'bg-emerald-500' : 'bg-primary-500'}`}
                  style={{ width: `${genProgress}%` }} />
              </div>
              {genDone && <p className="text-xs text-emerald-600 mt-1">生成完成，列表已刷新</p>}
            </div>
          )}
        </div>
      )}

      {/* Filters */}
      <div className="card py-3 px-5">
        <div className="flex items-center gap-3">
          <Filter className="w-4 h-4 text-slate-400" />
          <select value={filterReq} onChange={e => { setFilterReq(e.target.value); setPage(1) }}
            className="select-field max-w-xs text-sm">
            <option value="">全部需求</option>
            {reqs.map(r => <option key={r.id} value={r.id}>{r.id} — {r.title}</option>)}
          </select>
          {filterReq && (
            <button onClick={() => handleBatchDelete(filterReq)}
              className="btn-secondary text-red-400 text-xs flex items-center gap-1">
              <Trash2 className="w-3 h-3" /> 删除此需求全部用例
            </button>
          )}
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
            <p className="text-sm text-slate-400 mt-1">选择需求并点击「生成用例」</p>
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
                  <th className="text-right px-5 py-3.5 w-28"></th>
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
                      <span className="badge bg-blue-50 text-blue-600">{caseTypeLabel(c.type)}</span>
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
                        <button onClick={() => handleDelete(c.id)} className="btn-ghost p-1.5 text-slate-400 hover:text-red-500" title="删除">
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
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
                <p className="text-sm font-medium text-slate-700">{caseTypeLabel(selectedCase.type)}</p>
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
