import { useState, useEffect, useRef } from 'react'
import { useSearchParams, useNavigate } from 'react-router-dom'
import { TestTube, Loader2, Filter, Download, ChevronDown, FileText, Search, X, CheckCircle2, Network, AlertTriangle } from 'lucide-react'
import { testCases as tcApi, requirements as reqApi, exportCases, products as prodApi } from '../api/client'
import { useProduct } from '../store/ProductContext'
import type { TestCase, Requirement, Project } from '../types'
import CaseEditDialog from '../components/CaseEditDialog'
import XMindEditor from '../components/XMindEditor'
import { subscribeCasesChanged } from '../utils/caseSync'

/**
 * 测试用例页（树形目录视图，XMind 编辑器模式）
 * - 必选项目后才展示信息（未选项目显示引导提示）
 * - 「生成用例」跳转到测试用例生成页 /generate（选中需求则带需求进入独立生成页）
 * - 「打开 XMind」在新标签页打开真实 XMind 网页编辑器 /testcases/xmind-edit
 */

export interface TestCasesProps {
  /** 锁定项目（项目详情页嵌入时传入，隐藏项目筛选器） */
  fixedProjectId?: string
  /** 嵌入模式：隐藏页面大标题（父页面已有标题），保留操作按钮 */
  embedded?: boolean
}

export default function TestCases({ fixedProjectId, embedded = false }: TestCasesProps = {}) {
  const { selectedProductId } = useProduct()
  const [cases, setCases] = useState<TestCase[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [truncated, setTruncated] = useState(false)  // P2: 全量加载触及 5000 上限时提示

  // 筛选
  const [filterProject, setFilterProject] = useState(fixedProjectId || '')
  const [filterReq, setFilterReq] = useState('')
  const [filterSearch, setFilterSearch] = useState('')
  const [reqs, setReqs] = useState<Requirement[]>([])
  const reqsRef = useRef<Requirement[]>([])
  useEffect(() => { reqsRef.current = reqs }, [reqs])
  const [projects, setProjects] = useState<Project[]>([])
  const [projectSearch, setProjectSearch] = useState('')
  const [showProjectDropdown, setShowProjectDropdown] = useState(false)
  const projectDropdownRef = useRef<HTMLDivElement>(null)
  const selectedProject = projects.find(p => p.id === filterProject)

  // 导出
  const [exporting, setExporting] = useState(false)
  const [xmindFormat, setXmindFormat] = useState<'xmind' | 'xmind8'>('xmind')
  const [exportDetails, setExportDetails] = useState(true)

  // 编辑对话框
  const [editingCase, setEditingCase] = useState<TestCase | null>(null)
  const navigate = useNavigate()

  // 从 URL query 读取 project_id（支持从项目管理页面跳转过来）
  const [searchParams, setSearchParams] = useSearchParams()
  useEffect(() => {
    if (fixedProjectId) return  // v3.2.8: 项目详情页已锁定项目，不读 URL
    const qProj = searchParams.get('project_id') || ''
    if (qProj) {
      setFilterProject(qProj)
      setSearchParams({}, { replace: true })
    }
  }, [])

  // v3.2.8: 嵌入模式下 fixedProjectId 变化时同步筛选
  useEffect(() => {
    if (fixedProjectId) setFilterProject(fixedProjectId)
  }, [fixedProjectId])

  // 加载需求 + 项目（先按产品过滤，无结果回退全部）
  useEffect(() => {
    const pid = selectedProductId || undefined
    const load = async () => {
      try {
        let r = await reqApi.list(1, 300, pid, filterProject || undefined)
        setReqs(r.items || [])
        if ((!r.items || r.items.length === 0) && pid) {
          r = await reqApi.list(1, 300, undefined, undefined)
          setReqs(r.items || [])
        }
      } catch { setReqs([]) }
      pid ? prodApi.listProjects(pid).then((r: any) => setProjects(Array.isArray(r) ? r : [])).catch(() => {}) : Promise.resolve()
    }
    load()
  }, [selectedProductId, filterProject])

  // 加载全部用例（树形视图与导出共用全量）
  const loadCases = async () => {
    setLoading(true)
    try {
      const all: TestCase[] = []
      let p = 1
      const pageSize = 100
      let serverTotal = 0
      while (p <= 50) {
        const r = await tcApi.list(
          filterProject === 'unassigned' ? undefined : (filterReq || undefined),
          filterProject || undefined,
          filterSearch || undefined,
          p, pageSize,
          selectedProductId || undefined,
        )
        const items = r.items || []
        serverTotal = r.total || 0
        all.push(...items)
        if (items.length < pageSize || all.length >= serverTotal) break
        p++
      }
      // P2: 上限 50 页 × 100 = 5000 条，超出时明确提示而非静默截断
      setTruncated(all.length < serverTotal)
      setCases(all)
      setTotal(all.length)

      // 补充加载缺失需求信息
      const knownReqIds = new Set(reqsRef.current.map(r => r.id))
      const missingReqIds = Array.from(new Set(all.map(c => c.req_id).filter(id => id && !knownReqIds.has(id))))
      if (missingReqIds.length > 0) {
        try {
          const fetched = await Promise.all(
            missingReqIds.slice(0, 50).map(id =>
              reqApi.get(id).then((d: any) => ({ id: d.id, title: d.title, project_id: d.project_id } as Requirement)).catch(() => null)
            )
          )
          const found = fetched.filter((r: any): r is Requirement => r !== null)
          if (found.length > 0) {
            setReqs(prev => {
              const existing = new Set(prev.map(r => r.id))
              const toAdd = found.filter(r => !existing.has(r.id))
              return toAdd.length > 0 ? [...prev, ...toAdd] : prev
            })
          }
        } catch { /* 不阻断 */ }
      }
    } catch { setCases([]) }
    finally { setLoading(false) }
  }

  useEffect(() => { loadCases() }, [filterReq, filterProject, filterSearch, selectedProductId])

  // XMind 编辑页通常在另一个标签页，保存后这里也要跟着刷新（跨标签页通知）
  const loadCasesRef = useRef(loadCases)
  useEffect(() => { loadCasesRef.current = loadCases })
  useEffect(() => subscribeCasesChanged(() => loadCasesRef.current()), [])

  // 关闭下拉（点击外部）
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (projectDropdownRef.current && !projectDropdownRef.current.contains(e.target as Node))
        setShowProjectDropdown(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  // 删除用例
  const handleDelete = async (caseId: string) => {
    if (!confirm('确认删除此用例？此操作不可撤销。')) return
    try { await tcApi.delete(caseId); loadCases() }
    catch (e: any) { alert('删除失败: ' + e.message) }
  }

  const handleEditSave = () => { setEditingCase(null); loadCases() }

  const handleApprove = async (caseId: string) => {
    try { await tcApi.feedback(caseId, true, '审核通过'); loadCases() }
    catch (e: any) { alert('审核失败: ' + e.message) }
  }

  const handleReject = async (caseId: string) => {
    const reason = prompt('请输入驳回原因（可选）：', '') ?? ''
    try { await tcApi.feedback(caseId, false, reason || '审核驳回'); loadCases() }
    catch (e: any) { alert('驳回失败: ' + e.message) }
  }

  // 新增用例：必须先选择需求
  const handleAddNew = () => {
    if (!filterReq) { alert('请先在筛选栏选择一个需求，新增用例需关联到具体需求'); return }
    setEditingCase({
      id: '',
      req_id: filterReq,
      product_id: selectedProductId || undefined,
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
    })
  }

  const handleAddCaseAtLevel = (reqId: string, fp: string) => {
    setEditingCase({
      id: '',
      req_id: reqId,
      product_id: selectedProductId || undefined,
      title: '',
      type: 'functional',
      priority: 'P1',
      status: 'draft',
      confidence: 0,
      functional_point: fp,
      description: '',
      preconditions: [],
      steps: [{ step: 1, action: '', expected: '' }],
      created_at: new Date().toISOString(),
    })
  }

  const handleMoveCase = async (caseId: string, newReqId: string, newFp: string) => {
    const updateData: Record<string, unknown> = { req_id: newReqId }
    if (newFp !== undefined) updateData.functional_point = newFp
    await tcApi.update(caseId, updateData)
    loadCases()
  }

  const handleBatchApprove = async (caseIds: string[], accepted: boolean) => {
    await tcApi.batchFeedback(caseIds, accepted)
    loadCases()
  }

  // 导出
  const handleExportXMind = async () => {
    if (cases.length === 0) { alert('当前筛选条件下没有可导出的用例'); return }
    setExporting(true)
    try {
      const exportName = selectedProject?.name || filterProject || filterReq || '所有用例'
      const blob = await exportCases.download(xmindFormat, cases.map(c => c.id), exportName, exportDetails)
      const url = URL.createObjectURL(blob)
      const suffix = xmindFormat === 'xmind8' ? '-xmind8' : ''
      const a = document.createElement('a'); a.href = url; a.download = `testcases-${exportName}${suffix}.xmind`; a.click()
      URL.revokeObjectURL(url)
    } catch (e: any) { alert('导出失败: ' + e.message) }
    finally { setExporting(false) }
  }

  // 生成 → 跳转到测试用例生成页
  const handleGenerate = () => {
    if (filterReq) navigate(`/requirements/${encodeURIComponent(filterReq)}/generate`)
    else navigate('/generate')
  }

  // 打开真实 XMind 网页编辑器（新标签页）
  const openXMindEditor = () => {
    if (!filterProject) { alert('请先在筛选栏选择一个项目，再打开 XMind 编辑'); return }
    const params = new URLSearchParams({ project_id: filterProject })
    if (selectedProductId) params.set('product_id', selectedProductId)
    window.open(`/testcases/xmind-edit?${params.toString()}`, '_blank')
  }

  const filteredProjects = projects.filter(p =>
    !projectSearch || p.name.toLowerCase().includes(projectSearch.toLowerCase()) || p.id.includes(projectSearch)
  )

  // 未选项目时不展示用例信息
  const noProject = !filterProject

  return (
    <div className="space-y-6">
      {/* 标题栏 */}
      <div className={`flex items-center ${embedded ? 'justify-end' : 'justify-between'}`}>
        {!embedded && (
          <div>
            <h1 className="page-title">测试用例</h1>
            <p className="text-sm mt-1" style={{ color: 'var(--foreground-tertiary)' }}>
              {noProject ? '请先选择一个项目，再查看该项目的测试用例' : `${total} 个用例`}
            </p>
          </div>
        )}
        <div className="flex items-center gap-2">
          <button onClick={handleGenerate} className="btn-primary flex items-center gap-2" disabled={noProject}>
            <FileText className="w-4 h-4" />生成用例
          </button>
          <button onClick={openXMindEditor} className="btn-secondary flex items-center gap-2 disabled:opacity-50"
            disabled={noProject || cases.length === 0} title="在新标签页打开真实 XMind 网页编辑器">
            <Network className="w-4 h-4" />打开 XMind
          </button>
          {/* 导出 XMind */}
          <div className="relative group">
            <button onClick={handleExportXMind} disabled={exporting || cases.length === 0}
              className="btn-secondary flex items-center gap-2 disabled:opacity-50" title="导出 XMind 思维导图">
              {exporting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
              <span>{xmindFormat === 'xmind8' ? '导出 XMind 8' : '导出 XMind'}</span>
              <ChevronDown className="w-3 h-3 opacity-60" />
            </button>
            <div className="absolute right-0 top-full mt-1 z-20 hidden group-hover:block"
              style={{ background: 'var(--background)', border: '1px solid var(--border)', borderRadius: 'calc(var(--radius) * 0.5)', boxShadow: 'var(--shadow-apple-lg)', width: 'max-content', minWidth: '200px' }}>
              <button onClick={() => setXmindFormat('xmind')}
                className="w-full text-left px-3 py-2 text-xs hover:bg-[var(--background-muted)] transition-colors"
                style={xmindFormat === 'xmind' ? { background: 'rgba(0, 122, 255, 0.08)', color: 'var(--brand-500)', fontWeight: 500 } : { color: 'var(--foreground-secondary)' }}>
                <span>XMind 2020+ / Zen（JSON）</span>
              </button>
              <button onClick={() => setXmindFormat('xmind8')}
                className="w-full text-left px-3 py-2 text-xs hover:bg-[var(--background-muted)] transition-colors"
                style={xmindFormat === 'xmind8' ? { background: 'rgba(0, 122, 255, 0.08)', color: 'var(--brand-500)', fontWeight: 500 } : { color: 'var(--foreground-secondary)' }}>
                <span>XMind 8（XML）</span>
              </button>
              <div style={{ borderTop: '1px solid var(--border)', margin: '4px 0' }} />
              <label className="flex items-center gap-2 px-3 py-2 text-xs cursor-pointer hover:bg-[var(--background-muted)] transition-colors" style={{ color: 'var(--foreground-secondary)' }}>
                <input type="checkbox" checked={exportDetails} onChange={e => { e.stopPropagation(); setExportDetails(e.target.checked) }} className="w-3.5 h-3.5" style={{ accentColor: 'var(--brand-500)' }} />
                <span>导出详细步骤</span>
              </label>
            </div>
          </div>
        </div>
      </div>

      {/* 筛选栏 */}
      <div className="card py-3 px-5">
        <div className="flex items-center gap-3 flex-wrap">
          <Filter className="w-4 h-4 shrink-0" style={{ color: 'var(--foreground-tertiary)' }} />

          {/* v3.2.8: 项目详情页嵌入时已锁定项目，隐藏项目筛选器 */}
          {!fixedProjectId && (
          <div className="relative" ref={projectDropdownRef}>
            <div className="flex items-center min-w-[180px]" style={{ background: 'var(--background)', border: '1px solid var(--border)', borderRadius: 'calc(var(--radius) * 0.5)' }}>
              <input value={projectSearch} onChange={e => { setProjectSearch(e.target.value); setShowProjectDropdown(true) }}
                onFocus={() => setShowProjectDropdown(true)}
                placeholder={filterProject === 'unassigned' ? '(未绑定项目)' : (selectedProject?.name || "搜索/选择项目...")}
                className="w-full px-3 py-2 text-sm outline-none bg-transparent" style={{ borderRadius: 'calc(var(--radius) * 0.5)' }} />
              {filterProject && (
                <button onClick={() => { setFilterProject(''); setProjectSearch('') }}
                  className="px-2 hover:text-[var(--foreground-secondary)]" style={{ color: 'var(--foreground-tertiary)' }}><X className="w-3.5 h-3.5" /></button>
              )}
              <ChevronDown className={`w-3.5 h-3.5 mr-2 transition-transform ${showProjectDropdown ? 'rotate-180' : ''}`} style={{ color: 'var(--foreground-tertiary)' }} />
            </div>
            {showProjectDropdown && (
              <div className="absolute top-full left-0 mt-1 w-full py-1 z-50 max-h-48 overflow-y-auto" style={{ background: 'var(--background)', border: '1px solid var(--border)', borderRadius: 'calc(var(--radius) * 0.5)', boxShadow: 'var(--shadow-apple-lg)' }}>
                <button onClick={() => { setFilterProject('unassigned'); setProjectSearch(''); setShowProjectDropdown(false); setFilterReq('') }}
                  className={`w-full text-left px-3 py-2 text-sm hover:bg-[var(--background-muted)] ${filterProject === 'unassigned' ? 'font-medium italic' : 'italic'}`}
                  style={filterProject === 'unassigned' ? { background: 'rgba(0, 122, 255, 0.08)', color: 'var(--brand-500)' } : { color: 'var(--foreground-tertiary)' }}>
                  (未绑定项目)
                </button>
                {filteredProjects.length === 0 ? (
                  <p className="text-xs px-3 py-2" style={{ color: 'var(--foreground-tertiary)' }}>无匹配项目</p>
                ) : (
                  filteredProjects.map(p => (
                    <button key={p.id} onClick={() => { setFilterProject(p.id); setProjectSearch(''); setShowProjectDropdown(false); setFilterReq('') }}
                      className={`w-full text-left px-3 py-2 text-sm hover:bg-[var(--background-muted)] ${filterProject === p.id ? 'font-medium' : ''}`}
                      style={filterProject === p.id ? { background: 'rgba(0, 122, 255, 0.08)', color: 'var(--brand-500)' } : { color: 'var(--foreground-secondary)' }}>
                      {p.name} <span className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>{p.version ? `v${p.version}` : ''}</span>
                    </button>
                  ))
                )}
              </div>
            )}
          </div>
          )}

          {/* 需求 */}
          <div className="flex items-center gap-1.5">
            <span className="text-xs shrink-0" style={{ color: 'var(--foreground-tertiary)' }}>需求</span>
            <select value={filterReq} onChange={e => { setFilterReq(e.target.value) }} disabled={noProject}
              className="select-field text-sm min-w-[200px]">
              <option value="">全部需求</option>
              {reqs.map(r => <option key={r.id} value={r.id}>{r.title}</option>)}
            </select>
          </div>

          {/* 标题搜索 */}
          <div className="flex items-center min-w-[200px]" style={{ background: 'var(--background)', border: '1px solid var(--border)', borderRadius: 'calc(var(--radius) * 0.5)' }}>
            <Search className="w-3.5 h-3.5 ml-2.5" style={{ color: 'var(--foreground-tertiary)' }} />
            <input value={filterSearch} onChange={e => { setFilterSearch(e.target.value) }}
              onKeyDown={e => e.key === 'Enter' && loadCases()}
              placeholder="搜索用例标题..." className="w-full px-2.5 py-2 text-sm outline-none bg-transparent" />
            {filterSearch && (
              <button onClick={() => { setFilterSearch('') }}
                className="px-2 hover:text-[var(--foreground-secondary)]" style={{ color: 'var(--foreground-tertiary)' }}><X className="w-3.5 h-3.5" /></button>
            )}
          </div>

          {(filterReq || filterProject || filterSearch) && (
            <button onClick={() => { setFilterReq(''); setFilterProject(''); setFilterSearch(''); setProjectSearch('') }}
              className="text-xs hover:underline shrink-0" style={{ color: 'var(--brand-500)' }}>清除全部</button>
          )}
        </div>
      </div>

      {/* 必选项目引导 */}
      {noProject ? (
        <div className="card text-center py-20">
          <TestTube className="w-12 h-12 mx-auto mb-3" style={{ color: 'var(--foreground-quaternary)' }} />
          <p className="font-medium" style={{ color: 'var(--foreground-secondary)' }}>请先选择项目</p>
          <p className="text-sm mt-1" style={{ color: 'var(--foreground-tertiary)' }}>在上方筛选栏选择一个项目后，才会展示该项目下的测试用例</p>
        </div>
      ) : (
        <>
          {/* P2: 触及 5000 条加载上限时明确提示 */}
          {truncated && (
            <div className="card py-2.5 px-4 flex items-center gap-2 text-sm"
              style={{ background: 'rgba(255, 149, 0, 0.08)', border: '1px solid rgba(255, 149, 0, 0.35)' }}>
              <AlertTriangle className="w-4 h-4 shrink-0" style={{ color: '#ff9500' }} />
              <span style={{ color: 'var(--foreground-secondary)' }}>
                用例总数超过 5000，当前仅加载前 5000 条展示/导出。请在上方筛选（按需求/搜索）缩小范围后查看其余用例。
              </span>
            </div>
          )}
          {loading ? (
            <div className="card p-6 space-y-4">{[1,2,3,4,5].map(i => <div key={i} className="skeleton h-14" />)}</div>
          ) : (
            <XMindEditor
              cases={cases}
              reqs={reqs}
              selectedProjectName={selectedProject?.name || filterProject || '所有用例'}
              onEdit={(c) => setEditingCase(c)}
              onDelete={handleDelete}
              onApprove={handleApprove}
              onReject={handleReject}
              onAddNew={handleAddNew}
              onAddCaseAtLevel={handleAddCaseAtLevel}
              onMoveCase={handleMoveCase}
              onBatchApprove={handleBatchApprove}
            />
          )}
        </>
      )}

      {/* 编辑用例对话框 */}
      {editingCase && (
        <CaseEditDialog caseData={editingCase} onClose={() => setEditingCase(null)} onSaved={handleEditSave} />
      )}
    </div>
  )
}