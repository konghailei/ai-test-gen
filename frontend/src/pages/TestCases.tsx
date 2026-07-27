import { useState, useEffect, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import { TestTube, Trash2, Loader2, Play, Sparkles, Filter, Download, ChevronDown, ChevronRight, ListChecks, AlertTriangle, FileText, Search, X, Network, List, CheckCircle, XCircle, Edit3 } from 'lucide-react'
import { testCases as tcApi, requirements as reqApi, generateCases, exportCases, products as prodApi } from '../api/client'
import { useProduct } from '../store/ProductContext'
import type { TestCase, Requirement, Project, SSEEvent } from '../types'
import { formatDate, priorityBadge, statusBadge, confidenceColor, CASE_STATUS_LABELS } from '../utils'
import { caseTypeLabel } from '../utils/labels'
import CaseEditDialog from '../components/CaseEditDialog'
import XMindEditor from '../components/XMindEditor'

const PAGE_SIZE = 20
type ViewMode = 'list' | 'xmind'

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
  const [xmindFormat, setXmindFormat] = useState<'xmind' | 'xmind8'>('xmind')

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

  // 视图模式：list / xmind
  const [viewMode, setViewMode] = useState<ViewMode>('list')
  // XMind 视图下加载全部用例（不分页）
  const [allCases, setAllCases] = useState<TestCase[]>([])
  const [allCasesLoading, setAllCasesLoading] = useState(false)
  // 编辑对话框
  const [editingCase, setEditingCase] = useState<TestCase | null>(null)

  // 加载需求（先按产品过滤，无结果回退全部）
  const [searchParams, setSearchParams] = useSearchParams()
  // 从 URL query 读取 project_id（支持从项目管理页面跳转过来）
  useEffect(() => {
    const qProj = searchParams.get('project_id') || ''
    if (qProj) {
      setFilterProject(qProj)
      // 清掉 query 参数，避免后续刷新重复带上
      setSearchParams({}, { replace: true })
    }
  }, [])

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

  // 切换到 XMind 视图时加载全部用例
  useEffect(() => {
    if (viewMode === 'xmind') loadAllCases()
  }, [viewMode, filterReq, filterProject, filterSearch, selectedProductId])

  const loadAllCases = async () => {
    setAllCasesLoading(true)
    try {
      // 后端限制 page_size <= 100，循环分页加载全部用例
      const all: TestCase[] = []
      let p = 1
      const pageSize = 100
      // 最多取 50 页 = 5000 条，避免无限循环
      while (p <= 50) {
        const r = await tcApi.list(
          filterReq || undefined,
          filterProject || undefined,
          filterSearch || undefined,
          p, pageSize,
          selectedProductId || undefined,
        )
        const items = r.items || []
        all.push(...items)
        if (items.length < pageSize || all.length >= (r.total || 0)) break
        p++
      }
      setAllCases(all)
    } catch { setAllCases([]) }
    finally { setAllCasesLoading(false) }
  }

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

  // 删除用例（列表视图 + XMind 视图同步）
  const handleDelete = async (caseId: string) => {
    if (!confirm('确认删除此用例？此操作不可撤销。')) return
    try {
      await tcApi.delete(caseId)
      // 列表视图刷新
      loadCases()
      // XMind 视图本地同步移除
      setAllCases(prev => prev.filter(c => c.id !== caseId))
    } catch (e: any) { alert('删除失败: ' + e.message) }
  }

  // 编辑用例：保存后两个视图同步更新
  const handleEditSave = (updated: TestCase) => {
    setEditingCase(null)
    // XMind 视图本地同步
    setAllCases(prev => prev.map(c => c.id === updated.id ? updated : c))
    // 列表视图刷新
    loadCases()
  }

  // 审核通过
  const handleApprove = async (caseId: string) => {
    try {
      await tcApi.feedback(caseId, true, '审核通过')
      setAllCases(prev => prev.map(c => c.id === caseId ? { ...c, status: 'approved' } : c))
      loadCases()
    } catch (e: any) { alert('审核失败: ' + e.message) }
  }

  // 审核驳回
  const handleReject = async (caseId: string) => {
    const reason = prompt('请输入驳回原因（可选）：', '') ?? ''
    try {
      await tcApi.feedback(caseId, false, reason || '审核驳回')
      setAllCases(prev => prev.map(c => c.id === caseId ? { ...c, status: 'rejected' } : c))
      loadCases()
    } catch (e: any) { alert('驳回失败: ' + e.message) }
  }

  // 新增用例：列表视图下提示切换需求；XMind 视图下直接打开空表单（如果未选需求则提示）
  const handleAddNew = () => {
    if (!filterReq) {
      alert('请先在筛选栏选择一个需求，新增用例需关联到具体需求')
      return
    }
    const newCase: TestCase = {
      id: '',  // 后端创建时生成（当前后端无 POST 端点，此处用编辑模式打开已有用例的快捷方式提示）
      req_id: filterReq,
      title: '',
      type: 'functional',
      priority: 'P1',
      status: 'draft',
      confidence: 0,
      functional_point: '',
      description: '',
      preconditions: [],
      steps: [{ step: 1, action: '', expected: '' }],
      created_at: new Date().toISOString(),
    }
    alert('暂不支持直接新增用例，请通过「生成用例」按钮由 AI 生成，或在已有用例上点击编辑按钮进行修改')
    void newCase
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
      const blob = await exportCases.download(xmindFormat, cases.map(c => c.id), selectedProject?.name || filterProject)
      const url = URL.createObjectURL(blob)
      const suffix = xmindFormat === 'xmind8' ? '-xmind8' : ''
      const a = document.createElement('a'); a.href = url; a.download = `testcases-${selectedProject?.name || filterProject}${suffix}.xmind`; a.click()
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
          <p className="text-sm mt-1" style={{ color: 'var(--foreground-tertiary)' }}>{total} 个用例，{groups.length} 个功能点</p>
        </div>
        <div className="flex items-center gap-2">
          {/* 视图切换 */}
          <div className="flex items-center p-0.5" style={{ background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.5)' }}>
            <button onClick={() => setViewMode('list')}
              className="px-2.5 py-1.5 text-xs font-medium flex items-center gap-1 transition-colors hover:text-[var(--foreground)]"
              style={{
                borderRadius: 'calc(var(--radius) * 0.4)',
                ...(viewMode === 'list'
                  ? { background: 'var(--background)', color: 'var(--brand-500)', boxShadow: 'var(--shadow-apple-sm)' }
                  : { color: 'var(--foreground-tertiary)' })
              }}>
              <List className="w-3.5 h-3.5" />列表
            </button>
            <button onClick={() => setViewMode('xmind')}
              className="px-2.5 py-1.5 text-xs font-medium flex items-center gap-1 transition-colors hover:text-[var(--foreground)]"
              style={{
                borderRadius: 'calc(var(--radius) * 0.4)',
                ...(viewMode === 'xmind'
                  ? { background: 'var(--background)', color: 'var(--brand-500)', boxShadow: 'var(--shadow-apple-sm)' }
                  : { color: 'var(--foreground-tertiary)' })
              }}>
              <Network className="w-3.5 h-3.5" />XMind 编辑器
            </button>
          </div>
          <button onClick={() => setShowGenerate(!showGenerate)} className="btn-primary flex items-center gap-2">
            <Sparkles className="w-4 h-4" />{showGenerate ? '收起' : '生成用例'}
          </button>
          {/* 当前筛选了需求时，显示快捷重新生成 */}
          {filterReq && (
            <button onClick={() => { setShowGenerate(true); handleGenerate(filterReq) }} disabled={generating}
              className="btn-secondary flex items-center gap-2"
              style={{ color: 'var(--brand-500)', borderColor: 'rgba(0, 122, 255, 0.2)' }}>
              {generating ? <Loader2 className="w-4 h-4 animate-spin" /> : <Sparkles className="w-4 h-4" />}
              重新生成
            </button>
          )}
          {viewMode === 'list' && (
            <button onClick={handleBatchDelete} disabled={batchDeleting || total === 0}
              className="btn-secondary flex items-center gap-2 disabled:opacity-50"
              style={{ color: 'var(--state-error)' }}>
              {batchDeleting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Trash2 className="w-4 h-4" />}
              批量删除
            </button>
          )}
          <div className="flex items-center">
            <button onClick={handleExportXMind} disabled={exporting || !filterProject || cases.length === 0}
              className="btn-secondary flex items-center gap-2 disabled:opacity-50 rounded-r-none border-r-0">
              {exporting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
              {xmindFormat === 'xmind8' ? '导出 XMind 8' : '导出 XMind'}
            </button>
            <div className="relative group">
              <button disabled={exporting || !filterProject || cases.length === 0}
                className="btn-secondary rounded-l-none px-3 disabled:opacity-50">
                <ChevronDown className="w-4 h-4" />
              </button>
              <div className="absolute right-0 top-full mt-1 z-20 hidden group-hover:block min-w-[160px]" style={{ background: 'var(--background)', border: '1px solid var(--border)', borderRadius: 'calc(var(--radius) * 0.5)', boxShadow: 'var(--shadow-apple-lg)' }}>
                <button onClick={() => setXmindFormat('xmind')}
                  className={`w-full text-left px-3 py-2 text-xs hover:bg-[var(--background-muted)] ${xmindFormat === 'xmind' ? 'font-medium' : ''}`}
                  style={xmindFormat === 'xmind' ? { background: 'rgba(0, 122, 255, 0.08)', color: 'var(--brand-500)' } : { color: 'var(--foreground-secondary)' }}>
                  XMind 2020+ / Zen（JSON）
                </button>
                <button onClick={() => setXmindFormat('xmind8')}
                  className={`w-full text-left px-3 py-2 text-xs hover:bg-[var(--background-muted)] ${xmindFormat === 'xmind8' ? 'font-medium' : ''}`}
                  style={xmindFormat === 'xmind8' ? { background: 'rgba(0, 122, 255, 0.08)', color: 'var(--brand-500)' } : { color: 'var(--foreground-secondary)' }}>
                  XMind 8（XML）
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* 生成面板 */}
      {showGenerate && (
        <div className="card p-5 space-y-4">
          <h3 className="text-sm font-semibold flex items-center gap-2" style={{ color: 'var(--foreground)' }}><Play className="w-4 h-4" style={{ color: 'var(--brand-500)' }} /> 生成测试用例</h3>
          <div className="flex items-end gap-3">
            <div className="flex-1 relative" ref={genReqDropdownRef}>
              <div className="flex items-center" style={{ background: 'var(--background)', border: '1px solid var(--border)', borderRadius: 'calc(var(--radius) * 0.5)' }}>
                <Search className="w-3.5 h-3.5 ml-2.5 shrink-0" style={{ color: 'var(--foreground-tertiary)' }} />
                <input value={genReqSearch} onChange={e => { setGenReqSearch(e.target.value); setShowGenReqDropdown(true) }}
                  onFocus={() => setShowGenReqDropdown(true)}
                  placeholder={genReqId ? `${genReqId} ${reqs.find(r => r.id === genReqId)?.title || ''}` : "搜索需求 ID 或标题..."}
                  className="w-full px-2.5 py-2 text-sm outline-none bg-transparent" disabled={generating} />
                {genReqId && (
                  <button onClick={() => { setGenReqId(''); setGenReqSearch(''); }}
                    className="px-2 hover:text-[var(--foreground-secondary)]" style={{ color: 'var(--foreground-tertiary)' }}><X className="w-3.5 h-3.5" /></button>
                )}
              </div>
              {showGenReqDropdown && (
                <div className="absolute top-full left-0 mt-1 w-full py-1 z-40 max-h-48 overflow-y-auto" style={{ background: 'var(--background)', border: '1px solid var(--border)', borderRadius: 'calc(var(--radius) * 0.5)', boxShadow: 'var(--shadow-apple-lg)' }}>
                  {genReqFiltered.length === 0 ? (
                    <p className="text-xs px-3 py-2" style={{ color: 'var(--foreground-tertiary)' }}>
                      {reqs.length === 0 ? '暂未加载需求列表，请检查产品选择' : '无匹配需求'}
                    </p>
                  ) : (
                    genReqFiltered.slice(0, 50).map(r => (
                      <button key={r.id} onClick={() => { setGenReqId(r.id); setGenReqSearch(''); setShowGenReqDropdown(false) }}
                        className={`w-full text-left px-3 py-2 text-sm hover:bg-[var(--background-muted)] ${genReqId === r.id ? 'font-medium' : ''}`}
                        style={genReqId === r.id ? { background: 'rgba(0, 122, 255, 0.08)', color: 'var(--brand-500)' } : { color: 'var(--foreground-secondary)' }}>
                        <span className="font-mono text-xs mr-2" style={{ color: 'var(--foreground-tertiary)' }}>{r.id}</span>{r.title}
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
              <div className="flex justify-between text-xs mb-1" style={{ color: 'var(--foreground-tertiary)' }}><span>{genMessage}</span><span>{genProgress}%</span></div>
              <div className="h-2 rounded-full" style={{ background: 'var(--background-muted)' }}><div className="h-full rounded-full transition-all" style={{ width: `${genProgress}%`, background: genDone ? 'var(--state-success)' : 'var(--brand-500)' }} /></div>
            </div>
          )}
        </div>
      )}

      {/* 筛选栏 */}
      <div className="card py-3 px-5">
        <div className="flex items-center gap-3 flex-wrap">
          <Filter className="w-4 h-4 shrink-0" style={{ color: 'var(--foreground-tertiary)' }} />

          {/* 项目 — 可搜索下拉 */}
          <div className="relative" ref={projectDropdownRef}>
            <div className="flex items-center min-w-[180px]" style={{ background: 'var(--background)', border: '1px solid var(--border)', borderRadius: 'calc(var(--radius) * 0.5)' }}>
              <input value={projectSearch} onChange={e => { setProjectSearch(e.target.value); setShowProjectDropdown(true) }}
                onFocus={() => setShowProjectDropdown(true)}
                placeholder={filterProject === 'unassigned' ? '(未绑定项目)' : (selectedProject?.name || "搜索项目...")}
                className="w-full px-3 py-2 text-sm outline-none bg-transparent" style={{ borderRadius: 'calc(var(--radius) * 0.5)' }} />
              {filterProject && (
                <button onClick={() => { setFilterProject(''); setProjectSearch(''); setPage(1) }}
                  className="px-2 hover:text-[var(--foreground-secondary)]" style={{ color: 'var(--foreground-tertiary)' }}><X className="w-3.5 h-3.5" /></button>
              )}
              <ChevronDown className={`w-3.5 h-3.5 mr-2 transition-transform ${showProjectDropdown ? 'rotate-180' : ''}`} style={{ color: 'var(--foreground-tertiary)' }} />
            </div>
            {showProjectDropdown && (
              <div className="absolute top-full left-0 mt-1 w-full py-1 z-50 max-h-48 overflow-y-auto" style={{ background: 'var(--background)', border: '1px solid var(--border)', borderRadius: 'calc(var(--radius) * 0.5)', boxShadow: 'var(--shadow-apple-lg)' }}>
                {/* 未绑定项目选项 */}
                <button
                  onClick={() => { setFilterProject('unassigned'); setProjectSearch(''); setShowProjectDropdown(false); setFilterReq(''); setPage(1) }}
                  className={`w-full text-left px-3 py-2 text-sm hover:bg-[var(--background-muted)] ${filterProject === 'unassigned' ? 'font-medium italic' : 'italic'}`}
                  style={filterProject === 'unassigned' ? { background: 'rgba(0, 122, 255, 0.08)', color: 'var(--brand-500)' } : { color: 'var(--foreground-tertiary)' }}
                >
                  (未绑定项目)
                </button>
                {filteredProjects.length === 0 ? (
                  <p className="text-xs px-3 py-2" style={{ color: 'var(--foreground-tertiary)' }}>无匹配项目</p>
                ) : (
                  filteredProjects.map(p => (
                    <button key={p.id} onClick={() => { setFilterProject(p.id); setProjectSearch(''); setShowProjectDropdown(false); setFilterReq(''); setPage(1) }}
                      className={`w-full text-left px-3 py-2 text-sm hover:bg-[var(--background-muted)] ${filterProject === p.id ? 'font-medium' : ''}`}
                      style={filterProject === p.id ? { background: 'rgba(0, 122, 255, 0.08)', color: 'var(--brand-500)' } : { color: 'var(--foreground-secondary)' }}>
                      {p.name} <span className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>{p.version ? `v${p.version}` : ''}</span>
                    </button>
                  ))
                )}
              </div>
            )}
          </div>

          {/* 需求 */}
          <div className="flex items-center gap-1.5">
            <span className="text-xs shrink-0" style={{ color: 'var(--foreground-tertiary)' }}>需求</span>
            <select value={filterReq} onChange={e => { setFilterReq(e.target.value); setPage(1) }}
              className="select-field text-sm min-w-[200px]">
              <option value="">全部需求</option>
              {reqs.map(r => <option key={r.id} value={r.id}>{r.title}</option>)}
            </select>
          </div>

          {/* 标题搜索 */}
          <div className="flex items-center min-w-[200px]" style={{ background: 'var(--background)', border: '1px solid var(--border)', borderRadius: 'calc(var(--radius) * 0.5)' }}>
            <Search className="w-3.5 h-3.5 ml-2.5" style={{ color: 'var(--foreground-tertiary)' }} />
            <input value={filterSearch} onChange={e => { setFilterSearch(e.target.value); setPage(1) }}
              onKeyDown={e => e.key === 'Enter' && loadCases()}
              placeholder="搜索用例标题..."
              className="w-full px-2.5 py-2 text-sm outline-none bg-transparent" />
            {filterSearch && (
              <button onClick={() => { setFilterSearch(''); setPage(1) }}
                className="px-2 hover:text-[var(--foreground-secondary)]" style={{ color: 'var(--foreground-tertiary)' }}><X className="w-3.5 h-3.5" /></button>
            )}
          </div>

          {/* 操作 */}
          {(filterReq || filterProject || filterSearch) && (
            <button onClick={() => { setFilterReq(''); setFilterProject(''); setFilterSearch(''); setProjectSearch(''); setPage(1) }}
              className="text-xs hover:underline shrink-0" style={{ color: 'var(--brand-500)' }}>清除全部</button>
          )}
        </div>
      </div>

      {/* 用例列表视图 */}
      {viewMode === 'list' && (
      <div className="card p-0 overflow-hidden">
        {loading ? (
          <div className="p-6 space-y-4">{[1,2,3,4,5].map(i => <div key={i} className="skeleton h-14" />)}</div>
        ) : cases.length === 0 ? (
          <div className="text-center py-16">
            <TestTube className="w-12 h-12 mx-auto mb-3" style={{ color: 'var(--foreground-quaternary)' }} />
            <p className="font-medium" style={{ color: 'var(--foreground-tertiary)' }}>暂无用例</p>
            <p className="text-sm mt-1" style={{ color: 'var(--foreground-tertiary)' }}>选择需求并点击「生成用例」</p>
          </div>
        ) : (
          <div>
            {groups.map(([fpKey, fpCases]) => {
              const isExpanded = expandedGroups.has(fpKey)
              const reqLabel = reqs.find(r => r.id === fpCases[0]?.req_id)?.title || fpCases[0]?.req_id || ''
              const headerLabel = fpCases[0]?.functional_point ? fpKey : `${fpKey} ${reqLabel}`
              return (
                <div key={fpKey} style={{ borderBottom: '1px solid var(--border)' }} className="last:border-b-0">
                  <button onClick={() => toggleGroup(fpKey)}
                    className="w-full flex items-center gap-2 px-5 py-3 hover:bg-[var(--background-muted)] transition-colors text-left"
                    style={{ background: 'rgba(242, 242, 247, 0.5)' }}>
                    {isExpanded ? <ChevronDown className="w-4 h-4" style={{ color: 'var(--foreground-tertiary)' }} /> : <ChevronRight className="w-4 h-4" style={{ color: 'var(--foreground-tertiary)' }} />}
                    <FileText className="w-4 h-4" style={{ color: 'var(--brand-400)' }} />
                    <span className="text-sm font-semibold" style={{ color: 'var(--foreground)' }}>{headerLabel}</span>
                    <span className="badge text-xs" style={{ background: 'var(--background-muted)', color: 'var(--foreground-tertiary)' }}>{fpCases.length} 个用例</span>
                  </button>
                  {isExpanded && (
                    <div>
                      {fpCases.map(c => {
                        const caseExpanded = expandedCases.has(c.id)
                        return (
                          <div key={c.id} style={{ borderTop: '1px solid rgba(242, 242, 247, 0.7)' }} className="first:border-t-0">
                            <div className="flex items-center px-5 py-3 hover:bg-[rgba(242,242,247,0.4)] transition-colors cursor-pointer" onClick={() => toggleCase(c.id)}>
                              <div className="flex-1 flex items-center gap-3 min-w-0">
                                {caseExpanded ? <ChevronDown className="w-3.5 h-3.5 shrink-0" style={{ color: 'var(--foreground-tertiary)' }} /> : <ChevronRight className="w-3.5 h-3.5 shrink-0" style={{ color: 'var(--foreground-tertiary)' }} />}
                                <span className="text-xs font-mono shrink-0" style={{ color: 'var(--foreground-tertiary)' }}>{c.id}</span>
                                <span className="text-sm font-medium truncate" style={{ color: 'var(--foreground)' }}>{c.title}</span>
                              </div>
                              <div className="flex items-center gap-2 shrink-0 ml-3">
                                <span className="badge text-xs" style={{ background: 'rgba(0, 122, 255, 0.08)', color: 'var(--brand-500)' }}>{caseTypeLabel(c.type)}</span>
                                <span className={priorityBadge(c.priority)}>{c.priority}</span>
                                <span className={statusBadge(c.status)}>{CASE_STATUS_LABELS[c.status] || c.status}</span>
                                <span className={`text-xs font-mono font-medium ${confidenceColor(c.confidence)}`}>{(c.confidence * 100).toFixed(0)}%</span>
                                {/* 审核按钮 */}
                                {c.status !== 'approved' && (
                                  <button onClick={e => { e.stopPropagation(); handleApprove(c.id) }} className="btn-ghost p-1 hover:text-[var(--state-success)]" style={{ color: 'var(--foreground-tertiary)' }} title="审核通过">
                                    <CheckCircle className="w-3.5 h-3.5" />
                                  </button>
                                )}
                                {c.status !== 'rejected' && (
                                  <button onClick={e => { e.stopPropagation(); handleReject(c.id) }} className="btn-ghost p-1 hover:text-[var(--state-error)]" style={{ color: 'var(--foreground-tertiary)' }} title="驳回">
                                    <XCircle className="w-3.5 h-3.5" />
                                  </button>
                                )}
                                {/* 编辑按钮 */}
                                <button onClick={e => { e.stopPropagation(); setEditingCase(c) }} className="btn-ghost p-1 hover:text-[var(--brand-500)]" style={{ color: 'var(--foreground-tertiary)' }} title="编辑">
                                  <Edit3 className="w-3.5 h-3.5" />
                                </button>
                                <button onClick={e => { e.stopPropagation(); handleDelete(c.id) }} className="btn-ghost p-1 hover:text-[var(--state-error)]" style={{ color: 'var(--foreground-tertiary)' }} title="删除"><Trash2 className="w-3.5 h-3.5" /></button>
                              </div>
                            </div>
                            {caseExpanded && (
                              <div className="px-5 pb-4 pl-14 space-y-3" style={{ background: 'rgba(242, 242, 247, 0.25)' }}>
                                {c.description && <div><p className="text-xs font-semibold mb-1 flex items-center gap-1" style={{ color: 'var(--foreground-tertiary)' }}><FileText className="w-3 h-3" /> 描述</p><p className="text-sm" style={{ color: 'var(--foreground-secondary)' }}>{c.description}</p></div>}
                                {c.preconditions && c.preconditions.length > 0 && <div><p className="text-xs font-semibold mb-1 flex items-center gap-1" style={{ color: 'var(--foreground-tertiary)' }}><AlertTriangle className="w-3 h-3" /> 前置条件</p><ul className="list-disc list-inside space-y-0.5">{c.preconditions.map((p, i) => <li key={i} className="text-sm" style={{ color: 'var(--foreground-secondary)' }}>{p}</li>)}</ul></div>}
                                {c.steps && c.steps.length > 0 && <div><p className="text-xs font-semibold mb-1 flex items-center gap-1" style={{ color: 'var(--foreground-tertiary)' }}><ListChecks className="w-3 h-3" /> 测试步骤</p><table className="w-full text-sm"><thead><tr className="text-left text-xs" style={{ color: 'var(--foreground-tertiary)', borderBottom: '1px solid var(--border)' }}><th className="py-1 w-12">#</th><th className="py-1">操作</th><th className="py-1">预期</th></tr></thead><tbody>{c.steps.map((s, i) => <tr key={i} style={{ borderBottom: '1px solid rgba(242, 242, 247, 0.7)' }}><td className="py-1.5" style={{ color: 'var(--foreground-tertiary)' }}>{s.step || i + 1}</td><td className="py-1.5" style={{ color: 'var(--foreground)' }}>{s.action}</td><td className="py-1.5" style={{ color: 'var(--foreground-tertiary)' }}>{s.expected}</td></tr>)}</tbody></table></div>}
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
            <div className="flex items-center justify-between px-5 py-3" style={{ borderTop: '1px solid var(--border)', background: 'rgba(242, 242, 247, 0.4)' }}>
              <span className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>共 {total} 条，每页 {PAGE_SIZE} 条</span>
              <div className="flex items-center gap-2">
                <button disabled={page <= 1} onClick={() => setPage(1)} className="btn-ghost text-xs py-1 disabled:opacity-30">« 首页</button>
                <button disabled={page <= 1} onClick={() => setPage(page - 1)} className="btn-ghost text-xs py-1 disabled:opacity-30">‹ 上页</button>
                <span className="text-xs px-2" style={{ color: 'var(--foreground-tertiary)' }}>{page}/{totalPages || 1}</span>
                <button disabled={page >= totalPages} onClick={() => setPage(page + 1)} className="btn-ghost text-xs py-1 disabled:opacity-30">下页 ›</button>
                <button disabled={page >= totalPages} onClick={() => setPage(totalPages)} className="btn-ghost text-xs py-1 disabled:opacity-30">末页 »</button>
              </div>
            </div>
          </div>
        )}
      </div>
      )}

      {/* XMind 编辑器视图 */}
      {viewMode === 'xmind' && (
        <>
          {allCasesLoading ? (
            <div className="card p-6 space-y-4">{[1,2,3,4,5].map(i => <div key={i} className="skeleton h-14" />)}</div>
          ) : (
            <XMindEditor
              cases={allCases}
              reqs={reqs}
              selectedProjectName={selectedProject?.name || filterProject || '所有用例'}
              onEdit={(c) => setEditingCase(c)}
              onDelete={handleDelete}
              onApprove={handleApprove}
              onReject={handleReject}
              onAddNew={handleAddNew}
            />
          )}
        </>
      )}

      {/* 编辑用例对话框 */}
      {editingCase && (
        <CaseEditDialog
          caseData={editingCase}
          onClose={() => setEditingCase(null)}
          onSaved={handleEditSave}
        />
      )}
    </div>
  )
}
