import { useState, useEffect, useRef } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { Play, FileText, Search, CheckCircle2, AlertCircle, XCircle, Download, ArrowRight, Sparkles, Loader2, StopCircle, Folder, ChevronDown } from 'lucide-react'
import { requirements as reqApi, generateCases, testCases as tcApi, products as prodApi } from '../api/client'
import { useProduct } from '../store/ProductContext'
import type { SSEEvent, Requirement, TestCase, Project } from '../types'
import SSEProgress from '../components/SSEProgress'
import { formatDate, priorityBadge, statusBadge, confidenceColor, CASE_STATUS_LABELS } from '../utils'
import { caseTypeLabel } from '../utils/labels'

const SSE_STAGES = ['parse', 'select_channel', 'clarify', 'gate', 'impact', 'generate', 'review']

export default function Generate() {
  const { id: paramId } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const { selectedProductId, products } = useProduct()
  const selectedProductName = products.find(p => p.id === selectedProductId)?.name || selectedProductId
  const [projects, setProjects] = useState<Project[]>([])
  const [selectedProject, setSelectedProject] = useState<string>('')
  const [loadingProjects, setLoadingProjects] = useState(false)
  const [reqs, setReqs] = useState<Requirement[]>([])
  const [selectedReq, setSelectedReq] = useState(paramId || '')
  const [loadingReqs, setLoadingReqs] = useState(true)
  // 级联下拉框控制
  const [reqDropdownOpen, setReqDropdownOpen] = useState(false)
  const [reqSearch, setReqSearch] = useState('')
  const reqDropdownRef = useRef<HTMLDivElement>(null)

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
  // v2.0：新增一致性检查、FP 覆盖率、通道展示
  const [consistencyCheck, setConsistencyCheck] = useState<any>(null)
  const [fpCoverage, setFpCoverage] = useState<any>(null)
  const [channel, setChannel] = useState<string>('')
  const [done, setDone] = useState(false)
  const [error, setError] = useState('')
  // v2.0：导出错误独立状态，避免污染主流程 error
  const [exportError, setExportError] = useState('')

  // 加载当前产品下的项目列表
  useEffect(() => {
    if (!selectedProductId) {
      setProjects([])
      setSelectedProject('')
      return
    }
    setLoadingProjects(true)
    setSelectedProject('')  // 切换产品时重置项目选择
    prodApi.listProjects(selectedProductId)
      .then(list => setProjects(Array.isArray(list) ? list : []))
      .catch(e => {
        console.error('[Generate] 项目列表加载失败:', e)
        setProjects([])
      })
      .finally(() => setLoadingProjects(false))
  }, [selectedProductId])

  // 加载需求列表：按产品 + 项目过滤
  useEffect(() => {
    // 切换项目时清空已选需求（但保留 URL 参数传入的初始需求）
    if (!paramId) setSelectedReq('')
    setLoadingReqs(true)
    reqApi.list(1, 100, selectedProductId || undefined, selectedProject || undefined)
      .then(r => setReqs(r.items || []))
      .catch(e => {
        console.error('[Generate] 需求列表加载失败:', e)
        setError(`需求列表加载失败: ${e?.message || '网络异常'}`)
        setReqs([])
      })
      .finally(() => setLoadingReqs(false))
  }, [selectedProductId, selectedProject])

  // URL 参数传入需求 ID 时，自动选中该需求（若不在当前项目下，则切到"全部项目"再选）
  useEffect(() => {
    if (paramId) {
      setSelectedReq(paramId)
      // 若当前项目筛选下找不到该需求，则清除项目筛选以加载全部需求
      const exists = reqs.some(r => r.id === paramId)
      if (!exists && selectedProject) {
        setSelectedProject('')
      }
    }
  }, [paramId, reqs, selectedProject])

  // 点击外部关闭需求下拉框
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (reqDropdownRef.current && !reqDropdownRef.current.contains(e.target as Node)) {
        setReqDropdownOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  // 需求搜索过滤
  const filteredReqs = reqs.filter(r => {
    if (!reqSearch) return true
    const q = reqSearch.toLowerCase()
    return r.id.toLowerCase().includes(q) || (r.title || '').toLowerCase().includes(q)
  })

  const handleGenerate = () => {
    if (!selectedReq) return
    setGenerating(true)
    setCurrentStage('')
    setProgress(0)
    setMessage('正在启动生成流程...')
    setGeneratedCases([])
    setReviewReport(null)
    setKbFlowback(null)
    // v2.0：重置新增状态
    setConsistencyCheck(null)
    setFpCoverage(null)
    setChannel('')
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
          // v2.0：提取一致性检查、FP 覆盖率、通道
          setConsistencyCheck((e as any).consistency_check || null)
          setFpCoverage((e as any).fp_coverage || null)
          setChannel(e.channel || '')
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
      setExportError('没有可导出的用例')
      return
    }
    setExportError('')
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
      setExportError(e?.message || '导出失败')
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

      {/* Select project & requirement — 级联选择：项目 → 需求（带搜索） */}
      {!done && (
        <div className="card space-y-4">
          <h2 className="section-title flex items-center gap-2">
            <FileText className="w-4 h-4 text-primary-500" /> 选择项目与需求
          </h2>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            {/* 产品（只读显示） */}
            <div>
              <label className="block text-xs font-semibold text-slate-600 mb-1">当前产品</label>
              <div className="flex items-center gap-2 p-2.5 bg-slate-50 rounded-lg text-sm h-[38px]">
                <Folder className="w-4 h-4 text-primary-500 shrink-0" />
                <span className="font-medium text-slate-700 truncate">{selectedProductName || '未选择产品'}</span>
                {!selectedProductId && (
                  <span className="text-xs text-amber-600 ml-auto shrink-0">请先选择产品</span>
                )}
              </div>
            </div>

            {/* 项目下拉框 */}
            <div>
              <label className="block text-xs font-semibold text-slate-600 mb-1">
                项目 <span className="text-slate-400 font-normal">(级联筛选需求)</span>
              </label>
              <div className="relative">
                <Folder className="w-4 h-4 text-slate-400 absolute left-3 top-1/2 -translate-y-1/2 pointer-events-none" />
                <select
                  value={selectedProject}
                  onChange={e => setSelectedProject(e.target.value)}
                  className="select-field pl-9"
                  disabled={loadingProjects || !selectedProductId}
                >
                  <option value="">全部项目</option>
                  {projects.map(p => (
                    <option key={p.id} value={p.id}>{p.name} {p.version ? `(v${p.version})` : ''}</option>
                  ))}
                </select>
              </div>
              {loadingProjects && (
                <p className="text-xs text-slate-400 mt-1 flex items-center gap-1">
                  <Loader2 className="w-3 h-3 animate-spin" /> 加载项目...
                </p>
              )}
            </div>

            {/* 需求带搜索 combobox（级联：根据项目过滤） */}
            <div>
              <label className="block text-xs font-semibold text-slate-600 mb-1">
                需求 <span className="text-red-500">*</span>
                {selectedProject && (
                  <span className="text-slate-400 font-normal ml-1">
                    · 已按项目筛选
                  </span>
                )}
              </label>
              <div className="relative" ref={reqDropdownRef}>
                <div
                  className="flex items-center gap-2 px-3 py-2 rounded-lg cursor-pointer text-sm border transition-colors hover:border-primary-400"
                  style={{
                    background: 'var(--background, #fff)',
                    borderColor: reqDropdownOpen ? 'rgba(0, 122, 255, 0.4)' : 'var(--border, #e2e8f0)',
                    boxShadow: reqDropdownOpen ? '0 0 0 3px rgba(0, 122, 255, 0.1)' : 'none',
                  }}
                  onClick={() => setReqDropdownOpen(!reqDropdownOpen)}
                >
                  {selectedReq ? (
                    <span className="flex-1 truncate">
                      <span className="font-mono text-xs text-slate-400">{selectedReq}</span>
                      <span className="ml-2 text-slate-700">{reqs.find(r => r.id === selectedReq)?.title || '加载中...'}</span>
                    </span>
                  ) : (
                    <span className="flex-1 text-slate-400">
                      {loadingReqs ? '加载需求中...' : reqs.length === 0 ? '暂无可用需求' : '请选择需求（可搜索）'}
                    </span>
                  )}
                  <ChevronDown className={`w-4 h-4 text-slate-400 transition-transform ${reqDropdownOpen ? 'rotate-180' : ''}`} />
                </div>

                {/* 下拉面板：搜索框 + 需求列表 */}
                {reqDropdownOpen && (
                  <div className="absolute top-full left-0 mt-1 w-full z-40 rounded-lg overflow-hidden border" style={{ background: '#fff', borderColor: 'var(--border, #e2e8f0)', boxShadow: 'var(--shadow-apple-lg, 0 10px 30px rgba(0,0,0,0.12))' }}>
                    {/* 搜索框 */}
                    <div className="p-2 border-b" style={{ borderColor: 'var(--border, #e2e8f0)' }}>
                      <div className="relative">
                        <Search className="w-3.5 h-3.5 text-slate-400 absolute left-2.5 top-1/2 -translate-y-1/2" />
                        <input
                          value={reqSearch}
                          onChange={e => setReqSearch(e.target.value)}
                          placeholder="搜索需求 ID 或标题..."
                          className="w-full pl-8 pr-3 py-1.5 text-sm border rounded outline-none focus:border-primary-400"
                          style={{ borderColor: 'var(--border, #e2e8f0)' }}
                          autoFocus
                        />
                      </div>
                    </div>
                    {/* 需求列表 */}
                    <div className="max-h-60 overflow-y-auto">
                      {loadingReqs ? (
                        <div className="px-3 py-3 text-sm text-slate-400 flex items-center gap-2">
                          <Loader2 className="w-3 h-3 animate-spin" /> 加载中...
                        </div>
                      ) : filteredReqs.length === 0 ? (
                        <div className="px-3 py-3 text-sm text-slate-400">
                          {reqs.length === 0 ? '暂无可用需求' : '无匹配需求'}
                        </div>
                      ) : (
                        filteredReqs.map(r => (
                          <div
                            key={r.id}
                            onClick={() => {
                              setSelectedReq(r.id)
                              setReqDropdownOpen(false)
                              setReqSearch('')
                            }}
                            className="px-3 py-2 cursor-pointer hover:bg-slate-50 flex items-center gap-2 text-sm border-b last:border-0"
                            style={{ borderColor: 'var(--border, #f1f5f9)' }}
                          >
                            <span className="font-mono text-xs text-slate-400 shrink-0">{r.id}</span>
                            <span className="flex-1 truncate text-slate-700">{r.title}</span>
                            <span className={`text-[10px] px-1.5 py-0.5 rounded shrink-0 ${priorityBadge(r.priority)}`}>{r.priority}</span>
                            {r.id === selectedReq && <CheckCircle2 className="w-3.5 h-3.5 text-primary-500 shrink-0" />}
                          </div>
                        ))
                      )}
                    </div>
                  </div>
                )}
              </div>
              {!loadingReqs && reqs.length === 0 && selectedProductId && (
                <p className="text-xs text-amber-600 mt-1 flex items-center gap-1">
                  <AlertCircle className="w-3 h-3" />
                  {selectedProject
                    ? `项目下暂无需求`
                    : `当前产品下暂无需求`}
                </p>
              )}
            </div>
          </div>

          {/* 选中需求摘要 + 生成按钮 */}
          {selectedReq && req && (
            <div className="flex items-center justify-between gap-4 p-3 bg-slate-50 rounded-xl text-sm">
              <div className="flex items-center gap-3">
                <span className="font-mono text-xs text-slate-400">{req.id}</span>
                <span className="font-medium text-slate-700">{req.title}</span>
                <span className={priorityBadge(req.priority)}>{req.priority}</span>
              </div>
              <button onClick={handleGenerate} disabled={!selectedReq || generating} className="btn-primary">
                {generating ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
                开始生成
              </button>
            </div>
          )}
          {selectedReq && !req && (
            <div className="flex items-center justify-end">
              <button onClick={handleGenerate} disabled={!selectedReq || generating} className="btn-primary">
                {generating ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
                开始生成
              </button>
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
            {channel && (
              <span className="ml-2 px-2 py-0.5 text-xs bg-blue-100 text-blue-700 rounded-full">
                {channel === 'FULL' ? '完整通道' : channel === 'FAST' ? '快速通道' : channel}
              </span>
            )}
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
              <p className="text-xl font-bold text-blue-600">{((reviewReport.avg_confidence || 0) * 100).toFixed(0)}%</p>
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
                    {caseTypeLabel(type)}: {count}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* v2.0：一致性检查结果 */}
      {consistencyCheck && Object.keys(consistencyCheck).length > 0 && (
        <div className={`card space-y-3 border-l-4 ${consistencyCheck.is_inconsistent ? 'border-l-red-500' : 'border-l-emerald-500'}`}>
          <h2 className="section-title flex items-center gap-2">
            {consistencyCheck.is_inconsistent ? (
              <AlertCircle className="w-4 h-4 text-red-500" />
            ) : (
              <CheckCircle2 className="w-4 h-4 text-emerald-500" />
            )}
            一致性检查
            <span className={`text-xs px-2 py-0.5 rounded-full ${consistencyCheck.is_inconsistent ? 'bg-red-100 text-red-700' : 'bg-emerald-100 text-emerald-700'}`}>
              {consistencyCheck.is_inconsistent ? '存在异常' : '一致'}
            </span>
          </h2>
          <div className="grid grid-cols-3 gap-3">
            <div className="text-center p-3 bg-slate-50 rounded-xl">
              <p className="text-lg font-bold text-slate-700">
                {consistencyCheck.count_diff_pct !== undefined
                  ? `${consistencyCheck.count_diff_pct > 0 ? '+' : ''}${(consistencyCheck.count_diff_pct * 100).toFixed(1)}%`
                  : '—'}
              </p>
              <p className="text-xs text-slate-400">用例数差异</p>
            </div>
            <div className="text-center p-3 bg-slate-50 rounded-xl">
              <p className="text-lg font-bold text-slate-700">
                {consistencyCheck.type_jaccard !== undefined
                  ? `${(consistencyCheck.type_jaccard * 100).toFixed(1)}%`
                  : '—'}
              </p>
              <p className="text-xs text-slate-400">类型分布相似度</p>
            </div>
            <div className="text-center p-3 bg-slate-50 rounded-xl">
              <p className="text-lg font-bold text-slate-700">
                {consistencyCheck.semantic_similarity !== undefined
                  ? `${(consistencyCheck.semantic_similarity * 100).toFixed(1)}%`
                  : '—'}
              </p>
              <p className="text-xs text-slate-400">语义相似度</p>
            </div>
          </div>
          {consistencyCheck.reasons && consistencyCheck.reasons.length > 0 && (
            <div className="text-xs text-slate-500 bg-slate-50 rounded-lg p-3">
              <p className="font-medium mb-1">判定依据：</p>
              <ul className="list-disc list-inside space-y-0.5">
                {consistencyCheck.reasons.map((r: string, i: number) => (
                  <li key={i}>{r}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}

      {/* v2.0：FP 覆盖率 */}
      {fpCoverage && fpCoverage.total_fps > 0 && (
        <div className="card space-y-3">
          <h2 className="section-title flex items-center gap-2">
            <CheckCircle2 className="w-4 h-4 text-primary-500" /> 功能点覆盖
            <span className="text-sm font-normal text-slate-400">
              ({fpCoverage.covered_fps}/{fpCoverage.total_fps})
            </span>
          </h2>
          <div>
            <div className="flex items-center justify-between mb-1.5">
              <span className="text-xs text-slate-500">覆盖率</span>
              <span className={`text-sm font-mono font-medium ${
                (fpCoverage.coverage_rate || 0) >= 0.8 ? 'text-emerald-600' :
                (fpCoverage.coverage_rate || 0) >= 0.5 ? 'text-amber-600' : 'text-red-600'
              }`}>
                {((fpCoverage.coverage_rate || 0) * 100).toFixed(1)}%
              </span>
            </div>
            <div className="w-full h-2 bg-slate-100 rounded-full overflow-hidden">
              <div
                className={`h-full rounded-full transition-all ${
                  (fpCoverage.coverage_rate || 0) >= 0.8 ? 'bg-emerald-500' :
                  (fpCoverage.coverage_rate || 0) >= 0.5 ? 'bg-amber-500' : 'bg-red-500'
                }`}
                style={{ width: `${(fpCoverage.coverage_rate || 0) * 100}%` }}
              />
            </div>
          </div>
          {fpCoverage.uncovered && fpCoverage.uncovered.length > 0 && (
            <div>
              <h3 className="text-xs font-medium text-red-600 mb-2">未覆盖的功能点</h3>
              <div className="flex flex-wrap gap-1.5">
                {fpCoverage.uncovered.map((fp: any, i: number) => (
                  <span key={i} className="badge bg-red-50 text-red-600 text-xs">
                    {fp.name || fp.id}
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
          {exportError && (
            <p className="text-xs text-red-500 flex items-center gap-1">
              <AlertCircle className="w-3 h-3" /> {exportError}
            </p>
          )}
          <div className="space-y-2">
            {generatedCases.map((c) => (
              <div key={c.id} className="p-4 bg-slate-50/50 rounded-xl hover:bg-slate-50 transition-colors">
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="text-xs font-mono text-slate-400">{c.id}</span>
                    <p className="text-sm font-medium text-slate-700 truncate">{c.title}</p>
                  </div>
                  <div className="flex items-center gap-2 flex-shrink-0">
                    <span className="badge bg-blue-50 text-blue-600">{caseTypeLabel(c.type)}</span>
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
