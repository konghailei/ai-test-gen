import { useState, useEffect } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { Play, FileText, Search, CheckCircle2, AlertCircle, XCircle, Download, ArrowRight, Sparkles, Loader2, StopCircle } from 'lucide-react'
import { requirements as reqApi, generateCases, testCases as tcApi } from '../api/client'
import { useProduct } from '../store/ProductContext'
import type { SSEEvent, Requirement, TestCase } from '../types'
import SSEProgress from '../components/SSEProgress'
import { formatDate, priorityBadge, statusBadge, confidenceColor, CASE_STATUS_LABELS } from '../utils'

const SSE_STAGES = ['parse', 'select_channel', 'clarify', 'gate', 'impact', 'generate', 'review']

export default function Generate() {
  const { id: paramId } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const { selectedProductId, products } = useProduct()
  const selectedProductName = products.find(p => p.id === selectedProductId)?.name || selectedProductId
  const [reqs, setReqs] = useState<Requirement[]>([])
  const [selectedReq, setSelectedReq] = useState(paramId || '')
  const [loadingReqs, setLoadingReqs] = useState(true)

  // SSE state
  const [generating, setGenerating] = useState(false)
  const [currentStage, setCurrentStage] = useState('')
  const [progress, setProgress] = useState(0)
  const [message, setMessage] = useState('')
  const [abortController, setAbortController] = useState<AbortController | null>(null)

  // Results
  const [generatedCases, setGeneratedCases] = useState<TestCase[]>([])
  const [reviewReport, setReviewReport] = useState<any>(null)
  const [kbFlowback, setKbFlowback] = useState<any>(null)
  const [done, setDone] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    reqApi.list(1, 100, selectedProductId || undefined).then(r => setReqs(r.items || [])).catch(() => {}).finally(() => setLoadingReqs(false))
  }, [selectedProductId])

  useEffect(() => {
    if (paramId) setSelectedReq(paramId)
  }, [paramId])

  const handleGenerate = () => {
    if (!selectedReq) return
    setGenerating(true)
    setCurrentStage('')
    setProgress(0)
    setMessage('正在启动生成流程...')
    setGeneratedCases([])
    setReviewReport(null)
    setKbFlowback(null)
    setDone(false)
    setError('')

    const ctrl = generateCases(
      selectedReq,
      (event: any) => {
        const e = event as SSEEvent
        if (e.stage) setCurrentStage(e.stage)
        if (e.progress !== undefined) setProgress(e.progress)
        if (e.message) setMessage(e.message)
        if (e.stage === 'done') {
          setDone(true)
          setGeneratedCases(e.cases || [])
          setReviewReport(e.review_report || null)
          setKbFlowback(e.kb_flowback || null)
          setMessage('生成完成！')
          setProgress(100)
          setGenerating(false)
        }
        if (e.stage === 'error') {
          setError(e.message || '生成过程出错')
          setGenerating(false)
        }
      },
      (err) => { setError(err); setGenerating(false) },
      () => { setGenerating(false) }
    )
    setAbortController(ctrl)
  }

  const handleCancel = () => {
    abortController?.abort()
    setGenerating(false)
    setMessage('已取消')
  }

  const handleExport = async (format: string) => {
    if (generatedCases.length === 0) {
      setError('没有可导出的用例')
      return
    }
    try {
      const { exportCases } = await import('../api/client')
      // ⚠️ 必须传 case_ids：后端 export_testcases 要求 case_ids 非空，否则 400
      const blob = await exportCases.download(format, generatedCases.map(c => c.id))
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      const ext = format === 'markdown' ? 'md' : format === 'gherkin' ? 'feature' : format === 'pytest' ? 'py' : 'json'
      a.href = url; a.download = `testcases.${ext}`
      a.click()
      URL.revokeObjectURL(url)
    } catch (e: any) {
      setError(e?.message || '导出失败')
    }
  }

  const req = reqs.find(r => r.id === selectedReq)

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="page-title">测试用例生成</h1>
          <p className="text-sm text-slate-500 mt-1">选择需求 → 流式生成 → 查看结果</p>
        </div>
      </div>

      {/* Select requirement */}
      {!done && (
        <div className="card space-y-4">
          <h2 className="section-title flex items-center gap-2">
            <FileText className="w-4 h-4 text-primary-500" /> 选择需求
          </h2>
          <div className="flex items-center gap-3">
            <div className="flex-1 relative">
              <Search className="w-4 h-4 text-slate-400 absolute left-3.5 top-1/2 -translate-y-1/2" />
              <select value={selectedReq} onChange={e => setSelectedReq(e.target.value)}
                className="select-field pl-10">
                <option value="">请选择需求...</option>
                {reqs.map(r => <option key={r.id} value={r.id}>{r.id} — {r.title}</option>)}
              </select>
            </div>
            <button onClick={handleGenerate} disabled={!selectedReq || generating} className="btn-primary">
              {generating ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
              开始生成
            </button>
          </div>
          {loadingReqs && (
            <p className="text-sm text-slate-400 flex items-center gap-2">
              <Loader2 className="w-3 h-3 animate-spin" /> 正在加载需求列表...
            </p>
          )}
          {!loadingReqs && reqs.length === 0 && (
            <p className="text-sm text-amber-600 flex items-center gap-2">
              <AlertCircle className="w-4 h-4" />
              {selectedProductId
                ? `当前产品「${selectedProductName}」下暂无需求，请切换产品或在「需求管理」中上传需求`
                : '暂无可用需求，请先在「需求管理」中上传需求'}
            </p>
          )}
          {selectedReq && req && (
            <div className="flex items-center gap-4 p-3 bg-slate-50 rounded-xl text-sm">
              <span className="font-mono text-xs text-slate-400">{req.id}</span>
              <span className="font-medium text-slate-700">{req.title}</span>
              <span className={priorityBadge(req.priority)}>{req.priority}</span>
            </div>
          )}
        </div>
      )}

      {/* SSE Progress */}
      {(generating || done) && (
        <SSEProgress stages={SSE_STAGES} currentStage={currentStage}
          progress={progress} message={message}
          onCancel={generating ? handleCancel : undefined} />
      )}

      {/* Error */}
      {error && (
        <div className="bg-red-50 border border-red-100 rounded-2xl p-4 flex items-start gap-3">
          <XCircle className="w-5 h-5 text-red-500 flex-shrink-0 mt-0.5" />
          <div>
            <p className="text-sm font-medium text-red-800">生成出错</p>
            <p className="text-xs text-red-600 mt-0.5">{error}</p>
          </div>
        </div>
      )}

      {/* KB Flowback Summary */}
      {kbFlowback && (
        <div className="bg-gradient-to-r from-emerald-50 to-green-50 border border-emerald-100 rounded-2xl p-5">
          <h3 className="font-semibold text-emerald-800 text-sm flex items-center gap-2 mb-3">
            <Sparkles className="w-4 h-4" /> 知识库回流
          </h3>
          <div className="grid grid-cols-4 gap-3">
            <div className="text-center p-3 bg-white/60 rounded-xl">
              <p className="text-lg font-bold text-emerald-600">{kbFlowback.auto_approved || 0}</p>
              <p className="text-xs text-emerald-600/70">自动入库</p>
            </div>
            <div className="text-center p-3 bg-white/60 rounded-xl">
              <p className="text-lg font-bold text-amber-600">{kbFlowback.pending_review || 0}</p>
              <p className="text-xs text-amber-600/70">待审核</p>
            </div>
            <div className="text-center p-3 bg-white/60 rounded-xl">
              <p className="text-lg font-bold text-slate-500">{kbFlowback.rejected || 0}</p>
              <p className="text-xs text-slate-500/70">已驳回</p>
            </div>
            <div className="text-center p-3 bg-white/60 rounded-xl">
              <p className="text-lg font-bold text-red-500">{kbFlowback.errors || 0}</p>
              <p className="text-xs text-red-500/70">入库错误</p>
            </div>
          </div>
        </div>
      )}

      {/* Review Report */}
      {reviewReport && (
        <div className="card space-y-3">
          <h2 className="section-title">审核报告</h2>
          <div className="grid grid-cols-4 gap-3">
            <div className="text-center p-3 bg-slate-50 rounded-xl">
              <p className="text-xl font-bold text-slate-700">{reviewReport.total_cases}</p>
              <p className="text-xs text-slate-400">总用例</p>
            </div>
            <div className="text-center p-3 bg-success-50 rounded-xl">
              <p className="text-xl font-bold text-success-600">{reviewReport.passed_cases}</p>
              <p className="text-xs text-success-600/70">通过</p>
            </div>
            <div className="text-center p-3 bg-red-50 rounded-xl">
              <p className="text-xl font-bold text-red-500">{reviewReport.blocked_cases}</p>
              <p className="text-xs text-red-500/70">blocker</p>
            </div>
            <div className="text-center p-3 bg-blue-50 rounded-xl">
              <p className="text-xl font-bold text-blue-600">{(reviewReport.avg_confidence * 100).toFixed(0)}%</p>
              <p className="text-xs text-blue-600/70">平均置信度</p>
            </div>
          </div>

          {reviewReport.issues?.length > 0 && (
            <div>
              <h3 className="text-sm font-medium text-slate-600 mb-2">发现的问题</h3>
              <div className="space-y-1.5">
                {reviewReport.issues.map((issue: any, i: number) => (
                  <div key={i} className={`flex items-start gap-2 p-2.5 rounded-lg text-xs ${
                    issue.severity === 'blocker' ? 'bg-red-50 text-red-700' : 'bg-amber-50 text-amber-700'
                  }`}>
                    {issue.severity === 'blocker' ? <XCircle className="w-3.5 h-3.5 mt-0.5 flex-shrink-0" /> : <AlertCircle className="w-3.5 h-3.5 mt-0.5 flex-shrink-0" />}
                    <span>{issue.case_id}: {issue.message}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {reviewReport.coverage_summary && (
            <div>
              <h3 className="text-sm font-medium text-slate-600 mb-2">覆盖率</h3>
              <div className="flex flex-wrap gap-2">
                {Object.entries(reviewReport.coverage_summary).map(([type, count]: [string, any]) => (
                  <span key={type} className="badge bg-slate-100 text-slate-600">
                    {type}: {count}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Generated cases */}
      {generatedCases.length > 0 && (
        <div className="card space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="section-title flex items-center gap-2">
              <CheckCircle2 className="w-4 h-4 text-emerald-500" /> 生成结果
              <span className="text-sm font-normal text-slate-400">({generatedCases.length} 个用例)</span>
            </h2>
            <div className="flex items-center gap-2">
              {['markdown', 'gherkin', 'pytest', 'json'].map(f => (
                <button key={f} onClick={() => handleExport(f)} className="btn-ghost text-xs">{f}</button>
              ))}
            </div>
          </div>
          <div className="space-y-2">
            {generatedCases.map((c) => (
              <div key={c.id} className="p-4 bg-slate-50/50 rounded-xl hover:bg-slate-50 transition-colors">
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="text-xs font-mono text-slate-400">{c.id}</span>
                    <p className="text-sm font-medium text-slate-700 truncate">{c.title}</p>
                  </div>
                  <div className="flex items-center gap-2 flex-shrink-0">
                    <span className="badge bg-blue-50 text-blue-600">{c.type}</span>
                    <span className={priorityBadge(c.priority)}>{c.priority}</span>
                    <span className={statusBadge(c.status || 'draft')}>{CASE_STATUS_LABELS[c.status] || c.status || 'draft'}</span>
                    <span className={`text-xs font-mono ${confidenceColor(c.confidence)}`}>
                      {(c.confidence * 100).toFixed(0)}%
                    </span>
                  </div>
                </div>
                {c.case_json && (
                  <details className="group">
                    <summary className="text-xs text-slate-400 cursor-pointer hover:text-slate-600">查看详情</summary>
                    <pre className="mt-2 text-xs text-slate-600 bg-white rounded-lg p-3 overflow-x-auto">
                      {JSON.stringify(c.case_json, null, 2)}
                    </pre>
                  </details>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Regenerate */}
      {done && (
        <div className="flex justify-center gap-3">
          <button onClick={() => { setDone(false); setGeneratedCases([]) }} className="btn-secondary">
            <FileText className="w-4 h-4" /> 选择其他需求
          </button>
          <button onClick={handleGenerate} className="btn-primary">
            <Play className="w-4 h-4" /> 重新生成
          </button>
        </div>
      )}
    </div>
  )
}
