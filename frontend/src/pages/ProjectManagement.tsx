import { useState, useEffect, useMemo, useCallback, useRef } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  Plus, Trash2, FolderOpen, Loader2, Edit3, Save, X, Search, FileText,
  TestTube, Archive, RotateCcw, ChevronRight, ChevronDown, AlertTriangle,
  Upload, MessageCircle,
} from 'lucide-react'
import { products as prodApi, requirements as reqApi } from '../api/client'
import { useProduct } from '../store/ProductContext'
import type { Project, Requirement } from '../types'
import { formatDate, priorityBadge, STATUS_LABELS } from '../utils'
import UploadRequirementModal from '../components/UploadRequirementModal'

// v2.45 修复：本地时区日期格式化，避免 slice(0,10) 在带时区 ISO 字符串上截取到 UTC 日期
function formatDateOnly(iso: string | undefined | null): string {
  if (!iso) return ''
  try {
    const d = new Date(iso)
    if (isNaN(d.getTime())) return String(iso).slice(0, 10)
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  } catch {
    return String(iso).slice(0, 10)
  }
}

// 项目状态标签配置（二状态：active → archived）
// v2.8.53: 对齐设计稿配色 — 活跃使用 emerald 绿色系，归档使用 slate 灰色系
const PROJECT_STATUS_STYLES: Record<string, { label: string; bg: string; text: string; iconBg: string; iconColor: string }> = {
  active: { label: '活跃', bg: 'bg-emerald-50', text: 'text-emerald-700', iconBg: 'bg-blue-50', iconColor: 'text-blue-500' },
  archived: { label: '已归档', bg: 'bg-slate-100', text: 'text-slate-500', iconBg: 'bg-slate-100', iconColor: 'text-slate-400' },
}

/** 编辑弹窗 */
function EditModal({
  name, description, version, onSave, onClose, saving
}: {
  name: string; description: string; version?: string;
  onSave: (name: string, desc: string, version?: string) => Promise<void>;
  onClose: () => void; saving: boolean
}) {
  const [n, setN] = useState(name)
  const [d, setD] = useState(description)
  const [v, setV] = useState(version || '1.0')
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" style={{ background: 'rgba(0, 0, 0, 0.32)', backdropFilter: 'blur(4px)' }} onClick={() => !saving && onClose()}>
      <div className="max-w-md w-full p-6 animate-slide-up" style={{ background: 'var(--background)', borderRadius: 'calc(var(--radius) * 1.1)', boxShadow: 'var(--shadow-apple-xl)' }} onClick={e => e.stopPropagation()}>
        <div className="flex items-center gap-3 mb-4">
          <Edit3 className="w-5 h-5" style={{ color: 'var(--brand-500)' }} />
          <h2 className="text-lg font-semibold" style={{ color: 'var(--foreground)' }}>编辑项目</h2>
        </div>
        <div className="space-y-3">
          <div>
            <label className="label">名称</label>
            <input value={n} onChange={e => setN(e.target.value)} className="input-field text-sm" autoFocus />
          </div>
          <div>
            <label className="label">描述</label>
            <textarea value={d} onChange={e => setD(e.target.value)} className="input-field text-sm resize-none" rows={3} placeholder="可选" />
          </div>
          <div>
            <label className="label">版本号</label>
            <input value={v} onChange={e => setV(e.target.value)} className="input-field text-sm" placeholder="如 1.0、2.1.0" />
          </div>
        </div>
        <div className="flex justify-end gap-3 mt-5">
          <button onClick={onClose} className="btn-secondary" disabled={saving}><X className="w-4 h-4" /> 取消</button>
          <button onClick={() => onSave(n, d, v)} disabled={!n.trim() || saving} className="btn-primary">
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />} 保存
          </button>
        </div>
      </div>
    </div>
  )
}

/** 添加表单 */
function AddForm({ onAdd, onCancel, adding }: {
  onAdd: (name: string, desc: string, version: string) => Promise<void>;
  onCancel: () => void; adding: boolean
}) {
  const [name, setName] = useState('')
  const [desc, setDesc] = useState('')
  const [version, setVersion] = useState('1.0')
  return (
    <div className="flex items-start gap-2 mb-4 p-3" style={{ background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.6)' }}>
      <span className="badge text-xs shrink-0 mt-1" style={{ background: 'rgba(0, 122, 255, 0.12)', color: 'var(--brand-500)' }}>项目</span>
      <div className="flex-1 space-y-2">
        <input value={name} onChange={e => setName(e.target.value)} className="input-field text-sm py-1.5" placeholder="项目名称" autoFocus />
        <input value={desc} onChange={e => setDesc(e.target.value)} className="input-field text-sm py-1.5" placeholder="描述（可选）" />
        <input value={version} onChange={e => setVersion(e.target.value)} className="input-field text-sm py-1.5" placeholder="版本号，如 1.0、2.1.0" />
      </div>
      <div className="flex gap-1 shrink-0 mt-1">
        <button onClick={() => onAdd(name, desc, version)} disabled={!name.trim() || adding} className="btn-primary text-xs py-1.5">确定</button>
        <button onClick={onCancel} className="btn-ghost text-xs py-1.5">取消</button>
      </div>
    </div>
  )
}

/** v2.45 项目归档弹窗 */
function ArchiveModal({ projId, projName, onClose, onArchived }: {
  projId: string; projName: string; onClose: () => void; onArchived: () => void
}) {
  // v2.45 修复：使用本地日期方法生成默认值，避免 toISOString() 返回 UTC 时间导致 UTC+8 凌晨显示前一天
  const todayLocal = () => {
    const d = new Date()
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  }
  const [goLiveDate, setGoLiveDate] = useState(todayLocal())
  const [changeSummary, setChangeSummary] = useState('')
  const [archiving, setArchiving] = useState(false)
  const [error, setError] = useState('')
  // v2.45 增强：归档为高影响操作，需输入项目名二次确认防止误操作
  const [confirmName, setConfirmName] = useState('')

  const handleArchive = async () => {
    setArchiving(true); setError('')
    try {
      const res = await prodApi.archiveProject(projId, goLiveDate, changeSummary)
      // v2.8.58: 后端改为 Celery 异步派发，归档立即返回，不再阻塞
      // 异步模式下向量化在后台进行，vectorization_status=pending 由 Celery 回写
      onArchived()
    } catch (e: any) {
      setError(e.message || '归档失败')
    } finally { setArchiving(false) }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" style={{ background: 'rgba(0, 0, 0, 0.32)', backdropFilter: 'blur(4px)' }} onClick={() => !archiving && onClose()}>
      <div className="max-w-md w-full p-6 animate-slide-up" style={{ background: 'var(--background)', borderRadius: 'calc(var(--radius) * 1.1)', boxShadow: 'var(--shadow-apple-xl)' }} onClick={e => e.stopPropagation()}>
        <div className="flex items-center gap-3 mb-4">
          <Archive className="w-5 h-5" style={{ color: 'var(--foreground-tertiary)' }} />
          <h2 className="text-lg font-semibold" style={{ color: 'var(--foreground)' }}>归档项目</h2>
        </div>
        <p className="text-sm mb-4" style={{ color: 'var(--foreground-secondary)' }}>
          项目「{projName}」归档后，项目下所有需求将自动归档并写入知识库，供后续需求检索参考。
        </p>
        <div className="space-y-3">
          <div>
            <label className="label">上线时间 *</label>
            <input
              type="date"
              value={goLiveDate}
              onChange={e => setGoLiveDate(e.target.value)}
              className="input-field text-sm"
              autoFocus
            />
            <p className="text-xs mt-1" style={{ color: 'var(--foreground-tertiary)' }}>用于知识库检索时区分最新需求</p>
          </div>
          <div>
            <label className="label">变更说明（可选）</label>
            <textarea
              value={changeSummary}
              onChange={e => setChangeSummary(e.target.value)}
              className="input-field text-sm resize-none"
              rows={2}
              placeholder="本次归档的变更说明，将记录到版本历史"
            />
          </div>
          <div>
            <label className="label">输入项目名以确认 *</label>
            <input
              value={confirmName}
              onChange={e => setConfirmName(e.target.value)}
              className="input-field text-sm"
              placeholder={projName}
            />
            <p className="text-xs mt-1" style={{ color: 'var(--foreground-tertiary)' }}>
              归档会写入知识库并冻结依赖版本快照，请输入项目名「{projName}」确认
            </p>
          </div>
          {error && (
            <div className="text-sm p-2" style={{ background: 'var(--state-error-surface)', color: 'var(--state-error)', borderRadius: 'calc(var(--radius) * 0.4)' }}>
              {error}
            </div>
          )}
        </div>
        <div className="flex justify-end gap-3 mt-5">
          <button onClick={onClose} className="btn-secondary" disabled={archiving}><X className="w-4 h-4" /> 取消</button>
          <button
            onClick={handleArchive}
            disabled={!goLiveDate || archiving || confirmName.trim() !== projName}
            className="btn-primary"
          >
            {archiving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Archive className="w-4 h-4" />} 确认归档
          </button>
        </div>
      </div>
    </div>
  )
}

export default function ProjectManagementPage() {
  const { products, selectedProductId, selectedProduct } = useProduct()
  const navigate = useNavigate()
  // 读取 URL query 参数（支持 ?upload=true&project_id=xxx 直接打开上传弹窗）
  const [searchParams, setSearchParams] = useSearchParams()

  const [projects, setProjects] = useState<Record<string, Project[]>>({})
  const [loading, setLoading] = useState(false)
  const [search, setSearch] = useState('')
  const [showAdd, setShowAdd] = useState(false)
  const [adding, setAdding] = useState(false)
  const [editModal, setEditModal] = useState<{ id: string; name: string; description: string; version?: string } | null>(null)
  const [saving, setSaving] = useState(false)
  const [activeProductId, setActiveProductId] = useState(selectedProductId)
  const [archiveModal, setArchiveModal] = useState<{ id: string; name: string } | null>(null)
  const [reactivating, setReactivating] = useState<string | null>(null)
  const [toast, setToast] = useState<{ type: 'success' | 'error' | 'warning'; msg: string } | null>(null)

  // v2.8.52: 条件筛选
  const [filterProjStatus, setFilterProjStatus] = useState('')   // 项目状态: ''/active/archived
  const [filterReqStatus, setFilterReqStatus] = useState('')     // 需求状态: ''/analyzing/draft/pending_review/confirmed/archived
  const [filterReqPriority, setFilterReqPriority] = useState('') // 需求优先级: ''/P0/P1/P2/P3

  // v2.46 折叠面板状态：展开的项目 ID 集合
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set())
  // 每个项目下的需求列表缓存
  const [projectReqs, setProjectReqs] = useState<Record<string, Requirement[]>>({})
  // 正在加载需求的项目 ID 集合
  const [loadingReqs, setLoadingReqs] = useState<Set<string>>(new Set())
  // 搜索模式：匹配需求的项目 ID 集合（用于在搜索时额外显示项目名不匹配但需求匹配的项目）
  const [searchMatchedProjectIds, setSearchMatchedProjectIds] = useState<Set<string> | null>(null)
  // v2.8.57: analyzing 需求的实时任务进度（req_id → { progress, phase, message }）
  const [reqTaskProgress, setReqTaskProgress] = useState<Record<string, { progress: number; phase: string; message: string }>>({})
  const taskProgressTimerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // 上传需求弹窗状态
  const [showUpload, setShowUpload] = useState(false)
  // 上传弹窗预选的项目 ID（从项目卡片点击「上传需求文档」时携带）
  const [uploadProjectId, setUploadProjectId] = useState('')

  useEffect(() => {
    setActiveProductId(selectedProductId)
    // v2.45 修复：切换产品时关闭所有 Modal/AddForm，避免显示旧数据
    setEditModal(null)
    setArchiveModal(null)
    setShowAdd(false)
    // v2.46 切换产品时清空折叠状态和需求缓存
    setExpandedIds(new Set())
    setProjectReqs({})
    setSearchMatchedProjectIds(null)
    if (selectedProductId) loadData(selectedProductId)
  }, [selectedProductId])

  // 读取 URL query：?upload=true&project_id=xxx 时自动打开上传弹窗（支持外部直接链接）
  useEffect(() => {
    const qUpload = searchParams.get('upload') === 'true'
    const qProj = searchParams.get('project_id') || ''
    if (qUpload) {
      setUploadProjectId(qProj)
      setShowUpload(true)
      // 清掉 query 参数，避免后续刷新重复打开
      setSearchParams({}, { replace: true })
    }
  }, [searchParams])

  // v2.45 增强：EditModal/ArchiveModal 支持 Esc 关闭
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (archiveModal) setArchiveModal(null)
        else if (editModal) setEditModal(null)
        else if (showAdd) setShowAdd(false)
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [archiveModal, editModal, showAdd])

  // toast 自动消失
  useEffect(() => {
    if (!toast) return
    const t = setTimeout(() => setToast(null), 3500)
    return () => clearTimeout(t)
  }, [toast])

  const loadData = async (pid: string) => {
    if (!pid) return
    setLoading(true)
    try {
      const projs = await prodApi.listProjects(pid)
      setProjects(p => ({ ...p, [pid]: projs || [] }))
    } catch (e: any) {
      // v2.45 修复：列表加载失败时弹出 alert 提示，避免用户误以为"无项目"；
      // 导航导致的主动 abort / 登录过期跳转（v3.1.2）属正常流程，不弹窗
      if (!e?._aborted && !e?._unauthorized) alert('加载项目列表失败：' + (e?.message || '网络错误'))
    } finally { setLoading(false) }
  }

  // v2.8.52 加载项目下的需求列表（带搜索 + 状态/优先级过滤）
  const loadProjectReqs = useCallback(async (projectId: string, keyword?: string, reqStatus?: string, reqPriority?: string) => {
    setLoadingReqs(prev => new Set(prev).add(projectId))
    try {
      const r = await reqApi.list(1, 50, activeProductId || undefined, projectId, {
        search: keyword || undefined,
        status: reqStatus || undefined,
        priority: reqPriority || undefined,
      })
      setProjectReqs(prev => ({ ...prev, [projectId]: r.items || [] }))
    } catch (e: any) {
      setProjectReqs(prev => ({ ...prev, [projectId]: [] }))
      setToast({ type: 'error', msg: `加载项目需求失败：${e?.message || '未知错误'}` })
    } finally {
      setLoadingReqs(prev => {
        const next = new Set(prev)
        next.delete(projectId)
        return next
      })
    }
  }, [activeProductId])

  // v2.46 统一搜索：搜索关键词变化时，并行查询匹配需求的项目 ID 集合
  useEffect(() => {
    if (!activeProductId || !search.trim()) {
      setSearchMatchedProjectIds(null)
      return
    }
    let cancelled = false
    const t = setTimeout(async () => {
      try {
        // 查询所有匹配关键词+筛选条件的需求，提取对应 project_id 集合
        const r = await reqApi.list(1, 200, activeProductId, undefined, {
          search: search.trim(),
          status: filterReqStatus || undefined,
          priority: filterReqPriority || undefined,
        })
        if (cancelled) return
        const ids = new Set<string>()
        for (const req of (r.items || [])) {
          if (req.project_id) ids.add(req.project_id)
        }
        setSearchMatchedProjectIds(ids)
      } catch {
        if (!cancelled) setSearchMatchedProjectIds(new Set())
      }
    }, 400)
    return () => { cancelled = true; clearTimeout(t) }
  }, [search, activeProductId, filterReqStatus, filterReqPriority])

  // v2.8.52 搜索/筛选变化时，重新加载已展开项目的需求
  useEffect(() => {
    if (expandedIds.size === 0) return
    for (const pid of expandedIds) {
      loadProjectReqs(pid, search.trim() || undefined, filterReqStatus || undefined, filterReqPriority || undefined)
    }
  }, [search, filterReqStatus, filterReqPriority])

  // v2.8.57: 轮询 analyzing 需求的实时任务进度，展示迷你进度条
  useEffect(() => {
    if (taskProgressTimerRef.current) {
      clearInterval(taskProgressTimerRef.current)
      taskProgressTimerRef.current = null
    }
    // 收集所有展开项目中的 analyzing 需求 ID
    const analyzingReqIds: string[] = []
    for (const pid of expandedIds) {
      const reqs = projectReqs[pid] || []
      for (const r of reqs) {
        if (r.status === 'analyzing') analyzingReqIds.push(r.id)
      }
    }
    if (analyzingReqIds.length === 0) {
      setReqTaskProgress({})
      return
    }
    // 立即查询一次
    const pollOnce = async () => {
      const updates: Record<string, { progress: number; phase: string; message: string }> = {}
      await Promise.all(analyzingReqIds.map(async (rid) => {
        try {
          const task = await reqApi.getTaskByReq(rid)
          if (task && (task.status === 'running' || task.status === 'pending')) {
            updates[rid] = {
              progress: task.progress || 0,
              phase: task.phase || '',
              message: task.message || '',
            }
          }
        } catch { /* 静默失败 */ }
      }))
      setReqTaskProgress(updates)
    }
    pollOnce()
    taskProgressTimerRef.current = setInterval(pollOnce, 3000)
    return () => {
      if (taskProgressTimerRef.current) {
        clearInterval(taskProgressTimerRef.current)
        taskProgressTimerRef.current = null
      }
    }
  }, [expandedIds, projectReqs])

  const handleAdd = async (name: string, desc: string, version: string) => {
    if (!activeProductId || !name.trim()) return
    setAdding(true)
    try {
      await prodApi.createProject(activeProductId, name.trim(), desc.trim(), version.trim() || '1.0')
      await loadData(activeProductId)
      setShowAdd(false)
    } catch (e: any) { alert('创建失败: ' + e.message) } finally { setAdding(false) }
  }

  const handleDelete = async (id: string) => {
    if (!confirm('确认删除此项目？此操作不可恢复，关联需求与用例也将被清除。')) return
    try {
      await prodApi.deleteProject(id)
      if (activeProductId) await loadData(activeProductId)
    } catch (e: any) { alert('删除失败: ' + e.message) }
  }

  const handleEditSave = async (name: string, desc: string, version?: string) => {
    if (!editModal) return
    setSaving(true)
    try {
      await prodApi.updateProject(editModal.id, name.trim(), desc.trim(), version?.trim() || '1.0')
      if (activeProductId) await loadData(activeProductId)
      setEditModal(null)
    } catch (e: any) { alert('保存失败: ' + e.message) } finally { setSaving(false) }
  }

  const handleReactivate = async (id: string, name: string) => {
    if (!confirm(`确认恢复项目「${name}」为活跃状态？\n\n这将：\n• 从知识库删除该项目的所有需求向量\n• 对应版本的依赖关系标记为未启用\n• 需求和功能点状态回归活跃\n• 新需求影响分析不再考虑该版本\n\n此操作可逆，后续可重新归档。`)) return
    setReactivating(id)
    try {
      await prodApi.reactivateProject(id)
      if (activeProductId) await loadData(activeProductId)
      setToast({ type: 'success', msg: '项目已恢复活动状态' })
    } catch (e: any) { setToast({ type: 'error', msg: '恢复失败: ' + e.message }) } finally { setReactivating(null) }
  }

  // v2.8.52: 需求删除（在项目管理页面直接操作）
  const handleDeleteReq = async (reqId: string, reqTitle: string, projectId: string) => {
    if (!confirm(`确认删除需求「${reqTitle}」？此操作不可恢复，关联的测试用例也将被清除。`)) return
    try {
      await reqApi.delete(reqId)
      // 重新加载该项目的需求列表
      loadProjectReqs(projectId, search.trim() || undefined, filterReqStatus || undefined, filterReqPriority || undefined)
      setToast({ type: 'success', msg: '需求已删除' })
    } catch (e: any) {
      setToast({ type: 'error', msg: '删除失败：' + (e?.message || '未知错误') })
    }
  }

  // v2.46 切换项目展开/收起
  const toggleExpand = (projectId: string) => {
    setExpandedIds(prev => {
      const next = new Set(prev)
      if (next.has(projectId)) {
        next.delete(projectId)
      } else {
        next.add(projectId)
        // 首次展开时加载需求
        if (!projectReqs[projectId]) {
          loadProjectReqs(projectId, search.trim() || undefined, filterReqStatus || undefined, filterReqPriority || undefined)
        }
      }
      return next
    })
  }

  // v2.8.52 统一搜索 + 条件筛选
  const filtered = useMemo(() => {
    const all = projects[activeProductId || ''] || []
    let result = all
    // 项目状态筛选
    if (filterProjStatus) {
      result = result.filter(p => (p.status || 'active') === filterProjStatus)
    }
    // 搜索关键词
    if (search.trim()) {
      const q = search.toLowerCase()
      const matchedReqProjectIds = searchMatchedProjectIds || new Set<string>()
      result = result.filter(p =>
        p.name.toLowerCase().includes(q) ||
        (p.description || '').toLowerCase().includes(q) ||
        (p.version || '').toLowerCase().includes(q) ||
        matchedReqProjectIds.has(p.id)
      )
    }
    return result
  }, [projects, activeProductId, search, searchMatchedProjectIds, filterProjStatus])

  return (
    <div className="space-y-6">
      {/* toast 通知 */}
      {toast && (
        <div
          className="fixed top-4 right-4 z-50 px-4 py-3 text-sm shadow-lg animate-slide-up"
          style={{
            background: 'var(--background)',
            borderRadius: 'calc(var(--radius) * 0.6)',
            borderLeft: `3px solid var(--state-${toast.type === 'success' ? 'success' : toast.type === 'warning' ? 'warning' : 'error'})`,
            boxShadow: 'var(--shadow-apple-md)',
            color: 'var(--foreground)',
            maxWidth: '420px',
          }}
        >
          {toast.msg}
        </div>
      )}

      <div className="flex items-center justify-between">
        <div>
          <h1 className="page-title flex items-center gap-2">
            <FolderOpen className="w-6 h-6" style={{ color: 'var(--brand-500)' }} />
            项目管理
          </h1>
          <p className="text-sm mt-1" style={{ color: 'var(--foreground-tertiary)' }}>
            管理产品下的项目与需求 — 点击项目展开查看需求列表，支持按状态/优先级筛选
          </p>
        </div>
      </div>

      {!activeProductId ? (
        <div className="text-center py-16" style={{ color: 'var(--foreground-tertiary)' }}>
          <FolderOpen className="w-12 h-12 mx-auto mb-3 opacity-50" />
          <p className="font-medium">请先选择一个产品</p>
          <p className="text-sm mt-1">选择产品后查看和管理其项目</p>
        </div>
      ) : (
        <div className="card">
          <div className="flex items-center justify-between mb-4">
            <h2 className="section-title flex items-center gap-2">
              <FolderOpen className="w-5 h-5" style={{ color: 'var(--brand-500)' }} />
              {selectedProduct?.name || activeProductId}
              <span className="text-sm font-normal" style={{ color: 'var(--foreground-tertiary)' }}>— 项目列表</span>
            </h2>
          </div>

          <div className="flex items-center gap-2 mb-4 flex-wrap">
            <div className="relative flex-1 min-w-[200px]">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4" style={{ color: 'var(--foreground-tertiary)' }} />
              <input
                value={search}
                onChange={e => setSearch(e.target.value)}
                className="input-field text-sm py-1.5 pl-9"
                placeholder="搜索项目名称或需求标题..."
              />
            </div>
            {/* v2.8.52: 项目状态筛选 */}
            <select
              value={filterProjStatus}
              onChange={e => setFilterProjStatus(e.target.value)}
              className="input-field text-sm py-1.5 w-auto"
              title="筛选项目状态"
            >
              <option value="">全部状态</option>
              <option value="active">活跃</option>
              <option value="archived">已归档</option>
            </select>
            {/* v2.8.52: 需求状态筛选 */}
            <select
              value={filterReqStatus}
              onChange={e => setFilterReqStatus(e.target.value)}
              className="input-field text-sm py-1.5 w-auto"
              title="筛选需求状态"
            >
              <option value="">需求状态</option>
              <option value="analyzing">分析中</option>
              <option value="draft">待澄清</option>
              <option value="pending_review">待确认</option>
              <option value="confirmed">已确认</option>
              <option value="archived">已归档</option>
            </select>
            {/* v2.8.52: 需求优先级筛选 */}
            <select
              value={filterReqPriority}
              onChange={e => setFilterReqPriority(e.target.value)}
              className="input-field text-sm py-1.5 w-auto"
              title="筛选需求优先级"
            >
              <option value="">优先级</option>
              <option value="P0">P0</option>
              <option value="P1">P1</option>
              <option value="P2">P2</option>
              <option value="P3">P3</option>
            </select>
            <button onClick={() => setShowAdd(true)} className="btn-primary text-xs whitespace-nowrap">
              <Plus className="w-3 h-3" /> 添加项目
            </button>
          </div>

          {showAdd && (
            <AddForm onAdd={handleAdd} onCancel={() => setShowAdd(false)} adding={adding} />
          )}

          {loading ? (
            <div className="flex items-center justify-center py-10"><Loader2 className="w-6 h-6 animate-spin" style={{ color: 'var(--foreground-tertiary)' }} /></div>
          ) : filtered.length === 0 ? (
            <p className="text-xs py-8 text-center" style={{ background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.6)', color: 'var(--foreground-tertiary)' }}>
              {search ? '未找到匹配的项目或需求' : '暂无项目'}
            </p>
          ) : (
            <div className="space-y-1.5">
              {filtered.map(proj => {
                const isExpanded = expandedIds.has(proj.id)
                const statusStyle = PROJECT_STATUS_STYLES[proj.status || 'active'] || PROJECT_STATUS_STYLES.active
                const reqs = projectReqs[proj.id] || []
                const isLoadingReqs = loadingReqs.has(proj.id)
                // v2.46 搜索模式下高亮提示：项目名不匹配但需求匹配的项目
                const isMatchedByReq = search.trim() !== '' &&
                  searchMatchedProjectIds?.has(proj.id) &&
                  !proj.name.toLowerCase().includes(search.toLowerCase())

                return (
                  <div
                    key={proj.id}
                    className="border transition-all"
                    style={{
                      borderRadius: 'calc(var(--radius) * 0.6)',
                      borderColor: isExpanded ? 'var(--brand-300)' : 'var(--border)',
                      background: isMatchedByReq ? 'var(--state-warning-surface)' : 'var(--background)',
                    }}
                  >
                    {/* 项目头部（可点击展开） v2.8.53: 对齐设计稿布局 */}
                    <div className="flex items-center justify-between px-4 py-3.5 group">
                      {/* 左侧：折叠按钮 + 图标 + 信息 */}
                      <div className="flex items-start gap-2.5 min-w-0 flex-1 cursor-pointer" onClick={() => toggleExpand(proj.id)}>
                        {/* 展开图标 */}
                        {isExpanded ? (
                          <ChevronDown className="w-4 h-4 flex-shrink-0 mt-0.5" style={{ color: 'var(--foreground-tertiary)' }} />
                        ) : (
                          <ChevronRight className="w-4 h-4 flex-shrink-0 mt-0.5" style={{ color: 'var(--foreground-tertiary)' }} />
                        )}
                        {/* 项目图标 v2.8.53: 对齐设计稿 — 活跃使用蓝色背景，归档使用灰色背景 */}
                        <div
                          className={`w-7 h-7 rounded-md flex items-center justify-center flex-shrink-0 ${statusStyle.iconBg}`}
                        >
                          <FolderOpen className={`w-4 h-4 ${statusStyle.iconColor}`} />
                        </div>
                        {/* 项目信息 */}
                        <div className="min-w-0 pt-0.5">
                          <div className="flex items-center gap-2 flex-wrap">
                            <p className="text-sm font-semibold truncate" style={{ color: 'var(--foreground-secondary)' }}>{proj.name}</p>
                            {proj.version && (
                              <span className="text-[11px] px-2 py-0.5 font-mono font-medium" style={{ background: 'rgba(0, 122, 255, 0.08)', color: 'var(--brand-500)', borderRadius: 'calc(var(--radius) * 0.25)' }}>
                                v{proj.version}
                              </span>
                            )}
                            {/* 搜索匹配提示 */}
                            {isMatchedByReq && (
                              <span className="text-[11px] px-2 py-0.5 flex items-center gap-1 font-medium" style={{ background: 'var(--state-warning-surface)', color: 'var(--state-warning)', borderRadius: 'calc(var(--radius) * 0.25)' }}>
                                <AlertTriangle className="w-3 h-3" /> 需求匹配
                              </span>
                            )}
                          </div>
                          <p className="text-xs truncate mt-0.5" style={{ color: 'var(--foreground-tertiary)' }}>{proj.description || '无描述'}</p>
                          {proj.status === 'archived' && proj.go_live_date && (
                            <p className="text-[11px] mt-1" style={{ color: 'var(--foreground-quaternary)' }}>
                              上线时间: {formatDateOnly(proj.go_live_date)}
                            </p>
                          )}
                        </div>
                      </div>

                      {/* 中间：状态徽章 v2.8.53: 独立于项目信息显示 */}
                      <div className="flex items-center flex-shrink-0 mr-3">
                        <span className={`text-[11px] px-2 py-0.5 font-medium ${statusStyle.bg} ${statusStyle.text}`} style={{ borderRadius: '9999px' }}>
                          {statusStyle.label}
                        </span>
                      </div>

                      {/* 右侧：操作按钮 v2.8.53: 默认半透明 + hover 完全显示 */}
                      <div className="flex items-center gap-0.5 flex-shrink-0 opacity-[0.6] transition-opacity duration-200 group-hover:opacity-100">
                        {/* 上传需求文档：直接打开内嵌上传弹窗，不再跳转 */}
                        <button
                          onClick={() => { setUploadProjectId(proj.id); setShowUpload(true) }}
                          className="btn-ghost p-1.5"
                          title="上传需求文档"
                        >
                          <Upload className="w-3.5 h-3.5" />
                        </button>
                        {/* 查看测试用例 */}
                        <button
                          onClick={() => navigate(`/testcases?project_id=${encodeURIComponent(proj.id)}`)}
                          className="btn-ghost p-1.5"
                          title="查看该项目下的测试用例"
                        >
                          <TestTube className="w-3.5 h-3.5" />
                        </button>
                        {/* 活跃项目操作：归档 + 分隔 + 编辑 + 删除 */}
                        {proj.status === 'active' && (
                          <>
                            <button
                              onClick={() => setArchiveModal({ id: proj.id, name: proj.name })}
                              className="btn-ghost p-1.5"
                              title="归档项目（写入知识库，冻结需求编辑）"
                            >
                              <Archive className="w-3.5 h-3.5" style={{ color: 'var(--foreground-tertiary)' }} />
                            </button>
                            <span className="w-px h-4 mx-1" style={{ background: 'var(--border)' }} />
                            <button onClick={() => setEditModal({ id: proj.id, name: proj.name, description: proj.description, version: proj.version })}
                              className="btn-ghost p-1.5" title="编辑项目">
                              <Edit3 className="w-3.5 h-3.5" />
                            </button>
                            <button onClick={() => handleDelete(proj.id)}
                              className="btn-ghost p-1.5 hover:bg-red-50" title="删除项目">
                              <Trash2 className="w-3.5 h-3.5" style={{ color: 'var(--state-error)' }} />
                            </button>
                          </>
                        )}
                        {/* 归档项目操作：恢复活动 */}
                        {proj.status === 'archived' && (
                          <button
                            onClick={() => handleReactivate(proj.id, proj.name)}
                            disabled={reactivating === proj.id}
                            className="btn-ghost p-1.5"
                            title="恢复活动（从知识库删除，回到活跃状态）"
                          >
                            {reactivating === proj.id
                              ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                              : <RotateCcw className="w-3.5 h-3.5" style={{ color: 'var(--state-warning)' }} />
                            }
                          </button>
                        )}
                      </div>
                    </div>

                    {/* v2.8.52 折叠面板内容：项目下的需求列表 — 直接嵌入卡片，border-t 分隔 */}
                    {isExpanded && (
                      <div className="overflow-hidden" style={{ borderTop: '1px solid var(--border)', borderBottomLeftRadius: 'calc(var(--radius) * 0.6)', borderBottomRightRadius: 'calc(var(--radius) * 0.6)' }}>
                        {isLoadingReqs ? (
                          <div className="flex items-center justify-center py-6">
                            <Loader2 className="w-4 h-4 animate-spin mr-2" style={{ color: 'var(--foreground-tertiary)' }} />
                            <span className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>加载需求列表...</span>
                          </div>
                        ) : reqs.length === 0 ? (
                          <div className="text-center py-6 px-4">
                            <FileText className="w-6 h-6 mx-auto mb-1.5" style={{ color: 'var(--foreground-quaternary)' }} />
                            <p className="text-sm m-0" style={{ color: 'var(--foreground-tertiary)' }}>
                              {search.trim() || filterReqStatus || filterReqPriority ? '该项目下无匹配需求' : '该项目下暂无需求'}
                            </p>
                          </div>
                        ) : (
                          <table className="w-full">
                            <thead>
                              <tr style={{ background: 'var(--background-muted)' }}>
                                <th className="text-left text-[11px] font-semibold uppercase tracking-wider px-4 py-2" style={{ color: 'var(--foreground-tertiary)' }}>需求标题</th>
                                <th className="text-center text-[11px] font-semibold uppercase tracking-wider px-3 py-2 w-16" style={{ color: 'var(--foreground-tertiary)' }}>优先级</th>
                                <th className="text-center text-[11px] font-semibold uppercase tracking-wider px-3 py-2 w-24" style={{ color: 'var(--foreground-tertiary)' }}>状态</th>
                                <th className="text-right text-[11px] font-semibold uppercase tracking-wider px-4 py-2 w-28" style={{ color: 'var(--foreground-tertiary)' }}>创建时间</th>
                                <th className="text-center text-[11px] font-semibold uppercase tracking-wider px-3 py-2 w-16" style={{ color: 'var(--foreground-tertiary)' }}>操作</th>
                              </tr>
                            </thead>
                            <tbody>
                              {reqs.map(req => (
                                <tr
                                  key={req.id}
                                  className="transition-colors duration-150 cursor-pointer group hover:bg-slate-50"
                                  style={{ borderTop: '1px solid var(--border)' }}
                                  onClick={() => navigate(`/requirements/${encodeURIComponent(req.id)}`)}
                                >
                                  <td className="px-4 py-2.5">
                                    <div className="flex items-center gap-2 min-w-0">
                                      <FileText className="w-3.5 h-3.5 flex-shrink-0" style={{ color: 'var(--foreground-tertiary)' }} />
                                      <span className="text-sm font-medium truncate" style={{ color: 'var(--foreground-secondary)' }} title={req.title}>
                                        {req.title || req.id}
                                      </span>
                                    </div>
                                  </td>
                                  <td className="px-3 py-2.5 text-center">
                                    <span className={`text-xs font-semibold font-mono ${priorityBadge(req.priority)}`}>{req.priority}</span>
                                  </td>
                                  <td className="px-3 py-2.5 text-center">
                                    <span
                                      className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-[11px] font-medium"
                                      style={(() => {
                                        switch (req.status) {
                                          case 'draft': return { background: 'var(--state-warning-surface)', color: 'var(--state-warning)' }
                                          case 'active': return { background: 'var(--state-success-surface)', color: 'var(--state-success)' }
                                          case 'confirmed': return { background: 'var(--state-success-surface)', color: 'var(--state-success)' }
                                          case 'archived': return { background: 'var(--background-muted)', color: 'var(--foreground-tertiary)' }
                                          case 'superseded': return { background: 'var(--state-warning-surface)', color: 'var(--state-warning)' }
                                          case 'deprecated': return { background: 'var(--state-error-surface)', color: 'var(--state-error)' }
                                          case 'analyzing': return { background: 'rgba(0, 122, 255, 0.08)', color: 'var(--brand-500)' }
                                          case 'pending_review': return { background: 'rgba(88, 86, 214, 0.1)', color: '#5856d6' }
                                          default: return { background: 'rgba(0, 122, 255, 0.08)', color: 'var(--brand-500)' }
                                        }
                                      })()}
                                    >
                                      {STATUS_LABELS[req.status] || req.status}
                                      {req.status === 'draft' && <MessageCircle className="w-3 h-3 ml-0.5 animate-pulse" />}
                                      {req.status === 'analyzing' && reqTaskProgress[req.id] && (
                                        <Loader2 className="w-3 h-3 ml-0.5 animate-spin" />
                                      )}
                                    </span>
                                    {/* v2.8.57: analyzing 状态展示迷你进度条 */}
                                    {req.status === 'analyzing' && reqTaskProgress[req.id] && (() => {
                                      const tp = reqTaskProgress[req.id]
                                      const phaseLabels: Record<string, string> = {
                                        upload: '解析中', parse: '解析中', kb_search: '检索知识库',
                                        clarify: '澄清中', regenerate: '保存中', recheck: '验证中',
                                        done: '完成', '': '处理中',
                                      }
                                      return (
                                        <div className="mt-1.5 flex items-center gap-1.5" style={{ minWidth: 120 }}>
                                          <div
                                            className="rounded-full overflow-hidden flex-shrink-0"
                                            style={{ width: 60, height: 3, background: 'var(--background-muted)' }}
                                          >
                                            <div
                                              className="h-full rounded-full transition-all duration-500"
                                              style={{ width: `${tp.progress}%`, background: 'var(--brand-500)' }}
                                            />
                                          </div>
                                          <span className="text-[10px] whitespace-nowrap" style={{ color: 'var(--foreground-muted)' }}>
                                            {tp.progress}% · {phaseLabels[tp.phase] || tp.phase}
                                          </span>
                                        </div>
                                      )
                                    })()}
                                  </td>
                                  <td className="px-4 py-2.5 text-right">
                                    <span className="text-xs tabular-nums" style={{ color: 'var(--foreground-tertiary)' }}>{formatDate(req.created_at)}</span>
                                  </td>
                                  <td className="px-3 py-2.5 text-center" onClick={e => e.stopPropagation()}>
                                    <div className="flex items-center justify-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                                      <button
                                        onClick={() => navigate(`/requirements/${encodeURIComponent(req.id)}`)}
                                        className="btn-ghost p-1"
                                        title="查看详情"
                                      >
                                        <FileText className="w-3 h-3" />
                                      </button>
                                      {req.status !== 'archived' && (
                                        <button
                                          onClick={(e) => { e.stopPropagation(); handleDeleteReq(req.id, req.title || req.id, proj.id) }}
                                          className="btn-ghost p-1 hover:bg-red-50"
                                          title="删除需求"
                                        >
                                          <Trash2 className="w-3 h-3" style={{ color: 'var(--state-error)' }} />
                                        </button>
                                      )}
                                    </div>
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        )}
                        {/* 底部操作栏 — 仅分隔 */}
                        <div className="px-4 py-1.5 border-t" style={{ borderColor: 'var(--border)', background: 'var(--background-muted)' }}>
                          <span className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>
                            共 {reqs.length} 条需求
                          </span>
                        </div>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </div>
      )}

      {editModal && (
        <EditModal
          name={editModal.name}
          description={editModal.description}
          version={editModal.version}
          saving={saving}
          onSave={handleEditSave}
          onClose={() => setEditModal(null)}
        />
      )}

      {archiveModal && (
        <ArchiveModal
          projId={archiveModal.id}
          projName={archiveModal.name}
          onClose={() => setArchiveModal(null)}
          onArchived={() => {
            setArchiveModal(null)
            if (activeProductId) loadData(activeProductId)
          }}
        />
      )}

      {/* 上传需求弹窗：上传成功后刷新对应项目的需求列表 */}
      <UploadRequirementModal
        open={showUpload}
        onClose={() => setShowUpload(false)}
        productId={activeProductId}
        preselectedProjectId={uploadProjectId}
        onUploaded={(reqId) => {
          // 上传成功：刷新该项目下的需求列表并展开该项目，提示成功
          if (uploadProjectId) {
            setExpandedIds(prev => new Set(prev).add(uploadProjectId))
            loadProjectReqs(uploadProjectId, search.trim() || undefined, filterReqStatus || undefined, filterReqPriority || undefined)
          }
          setToast({ type: 'success', msg: `需求已上传成功（${reqId}）` })
        }}
      />

    </div>
  )
}
