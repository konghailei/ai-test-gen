import { useState, useEffect, useRef } from 'react'
import { TestTube, Trash2, Loader2, Play, Sparkles, Filter, Download, ChevronDown, ChevronRight, ListChecks, AlertTriangle, FileText, Search, X } from 'lucide-react'
import { testCases as tcApi, requirements as reqApi, generateCases, exportCases, products as prodApi } from '../api/client'
import { useProduct } from '../store/ProductContext'
import type { TestCase, Requirement, Project, SSEEvent } from '../types'
import { formatDate, priorityBadge, statusBadge, confidenceColor, CASE_STATUS_LABELS } from '../utils'
import { caseTypeLabel } from '../utils/labels'

const PAGE_SIZE = 20

function groupByFP(cases: TestCase[]) {
  const map: Record<string, TestCase[]> = {}
  for (const c of cases) {
    const key = c.functional_point || c.req_id || '未分类'
    if (!map[key]) map[key] = []
    map[key].push(c)
  }
  return Object.entries(map).sort(([a], [b]) => a.localeCompare(b))
}

export default function TestCases() {
  const { selectedProductId } = useProduct()
  const [cases, setCases] = useState<TestCase[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(true)
  const [batchDeleting, setBatchDeleting] = useState(false)

  // 筛选
  const [filterProject, setFilterProject] = useState('')
  const [filterReq, setFilterReq] = useState('')
  const [filterSearch, setFilterSearch] = useState('')
  const [reqs, setReqs] = useState<Requirement[]>([])
  const [projects, setProjects] = useState<Project[]>([])
  // 项目搜索下拉
  const [projectSearch, setProjectSearch] = useState('')
  const [showProjectDropdown, setShowProjectDropdown] = useState(false)
  const projectDropdownRef = useRef<HTMLDivElement>(null)
  const selectedProject = projects.find(p => p.id === filterProject)

  // 折叠
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set())
  const [expandedCases, setExpandedCases] = useState<Set<string>>(new Set())

  // 导出
  const [exporting, setExporting] = useState(false)

  // 生成
  const [showGenerate, setShowGenerate] = useState(false)
  const [genReqId, setGenReqId] = useState('')
  const [generating, setGenerating] = useState(false)
  const [genProgress, setGenProgress] = useState(0)
  const [genMessage, setGenMessage] = useState('')
  const [genDone, setGenDone] = useState(false)

  // 生成面板专用：单独的搜索式需求下拉
  const [genReqSearch, setGenReqSearch] = useState('')
  const [showGenReqDropdown, setShowGenReqDropdown] = useState(false)
  const genReqDropdownRef = useRef<HTMLDivElement>(null)
  const genReqFiltered = reqs.filter(r => {
    if (!genReqSearch) return true
    const s = genReqSearch.toLowerCase()
    return r.id.toLowerCase().includes(s) || r.title.toLowerCase().includes(s)
  })

  // 加载需求（先按产品过滤，无结果回退全部）
  useEffect(() => {
    const pid = selectedProductId || undefined
    const load = async () => {
      try {
        let r = await reqApi.list(1, 300, pid, filterProject || undefined)
        setReqs(r.items || [])
        // 产品过滤无结果时回退到不过滤
        if ((!r.items || r.items.length === 0) && pid) {
          r = await reqApi.list(1, 300, undefined, undefined)
          setReqs(r.items || [])
        }
      } catch { setReqs([]) }
      pid ? prodApi.listProjects(pid).then((r: any) => setProjects(Array.isArray(r) ? r : [])).catch(() => {}) : Promise.resolve()
    }
    load()
  }, [selectedProductId, filterProject])

  const loadCases = () => {
    setLoading(true)
    tcApi.list(
      filterReq || undefined,
      filterProject || undefined,
      filterSearch || undefined,
      page, PAGE_SIZE,
      selectedProductId || undefined,
    ).then(r => {
      setCases(r.items || [])
      setTotal(r.total || 0)
      setExpandedGroups(new Set(groupByFP(r.items || []).map(([k]) => k)))
    }).catch(() => {}).finally(() => setLoading(false))
  }

  useEffect(() => { loadCases() }, [page, filterReq, filterProject, filterSearch, selectedProductId])

  // 关闭下拉（点击外部）
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (projectDropdownRef.current && !projectDropdownRef.current.contains(e.target as Node))
        setShowProjectDropdown(false)
      if (genReqDropdownRef.current && !genReqDropdownRef.current.contains(e.target as Node))
        setShowGenReqDropdown(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  const toggleGroup = (key: string) => setExpandedGroups(prev => { const n = new Set(prev); n.has(key) ? n.delete(key) : n.add(key); return n })
  const toggleCase = (id: string) => setExpandedCases(prev => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n })

  const handleDelete = async (caseId: string) => {
    if (!confirm('确认删除此用例？')) return
    try { await tcApi.delete(caseId); loadCases() } catch (e: any) { alert('删除失败: ' + e.message) }
  }

  // 批量删除：按当前筛选条件 + 项目
  const handleBatchDelete = async () => {
    if (!filterProject) { alert('请先选择项目再批量删除'); return }
    if (total === 0) { alert('当前筛选条件下没有用例'); return }
    const hint = filterReq
      ? `需求「${reqs.find(r => r.id === filterReq)?.title || filterReq}」`
      : `项目「${selectedProject?.name || filterProject}」`
    if (filterSearch) {
      if (!confirm(`确认删除 ${hint} 中匹配「${filterSearch}」的 ${total} 个用例？此操作不可撤销。`)) return
    } else {
      if (!confirm(`确认删除 ${hint} 下的全部 ${total} 个用例？此操作不可撤销。`)) return
    }
    setBatchDeleting(true)
    try {
      if (filterReq) {
        await tcApi.deleteByRequirement(filterReq)
      } else {
        // 按项目逐个删除
        const ids = cases.map(c => c.id)
        for (const id of ids) { await tcApi.delete(id).catch(() => {}) }
      }
      loadCases()
    } catch (e: any) { alert('批量删除失败: ' + e.message) }
    finally { setBatchDeleting(false) }
  }

  // 导出：按当前筛选条件 + 必须选项目
  const handleExportXMind = async () => {
    if (!filterProject) { alert('请先选择项目再导出'); return }
    if (cases.length === 0) { alert('当前筛选条件下没有可导出的用例'); return }
    setExporting(true)
    try {
      const blob = await exportCases.download('xmind', cases.map(c => c.id), selectedProject?.name || filterProject)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a'); a.href = url; a.download = `testcases-${selectedProject?.name || filterProject}.xmind`; a.click()
      URL.revokeObjectURL(url)
    } catch (e: any) { alert('导出失败: ' + e.message) }
    finally { setExporting(false) }
  }

  const handleGenerate = async (reqId?: string) => {
    const targetReqId = reqId || genReqId
    if (!targetReqId) return
    setGenReqId(targetReqId)
    setGenerating(true); setGenProgress(0); setGenMessage('启动中...'); setGenDone(false)
    const ctrl = generateCases(targetReqId,
      (e: any) => { if (e.stage) setGenProgress(e.progress || 0); if (e.message) setGenMessage(e.message) },
      (err: string) => { setGenerating(false); setGenMessage('失败: ' + err) },
      () => { setGenerating(false); setGenDone(true); setGenProgress(100); loadCases() },
    )
    setTimeout(() => { if (!ctrl.signal.aborted) ctrl.abort() }, 300000)
  }

  // 项目过滤
  const filteredProjects = projects.filter(p =>
    !projectSearch || p.name.toLowerCase().includes(projectSearch.toLowerCase()) || p.id.includes(projectSearch)
  )

  const groups = groupByFP(cases)
  const totalPages = Math.ceil(total / PAGE_SIZE)

  return (
    <div className="space-y-6">
      {/* 标题栏 */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="page-title">测试用例</h1>
          <p className="text-sm text-slate-500 mt-1">{total} 个用例，{groups.length} 个功能点</p>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={() => setShowGenerate(!showGenerate)} className="btn-primary flex items-center gap-2">
            <Sparkles className="w-4 h-4" />{showGenerate ? '收起' : '生成用例'}
          </button>
          {/* 当前筛选了需求时，显示快捷重新生成 */}
          {filterReq && (
            <button onClick={() => { setShowGenerate(true); handleGenerate(filterReq) }} disabled={generating}
              className="btn-secondary flex items-center gap-2 text-primary-600 border-primary-200">
              {generating ? <Loader2 className="w-4 h-4 animate-spin" /> : <Sparkles className="w-4 h-4" />}
              重新生成
            </button>
          )}
          <button onClick={handleBatchDelete} disabled={batchDeleting || total === 0}
            className="btn-secondary text-red-500 flex items-center gap-2 disabled:opacity-50">
            {batchDeleting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Trash2 className="w-4 h-4" />}
            批量删除
          </button>
          <button onClick={handleExportXMind} disabled={exporting || !filterProject || cases.length === 0}
            className="btn-secondary flex items-center gap-2 disabled:opacity-50">
            {exporting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
            导出 XMind
          </button>
        </div>
      </div>

      {/* 生成面板 */}
      {showGenerate && (
        <div className="card p-5 space-y-4">
          <h3 className="text-sm font-semibold text-slate-700 flex items-center gap-2"><Play className="w-4 h-4 text-primary-500" /> 生成测试用例</h3>
          <div className="flex items-end gap-3">
            <div className="flex-1 relative" ref={genReqDropdownRef}>
              <div className="flex items-center border border-slate-200 rounded-lg bg-white">
                <Search className="w-3.5 h-3.5 ml-2.5 text-slate-400 shrink-0" />
                <input value={genReqSearch} onChange={e => { setGenReqSearch(e.target.value); setShowGenReqDropdown(true) }}
                  onFocus={() => setShowGenReqDropdown(true)}
                  placeholder={genReqId ? `${genReqId} ${reqs.find(r => r.id === genReqId)?.title || ''}` : "搜索需求 ID 或标题..."}
                  className="w-full px-2.5 py-2 text-sm outline-none bg-transparent" disabled={generating} />
                {genReqId && (
                  <button onClick={() => { setGenReqId(''); setGenReqSearch(''); }}
                    className="px-2 text-slate-400 hover:text-slate-600"><X className="w-3.5 h-3.5" /></button>
                )}
              </div>
              {showGenReqDropdown && (
                <div className="absolute top-full left-0 mt-1 w-full bg-white rounded-lg shadow-lg border border-slate-200 py-1 z-40 max-h-48 overflow-y-auto">
                  {genReqFiltered.length === 0 ? (
                    <p className="text-xs text-slate-400 px-3 py-2">
                      {reqs.length === 0 ? '暂未加载需求列表，请检查产品选择' : '无匹配需求'}
                    </p>
                  ) : (
                    genReqFiltered.slice(0, 50).map(r => (
                      <button key={r.id} onClick={() => { setGenReqId(r.id); setGenReqSearch(''); setShowGenReqDropdown(false) }}
                        className={`w-full text-left px-3 py-2 text-sm hover:bg-slate-50 ${genReqId === r.id ? 'bg-primary-50 text-primary-600 font-medium' : 'text-slate-600'}`}>
                        <span className="font-mono text-xs text-slate-400 mr-2">{r.id}</span>{r.title}
                      </button>
                    ))
                  )}
                </div>
              )}
            </div>
            <button onClick={() => handleGenerate()} disabled={generating || !genReqId} className="btn-primary flex items-center gap-2 disabled:opacity-50 shrink-0">
              {generating ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}{generating ? '生成中...' : '生成'}
            </button>
          </div>
          {(generating || genDone) && (
            <div>
              <div className="flex justify-between text-xs text-slate-500 mb-1"><span>{genMessage}</span><span>{genProgress}%</span></div>
              <div className="h-2 bg-slate-100 rounded-full"><div className={`h-full rounded-full transition-all ${genDone ? 'bg-emerald-500' : 'bg-primary-500'}`} style={{ width: `${genProgress}%` }} /></div>
            </div>
          )}
        </div>
      )}

      {/* 筛选栏 */}
      <div className="card py-3 px-5">
        <div className="flex items-center gap-3 flex-wrap">
          <Filter className="w-4 h-4 text-slate-400 shrink-0" />

          {/* 项目 — 可搜索下拉 */}
          <div className="relative" ref={projectDropdownRef}>
            <div className="flex items-center border border-slate-200 rounded-lg bg-white min-w-[180px]">
              <input value={projectSearch} onChange={e => { setProjectSearch(e.target.value); setShowProjectDropdown(true) }}
                onFocus={() => setShowProjectDropdown(true)}
                placeholder={selectedProject?.name || "搜索项目..."}
                className="w-full px-3 py-2 text-sm outline-none rounded-lg bg-transparent" />
              {filterProject && (
                <button onClick={() => { setFilterProject(''); setProjectSearch(''); setPage(1) }}
                  className="px-2 text-slate-400 hover:text-slate-600"><X className="w-3.5 h-3.5" /></button>
              )}
              <ChevronDown className={`w-3.5 h-3.5 mr-2 text-slate-400 transition-transform ${showProjectDropdown ? 'rotate-180' : ''}`} />
            </div>
            {showProjectDropdown && (
              <div className="absolute top-full left-0 mt-1 w-full bg-white rounded-lg shadow-lg border border-slate-200 py-1 z-50 max-h-48 overflow-y-auto">
                {filteredProjects.length === 0 ? (
                  <p className="text-xs text-slate-400 px-3 py-2">无匹配项目</p>
                ) : (
                  filteredProjects.map(p => (
                    <button key={p.id} onClick={() => { setFilterProject(p.id); setProjectSearch(''); setShowProjectDropdown(false); setFilterReq(''); setPage(1) }}
                      className={`w-full text-left px-3 py-2 text-sm hover:bg-slate-50 ${filterProject === p.id ? 'bg-primary-50 text-primary-600 font-medium' : 'text-slate-600'}`}>
                      {p.name} <span className="text-xs text-slate-400">{p.version ? `v${p.version}` : ''}</span>
                    </button>
                  ))
                )}
              </div>
            )}
          </div>

          {/* 需求 */}
          <div className="flex items-center gap-1.5">
            <span className="text-xs text-slate-400 shrink-0">需求</span>
            <select value={filterReq} onChange={e => { setFilterReq(e.target.value); setPage(1) }}
              className="select-field text-sm min-w-[200px]">
              <option value="">全部需求</option>
              {reqs.map(r => <option key={r.id} value={r.id}>{r.id} {r.title}</option>)}
            </select>
          </div>

          {/* 标题搜索 */}
          <div className="flex items-center border border-slate-200 rounded-lg bg-white min-w-[200px]">
            <Search className="w-3.5 h-3.5 ml-2.5 text-slate-400" />
            <input value={filterSearch} onChange={e => { setFilterSearch(e.target.value); setPage(1) }}
              onKeyDown={e => e.key === 'Enter' && loadCases()}
              placeholder="搜索用例标题..."
              className="w-full px-2.5 py-2 text-sm outline-none bg-transparent" />
            {filterSearch && (
              <button onClick={() => { setFilterSearch(''); setPage(1) }}
                className="px-2 text-slate-400 hover:text-slate-600"><X className="w-3.5 h-3.5" /></button>
            )}
          </div>

          {/* 操作 */}
          {(filterReq || filterProject || filterSearch) && (
            <button onClick={() => { setFilterReq(''); setFilterProject(''); setFilterSearch(''); setProjectSearch(''); setPage(1) }}
              className="text-xs text-primary-500 hover:underline shrink-0">清除全部</button>
          )}
        </div>
      </div>

      {/* 用例列表 */}
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
            {groups.map(([fpKey, fpCases]) => {
              const isExpanded = expandedGroups.has(fpKey)
              const reqLabel = reqs.find(r => r.id === fpCases[0]?.req_id)?.title || fpCases[0]?.req_id || ''
              const headerLabel = fpCases[0]?.functional_point ? fpKey : `${fpKey} ${reqLabel}`
              return (
                <div key={fpKey} className="border-b border-slate-100 last:border-b-0">
                  <button onClick={() => toggleGroup(fpKey)}
                    className="w-full flex items-center gap-2 px-5 py-3 bg-slate-50/50 hover:bg-slate-50 transition-colors text-left">
                    {isExpanded ? <ChevronDown className="w-4 h-4 text-slate-400" /> : <ChevronRight className="w-4 h-4 text-slate-400" />}
                    <FileText className="w-4 h-4 text-primary-400" />
                    <span className="text-sm font-semibold text-slate-700">{headerLabel}</span>
                    <span className="badge bg-slate-100 text-slate-500 text-xs">{fpCases.length} 个用例</span>
                  </button>
                  {isExpanded && (
                    <div className="divide-y divide-slate-50">
                      {fpCases.map(c => {
                        const caseExpanded = expandedCases.has(c.id)
                        return (
                          <div key={c.id}>
                            <div className="flex items-center px-5 py-3 hover:bg-slate-50/30 transition-colors cursor-pointer" onClick={() => toggleCase(c.id)}>
                              <div className="flex-1 flex items-center gap-3 min-w-0">
                                {caseExpanded ? <ChevronDown className="w-3.5 h-3.5 text-slate-400 shrink-0" /> : <ChevronRight className="w-3.5 h-3.5 text-slate-400 shrink-0" />}
                                <span className="text-xs font-mono text-slate-400 shrink-0">{c.id}</span>
                                <span className="text-sm font-medium text-slate-700 truncate">{c.title}</span>
                              </div>
                              <div className="flex items-center gap-2 shrink-0 ml-3">
                                <span className="badge bg-blue-50 text-blue-600 text-xs">{caseTypeLabel(c.type)}</span>
                                <span className={priorityBadge(c.priority)}>{c.priority}</span>
                                <span className={statusBadge(c.status)}>{CASE_STATUS_LABELS[c.status] || c.status}</span>
                                <span className={`text-xs font-mono font-medium ${confidenceColor(c.confidence)}`}>{(c.confidence * 100).toFixed(0)}%</span>
                                <button onClick={e => { e.stopPropagation(); handleDelete(c.id) }} className="btn-ghost p-1 text-slate-400 hover:text-red-500" title="删除"><Trash2 className="w-3.5 h-3.5" /></button>
                              </div>
                            </div>
                            {caseExpanded && (
                              <div className="px-5 pb-4 pl-14 space-y-3 bg-slate-50/20">
                                {c.description && <div><p className="text-xs font-semibold text-slate-500 mb-1 flex items-center gap-1"><FileText className="w-3 h-3" /> 描述</p><p className="text-sm text-slate-600">{c.description}</p></div>}
                                {c.preconditions && c.preconditions.length > 0 && <div><p className="text-xs font-semibold text-slate-500 mb-1 flex items-center gap-1"><AlertTriangle className="w-3 h-3" /> 前置条件</p><ul className="list-disc list-inside space-y-0.5">{c.preconditions.map((p, i) => <li key={i} className="text-sm text-slate-600">{p}</li>)}</ul></div>}
                                {c.steps && c.steps.length > 0 && <div><p className="text-xs font-semibold text-slate-500 mb-1 flex items-center gap-1"><ListChecks className="w-3 h-3" /> 测试步骤</p><table className="w-full text-sm"><thead><tr className="text-left text-xs text-slate-400 border-b border-slate-100"><th className="py-1 w-12">#</th><th className="py-1">操作</th><th className="py-1">预期</th></tr></thead><tbody>{c.steps.map((s, i) => <tr key={i} className="border-b border-slate-50"><td className="py-1.5 text-slate-400">{s.step || i + 1}</td><td className="py-1.5 text-slate-700">{s.action}</td><td className="py-1.5 text-slate-500">{s.expected}</td></tr>)}</tbody></table></div>}
                              </div>
                            )}
                          </div>
                        )
                      })}
                    </div>
                  )}
                </div>
              )
            })}
            <div className="flex items-center justify-between px-5 py-3 border-t border-slate-100 bg-slate-50/30">
              <span className="text-xs text-slate-400">共 {total} 条，每页 {PAGE_SIZE} 条</span>
              <div className="flex items-center gap-2">
                <button disabled={page <= 1} onClick={() => setPage(1)} className="btn-ghost text-xs py-1 disabled:opacity-30">« 首页</button>
                <button disabled={page <= 1} onClick={() => setPage(page - 1)} className="btn-ghost text-xs py-1 disabled:opacity-30">‹ 上页</button>
                <span className="text-xs text-slate-500 px-2">{page}/{totalPages || 1}</span>
                <button disabled={page >= totalPages} onClick={() => setPage(page + 1)} className="btn-ghost text-xs py-1 disabled:opacity-30">下页 ›</button>
                <button disabled={page >= totalPages} onClick={() => setPage(totalPages)} className="btn-ghost text-xs py-1 disabled:opacity-30">末页 »</button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
