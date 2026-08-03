import { useState, useEffect, useCallback, memo } from 'react'
import { FileText, TestTube, Trash2, Loader2, CheckCircle2, AlertTriangle, RefreshCw, Calendar, X, Save, XCircle, ArrowLeft, ChevronRight, Archive } from 'lucide-react'
import { knowledge, requirements as reqApi, products as prodApi } from '../api/client'
import type { KbEntry, KbModelStatus, KbProject } from '../types'
import { useProduct } from '../store/ProductContext'
import LoadingSpinner from '../components/LoadingSpinner'

function formatDateTime(iso?: string): string {
  if (!iso) return ''
  try {
    const d = new Date(iso)
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  } catch {
    return iso.slice(0, 16).replace('T', ' ')
  }
}

function formatDate(iso?: string): string {
  if (!iso) return ''
  try {
    const d = new Date(iso)
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  } catch {
    return iso.slice(0, 10)
  }
}

/** 将 datetime-local input 的值转为 ISO 字符串（含秒） */
function datetimeLocalToISO(localStr: string): string {
  if (!localStr) return ''
  // datetime-local 格式: YYYY-MM-DDTHH:MM
  const d = new Date(localStr)
  return d.toISOString()
}

/** 将 ISO 字符串转为 datetime-local input 可用的值 */
function isoToDatetimeLocal(iso?: string): string {
  if (!iso) return ''
  try {
    const d = new Date(iso)
    const pad = (n: number) => String(n).padStart(2, '0')
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
  } catch {
    return ''
  }
}

// ============================================================================
// 弹窗组件（React.memo 隔离 — 避免 datetime-local onChange 触发整页重渲染）
// ============================================================================

const GoLiveEditModal = memo(function GoLiveEditModal({
  entry,
  onClose,
  onSaved,
  toast,
}: {
  entry: KbEntry
  onClose: () => void
  onSaved: (id: string, newDate: string) => void
  toast: (t: { type: 'success' | 'error' | 'warning'; msg: string }) => void
}) {
  const [editValue, setEditValue] = useState(isoToDatetimeLocal(entry.go_live_date))
  const [saving, setSaving] = useState(false)

  // v2.45 增强：Esc 关闭 GoLiveEditModal（保存中禁用避免误关）
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !saving) onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [saving, onClose])

  const handleSave = async () => {
    if (!editValue) return
    setSaving(true)
    try {
      const isoDate = datetimeLocalToISO(editValue)
      const r = await reqApi.updateGoLiveDate(entry.id, isoDate)
      onSaved(entry.id, r.go_live_date)  // 更新父列表
      toast({
        type: r.milvus_updated ? 'success' : 'warning',
        msg: r.milvus_updated
          ? `「${entry.title}」上线时间已更新，向量库元数据已同步`
          : `「${entry.title}」上线时间已更新，但向量库元数据同步失败`,
      })
      onClose()
    } catch (e: any) {
      const errBody = e?.data?.detail || e?.detail || e?.message || ''
      toast({ type: 'error', msg: `修改上线时间失败：${errBody || '未知错误'}` })
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" style={{ background: 'rgba(0, 0, 0, 0.32)', backdropFilter: 'blur(4px)' }} onClick={() => !saving && onClose()}>
      <div className="max-w-sm w-full p-6 animate-slide-up" style={{ background: 'var(--background)', borderRadius: 'calc(var(--radius) * 1.1)', boxShadow: 'var(--shadow-apple-xl)' }} onClick={e => e.stopPropagation()}>
        <div className="flex items-center gap-3 mb-4">
          <Calendar className="w-5 h-5" style={{ color: 'var(--brand-500)' }} />
          <div>
            <h2 className="text-lg font-semibold" style={{ color: 'var(--foreground)' }}>修改上线时间</h2>
            <p className="text-xs truncate max-w-[240px]" style={{ color: 'var(--foreground-tertiary)' }}>{entry.title}</p>
          </div>
        </div>
        <div className="space-y-3">
          <div>
            <label className="label">上线时间</label>
            <input
              type="datetime-local"
              value={editValue}
              onChange={e => setEditValue(e.target.value)}
              className="input-field text-sm"
              autoFocus
            />
            <p className="text-xs mt-1" style={{ color: 'var(--foreground-tertiary)' }}>
              用于知识库检索时按上线时间排序，同一功能多个版本时以靠后时间为准
            </p>
          </div>
        </div>
        <div className="flex justify-end gap-3 mt-5">
          <button onClick={onClose} className="btn-secondary" disabled={saving}>
            <X className="w-4 h-4" /> 取消
          </button>
          <button onClick={handleSave} disabled={!editValue || saving} className="btn-primary">
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />} 确认修改
          </button>
        </div>
      </div>
    </div>
  )
})

export default function KnowledgeBase() {
  const { selectedProductId, products } = useProduct()

  // 项目列表 + 项目详情状态
  const [kbProjects, setKbProjects] = useState<KbProject[]>([])
  const [selectedKbProject, setSelectedKbProject] = useState<KbProject | null>(null)
  const [projectEntries, setProjectEntries] = useState<{ requirements: any[], test_cases: any[] }>({ requirements: [], test_cases: [] })
  const [loadingProjects, setLoadingProjects] = useState(false)
  const [loadingEntries, setLoadingEntries] = useState(false)
  const [detailTab, setDetailTab] = useState<'requirements' | 'test_cases'>('requirements')

  // 向量化状态索引：listKbEntriesByProject 不返回向量化字段，需从 knowledge.listEntries 补字段
  const [kbEntryIndex, setKbEntryIndex] = useState<Map<string, KbEntry>>(new Map())

  const [toast, setToast] = useState<{ type: 'success' | 'error' | 'warning'; msg: string } | null>(null)
  const [modelStatus, setModelStatus] = useState<KbModelStatus | null>(null)
  const [revectorizingIds, setRevectorizingIds] = useState<Set<string>>(new Set())
  const [batchBusy, setBatchBusy] = useState(false)

  // 修改上线时间弹窗 — 只保留 entry 引用（日期/保存状态由 memo 组件内部管理）
  const [editingEntry, setEditingEntry] = useState<KbEntry | null>(null)

  // 加载已归档项目列表
  const loadProjects = async () => {
    if (!selectedProductId) { setKbProjects([]); return }
    setLoadingProjects(true)
    try {
      const data = await prodApi.listKbProjects(selectedProductId)
      setKbProjects(data || [])
    } catch (e: any) {
      setKbProjects([])
      setToast({ type: 'error', msg: `加载项目列表失败：${e?.message || '未知错误'}` })
    }
    setLoadingProjects(false)
  }

  // 加载项目内条目（并行获取向量化状态并合并补字段）
  const loadProjectEntries = async (project: KbProject) => {
    setLoadingEntries(true)
    try {
      const [entriesResp, kbResp] = await Promise.all([
        prodApi.listKbEntriesByProject(project.id),
        // listKbEntriesByProject 返回的需求不含向量化字段，需从 knowledge.listEntries 补全
        // v2.45 修复：传 limit=500 避免默认 100 条截断导致超出部分误显示"未向量化"
        knowledge.listEntries(selectedProductId || undefined, 'requirement', { limit: 500 }).catch(() => ({ items: [] as KbEntry[], total: 0 })),
      ])
      // 构建向量化状态索引（按需求 id 索引）
      const kbMap = new Map<string, KbEntry>()
      for (const e of (kbResp.items || [])) kbMap.set(e.id, e)
      setKbEntryIndex(kbMap)

      // 合并向量化字段到需求条目（适配为 KbEntry 兼容结构）
      const reqs = (entriesResp.requirements || []).map((req: any) => {
        const kb = kbMap.get(req.id)
        return {
          ...req,
          type: 'requirement' as const,
          product_id: kb?.product_id || selectedProductId || '',
          summary: kb?.summary,
          vectorized: kb?.vectorized ?? false,
          vectorization_status: kb?.vectorization_status ?? null,
          vectorization_error: kb?.vectorization_error ?? null,
        }
      })
      // 用例补"所属需求"标题（从已加载的需求列表按 req_id 匹配）+ type 字段
      const tcs = (entriesResp.test_cases || []).map((tc: any) => ({
        ...tc,
        type: 'test_case' as const,
        req_title: reqs.find((rr: any) => rr.id === tc.req_id)?.title || tc.req_id || '',
      }))
      setProjectEntries({ requirements: reqs, test_cases: tcs })
    } catch (e: any) {
      setProjectEntries({ requirements: [], test_cases: [] })
      setToast({ type: 'error', msg: `加载项目条目失败：${e?.message || '未知错误'}` })
    }
    setLoadingEntries(false)
  }

  const loadModelStatus = async () => {
    try {
      const s = await knowledge.modelStatus()
      setModelStatus(s)
    } catch {}
  }

  // 切换产品：加载项目列表，清空项目详情
  useEffect(() => {
    setSelectedKbProject(null)
    // v2.45 修复：切换产品时关闭修改上线时间弹窗，避免对旧产品条目执行保存
    setEditingEntry(null)
    setProjectEntries({ requirements: [], test_cases: [] })
    setKbEntryIndex(new Map())
    loadProjects()
  }, [selectedProductId])

  useEffect(() => { loadModelStatus() }, [])

  // 自动消失 toast
  useEffect(() => {
    if (!toast) return
    const t = setTimeout(() => setToast(null), 5000)
    return () => clearTimeout(t)
  }, [toast])

  // 进入项目详情
  const handleEnterProject = (project: KbProject) => {
    setSelectedKbProject(project)
    setDetailTab('requirements')
    loadProjectEntries(project)
  }

  // 返回项目列表
  const handleBackToProjects = () => {
    setSelectedKbProject(null)
    setProjectEntries({ requirements: [], test_cases: [] })
    setKbEntryIndex(new Map())
    // v2.45 修复：删除条目后返回列表时刷新计数
    loadProjects()
  }

  // 删除知识库条目（需求或用例）
  const handleDeleteEntry = async (entry: any, entryType: 'requirement' | 'test_case') => {
    if (!confirm(`确定从知识库删除「${entry.title}」吗？`)) return
    try {
      const r = await knowledge.deleteEntry(entry.id, entryType)
      if (entryType === 'requirement') {
        setProjectEntries(prev => ({ ...prev, requirements: prev.requirements.filter(e => e.id !== entry.id) }))
      } else {
        setProjectEntries(prev => ({ ...prev, test_cases: prev.test_cases.filter(e => e.id !== entry.id) }))
      }
      if (r.db_deleted && r.kb_deleted) {
        setToast({ type: 'success', msg: `已删除「${entry.title}」（关系库 + 向量库）` })
      } else if (r.db_deleted && !r.kb_deleted) {
        setToast({ type: 'warning', msg: `关系库已删除，但向量库删除失败：${r.kb_error || '请检查 Milvus 状态'}` })
      } else {
        setToast({ type: 'success', msg: `已删除「${entry.title}」` })
      }
    } catch (e: any) {
      const errBody = e?.data?.detail || e?.detail || e?.message || ''
      setToast({ type: 'error', msg: `删除失败：${errBody || '未知错误（可能权限不足或网络异常）'}` })
    }
  }

  // 重新向量化（仅需求）
  const handleRevectorize = async (entry: any) => {
    setRevectorizingIds(prev => new Set(prev).add(entry.id))
    try {
      const r = await knowledge.revectorize(entry.id)
      if (r.queued) {
        setToast({ type: 'success', msg: `已派发「${entry.title}」的向量化任务，稍后刷新查看` })
        // 乐观更新为 processing
        setProjectEntries(prev => ({
          ...prev,
          requirements: prev.requirements.map(e => e.id === entry.id ? { ...e, vectorization_status: 'processing' } : e),
        }))
      } else {
        setToast({ type: 'error', msg: `派发失败：${r.message || 'Celery 未运行或嵌入模型未就绪'}` })
      }
    } catch (e: any) {
      const errBody = e?.data?.detail || e?.detail || e?.message || ''
      setToast({ type: 'error', msg: `重新向量化失败：${errBody || '未知错误'}` })
    } finally {
      setRevectorizingIds(prev => { const n = new Set(prev); n.delete(entry.id); return n })
    }
  }

  const handleRevectorizeMissing = async () => {
    setBatchBusy(true)
    try {
      const r = await knowledge.revectorizeMissing()
      setToast({
        type: r.failed > 0 ? 'warning' : 'success',
        msg: `已补向量化 ${r.queued} 条，失败 ${r.failed} 条（共缺失 ${r.missing} 条）`,
      })
      // 刷新当前视图
      if (selectedKbProject) {
        await loadProjectEntries(selectedKbProject)
      } else {
        await loadProjects()
      }
    } catch (e: any) {
      const errBody = e?.data?.detail || e?.detail || e?.message || ''
      setToast({ type: 'error', msg: `批量补向量化失败：${errBody || '未知错误'}` })
    } finally {
      setBatchBusy(false)
    }
  }

  // 修改上线时间：将项目内需求适配为 KbEntry 以复用 GoLiveEditModal
  const handleEditGoLive = (req: any) => {
    const entry: KbEntry = {
      id: req.id,
      title: req.title || req.id,
      type: 'requirement',
      product_id: req.product_id || selectedProductId || '',
      go_live_date: req.go_live_date,
      priority: req.priority,
      summary: req.summary,
      vectorized: req.vectorized,
      vectorization_status: req.vectorization_status,
    }
    setEditingEntry(entry)
  }

  const onSavedGoLive = useCallback((id: string, newDate: string) => {
    setProjectEntries(prev => ({
      ...prev,
      requirements: prev.requirements.map(r => r.id === id ? { ...r, go_live_date: newDate } : r),
    }))
  }, [])

  const productName = selectedProductId
    ? (products.find(p => p.id === selectedProductId)?.name || selectedProductId)
    : ''

  return (
    <div className="space-y-6">
      <div>
        <h1 className="page-title">知识库</h1>
        <p className="text-sm mt-1" style={{ color: 'var(--foreground-tertiary)' }}>已归档需求与测试用例管理 · 显示向量库与关系库双状态</p>
      </div>

      {modelStatus && !modelStatus.ready && (
        <div className="p-3 text-sm flex items-start gap-2" style={{ borderRadius: 'calc(var(--radius) * 0.6)', background: 'var(--state-warning-surface)', color: 'var(--state-warning)' }}>
          <AlertTriangle className="w-4 h-4 mt-0.5 flex-shrink-0" />
          <div className="flex-1">
            <p className="font-medium">嵌入模型未就绪，已归档需求将无法向量化</p>
            <p className="mt-0.5 text-xs leading-relaxed">{modelStatus.error || '请确认容器已挂载本地模型目录，且嵌入模型权重文件完整。'}</p>
          </div>
        </div>
      )}

      {/* Toast 提示 */}
      {toast && (
        <div className="p-3 flex items-start gap-2 text-sm" style={{
          borderRadius: 'calc(var(--radius) * 0.6)',
          background: toast.type === 'success' ? 'var(--state-success-surface)' : toast.type === 'warning' ? 'var(--state-warning-surface)' : 'var(--state-error-surface)',
          color: toast.type === 'success' ? 'var(--state-success)' : toast.type === 'warning' ? 'var(--state-warning)' : 'var(--state-error)',
        }}>
          {toast.type === 'success' ? <CheckCircle2 className="w-4 h-4 mt-0.5 flex-shrink-0" /> :
           toast.type === 'warning' ? <AlertTriangle className="w-4 h-4 mt-0.5 flex-shrink-0" /> :
           <AlertTriangle className="w-4 h-4 mt-0.5 flex-shrink-0" />}
          <span className="flex-1">{toast.msg}</span>
          <button onClick={() => setToast(null)} className="text-current opacity-60 hover:opacity-100">×</button>
        </div>
      )}

      {!selectedKbProject ? (
        // ====================================================================
        // 顶层视图：已归档项目列表
        // ====================================================================
        <div className="card">
          <div className="flex items-center justify-between mb-4">
            <h2 className="section-title flex items-center gap-2">
              <Archive className="w-4 h-4" style={{ color: 'var(--brand-500)' }} /> 已归档项目
            </h2>
            <div className="flex items-center gap-2">
              <button onClick={loadProjects} disabled={loadingProjects} className="btn-ghost text-xs">
                <Loader2 className={`w-3 h-3 ${loadingProjects ? 'animate-spin' : ''}`} /> 刷新
              </button>
              <button onClick={handleRevectorizeMissing} disabled={batchBusy} className="btn-ghost text-xs">
                <RefreshCw className={`w-3 h-3 ${batchBusy ? 'animate-spin' : ''}`} /> 全部补向量化
              </button>
            </div>
          </div>

          <div className="flex items-center gap-3 mb-4 flex-wrap">
            <span className="text-sm" style={{ color: 'var(--foreground-tertiary)' }}>
              {selectedProductId
                ? `产品：${productName}`
                : '提示：请在顶部栏选择产品'}
            </span>
            {kbProjects.length > 0 && <span className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>共 {kbProjects.length} 个项目</span>}
          </div>

          {loadingProjects && kbProjects.length === 0 ? (
            <LoadingSpinner text="加载项目列表..." />
          ) : kbProjects.length === 0 ? (
            <div className="text-center py-8" style={{ color: 'var(--foreground-tertiary)' }}>
              <Archive className="w-8 h-8 mx-auto mb-2 opacity-50" />
              <p className="text-sm">暂无已归档项目</p>
              <p className="text-xs mt-1">项目归档后其需求与用例将自动导入知识库</p>
            </div>
          ) : (
            <div className="space-y-2">
              {kbProjects.map(project => (
                <button
                  key={project.id}
                  onClick={() => handleEnterProject(project)}
                  className="w-full text-left p-4 transition-colors flex items-center justify-between gap-3"
                  style={{ background: 'var(--background)', borderRadius: 'calc(var(--radius) * 0.6)', border: '1px solid var(--border)' }}
                  onMouseEnter={e => (e.currentTarget.style.background = 'var(--background-muted)')}
                  onMouseLeave={e => (e.currentTarget.style.background = 'var(--background)')}
                >
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <p className="text-sm font-medium truncate" style={{ color: 'var(--foreground)' }}>{project.name || project.id}</p>
                      {project.version && (
                        <span className="text-[10px] px-1.5 py-0.5 rounded font-medium" style={{ background: 'var(--brand-500)', color: '#ffffff' }}>
                          v{project.version}
                        </span>
                      )}
                      {/* v2.45 修复：归档徽标用中性灰，避免绿色暗示"成功/正向"（与 DependencyGraph FP 归档样式一致） */}
                      <span className="text-[10px] px-1.5 py-0.5 rounded font-medium flex items-center gap-0.5" style={{ background: 'var(--background-muted)', color: 'var(--foreground-tertiary)' }}>
                        <CheckCircle2 className="w-2.5 h-2.5" /> 已归档
                      </span>
                      {project.requirement_count > 0 && (
                        <span className="text-[10px] px-1.5 py-0.5 rounded font-medium flex items-center gap-0.5" style={{ background: 'var(--background-muted)', color: 'var(--foreground-secondary)' }}>
                          <FileText className="w-2.5 h-2.5" /> 需求 {project.requirement_count}
                        </span>
                      )}
                      {project.test_case_count > 0 && (
                        <span className="text-[10px] px-1.5 py-0.5 rounded font-medium flex items-center gap-0.5" style={{ background: 'var(--background-muted)', color: 'var(--foreground-secondary)' }}>
                          <TestTube className="w-2.5 h-2.5" /> 用例 {project.test_case_count}
                        </span>
                      )}
                    </div>
                    {project.description && (
                      <p className="text-xs mt-1 truncate" style={{ color: 'var(--foreground-tertiary)' }}>{project.description}</p>
                    )}
                    <div className="flex items-center gap-3 mt-1 text-[11px] flex-wrap" style={{ color: 'var(--foreground-tertiary)' }}>
                      {project.go_live_date && (
                        <span className="flex items-center gap-0.5" style={{ color: 'var(--brand-500)' }}>
                          <Calendar className="w-2.5 h-2.5" /> 上线 {formatDate(project.go_live_date)}
                        </span>
                      )}
                      {project.archived_at && (
                        <span>归档 {formatDateTime(project.archived_at)}</span>
                      )}
                    </div>
                  </div>
                  <ChevronRight className="w-4 h-4 flex-shrink-0" style={{ color: 'var(--foreground-tertiary)' }} />
                </button>
              ))}
            </div>
          )}
        </div>
      ) : (
        // ====================================================================
        // 项目详情视图：需求列表 + 用例列表
        // ====================================================================
        <div className="card">
          {/* 返回按钮 + 项目信息摘要 */}
          <button
            onClick={handleBackToProjects}
            className="btn-ghost text-xs mb-3"
          >
            <ArrowLeft className="w-3.5 h-3.5" /> 返回项目列表
          </button>

          <div className="mb-4 pb-4" style={{ borderBottom: '1px solid var(--border)' }}>
            <div className="flex items-center gap-2 flex-wrap">
              <h2 className="section-title flex items-center gap-2">
                <Archive className="w-4 h-4" style={{ color: 'var(--brand-500)' }} /> {selectedKbProject.name}
              </h2>
              {selectedKbProject.version && (
                <span className="text-[11px] px-1.5 py-0.5 rounded font-medium" style={{ background: 'var(--brand-500)', color: '#ffffff' }}>
                  v{selectedKbProject.version}
                </span>
              )}
            </div>
            {selectedKbProject.description && (
              <p className="text-xs mt-1" style={{ color: 'var(--foreground-tertiary)' }}>{selectedKbProject.description}</p>
            )}
            <div className="flex items-center gap-3 mt-1.5 text-[11px] flex-wrap" style={{ color: 'var(--foreground-tertiary)' }}>
              {selectedKbProject.go_live_date && (
                <span className="flex items-center gap-0.5" style={{ color: 'var(--brand-500)' }}>
                  <Calendar className="w-2.5 h-2.5" /> 上线时间: {formatDateTime(selectedKbProject.go_live_date)}
                </span>
              )}
              {selectedKbProject.archived_at && (
                <span>归档时间: {formatDateTime(selectedKbProject.archived_at)}</span>
              )}
            </div>
          </div>

          {/* Tab 切换 */}
          <div className="flex items-center gap-1 mb-4">
            <button
              onClick={() => setDetailTab('requirements')}
              className="px-3 py-1.5 text-sm rounded-t transition-colors flex items-center gap-1.5"
              style={detailTab === 'requirements'
                ? { background: 'var(--background)', color: 'var(--brand-500)', borderBottom: '2px solid var(--brand-500)' }
                : { background: 'transparent', color: 'var(--foreground-tertiary)' }}
            >
              <FileText className="w-3.5 h-3.5" /> 需求 ({projectEntries.requirements.length})
            </button>
            <button
              onClick={() => setDetailTab('test_cases')}
              className="px-3 py-1.5 text-sm rounded-t transition-colors flex items-center gap-1.5"
              style={detailTab === 'test_cases'
                ? { background: 'var(--background)', color: 'var(--brand-500)', borderBottom: '2px solid var(--brand-500)' }
                : { background: 'transparent', color: 'var(--foreground-tertiary)' }}
            >
              <TestTube className="w-3.5 h-3.5" /> 用例 ({projectEntries.test_cases.length})
            </button>
          </div>

          {detailTab === 'requirements' ? (
            <>
              {/* 图例 */}
              <div className="flex items-center gap-4 mb-3 text-[11px] flex-wrap" style={{ color: 'var(--foreground-tertiary)' }}>
                <span className="flex items-center gap-1"><span className="inline-block w-2 h-2 rounded-full" style={{ background: 'var(--state-success)' }} /> 已向量化</span>
                <span className="flex items-center gap-1"><span className="inline-block w-2 h-2 rounded-full" style={{ background: 'var(--brand-500)' }} /> 向量化中</span>
                <span className="flex items-center gap-1"><span className="inline-block w-2 h-2 rounded-full" style={{ background: 'var(--state-error)' }} /> 向量化失败</span>
                <span className="flex items-center gap-1"><span className="inline-block w-2 h-2 rounded-full" style={{ background: 'var(--state-warning)' }} /> 未向量化</span>
              </div>

              {loadingEntries && projectEntries.requirements.length === 0 ? (
                <div className="space-y-2">{[1, 2, 3].map(i => <div key={i} className="skeleton h-14" />)}</div>
              ) : projectEntries.requirements.length === 0 ? (
                <div className="text-center py-8" style={{ color: 'var(--foreground-tertiary)' }}>
                  <FileText className="w-8 h-8 mx-auto mb-2 opacity-50" />
                  <p className="text-sm">该项目暂无需求</p>
                </div>
              ) : (
                <div className="space-y-2">
                  {projectEntries.requirements.map((entry: any) => (
                    <div key={entry.id} className="flex items-center justify-between p-3 transition-colors" style={{ background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.6)' }}>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <FileText className="w-4 h-4 flex-shrink-0" style={{ color: 'var(--brand-500)' }} />
                          <p className="text-sm font-medium truncate" style={{ color: 'var(--foreground-secondary)' }}>{entry.title || entry.id}</p>
                          {/* 状态徽标：v2.45 修复，归档状态用中性灰，避免绿色语义错误 */}
                          <span className="text-[10px] px-1.5 py-0.5 rounded font-medium" style={{ background: 'var(--background)', color: 'var(--foreground-tertiary)' }}>
                            {entry.status || '已归档'}
                          </span>
                          {entry.priority && (
                            <span className="text-[10px] px-1.5 py-0.5 rounded font-medium" style={{ background: 'var(--background)', color: 'var(--foreground-secondary)' }}>
                              {entry.priority}
                            </span>
                          )}
                          {/* 向量化状态徽标（从 kbEntryIndex 补字段后展示） */}
                          {entry.vectorization_status === 'processing' ? (
                            <span className="text-[10px] px-1.5 py-0.5 rounded font-medium flex items-center gap-0.5" style={{ background: 'rgba(0, 122, 255, 0.08)', color: 'var(--brand-500)' }} title="Celery 任务进行中，写入 Milvus…">
                              <Loader2 className="w-2.5 h-2.5 animate-spin" /> 向量化中
                            </span>
                          ) : entry.vectorized ? (
                            <span className="text-[10px] px-1.5 py-0.5 rounded font-medium flex items-center gap-0.5" style={{ background: 'var(--state-success-surface)', color: 'var(--state-success)' }}>
                              <CheckCircle2 className="w-2.5 h-2.5" /> 已向量化
                            </span>
                          ) : entry.vectorization_status === 'failed' ? (
                            <span className="text-[10px] px-1.5 py-0.5 rounded font-medium flex items-center gap-0.5" style={{ background: 'var(--state-error-surface)', color: 'var(--state-error)' }} title={entry.vectorization_error || '向量化任务失败（Celery 重试耗尽）'}>
                              <XCircle className="w-2.5 h-2.5" /> 向量化失败
                            </span>
                          ) : (
                            <span className="text-[10px] px-1.5 py-0.5 rounded font-medium flex items-center gap-0.5" style={{ background: 'var(--state-warning-surface)', color: 'var(--state-warning)' }} title="需求已归档但向量库中未找到向量，可能归档时写入失败">
                              <AlertTriangle className="w-2.5 h-2.5" /> 未向量化
                            </span>
                          )}
                          {/* 重新向量化按钮 */}
                          {!entry.vectorized && entry.vectorization_status !== 'processing' && (
                            <button
                              onClick={() => handleRevectorize(entry)}
                              disabled={revectorizingIds.has(entry.id)}
                              className="text-[10px] px-1.5 py-0.5 rounded font-medium disabled:opacity-50 flex items-center gap-0.5"
                              style={{ background: 'var(--background)', color: 'var(--foreground-secondary)' }}
                              title="重新派发向量化任务"
                            >
                              {revectorizingIds.has(entry.id) ? <Loader2 className="w-2.5 h-2.5 animate-spin" /> : <RefreshCw className="w-2.5 h-2.5" />}
                              重新向量化
                            </button>
                          )}
                        </div>
                        {entry.go_live_date && (
                          <p className="text-xs mt-0.5 flex items-center gap-0.5" style={{ color: 'var(--brand-500)' }}>
                            <Calendar className="w-2.5 h-2.5" /> 上线时间: {formatDateTime(entry.go_live_date)}
                          </p>
                        )}
                      </div>
                      <div className="flex items-center gap-1 flex-shrink-0 ml-2">
                        <button
                          onClick={() => handleEditGoLive(entry)}
                          className="p-1.5 transition-all"
                          style={{ borderRadius: 'calc(var(--radius) * 0.5)', color: 'var(--foreground-tertiary)' }}
                          title="修改上线时间"
                        >
                          <Calendar className="w-3.5 h-3.5" />
                        </button>
                        <button onClick={() => handleDeleteEntry(entry, 'requirement')}
                          className="p-1.5 transition-all"
                          style={{ borderRadius: 'calc(var(--radius) * 0.5)', color: 'var(--foreground-tertiary)' }}
                          title="从知识库删除">
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </>
          ) : (
            // 用例列表
            loadingEntries && projectEntries.test_cases.length === 0 ? (
              <div className="space-y-2">{[1, 2, 3].map(i => <div key={i} className="skeleton h-14" />)}</div>
            ) : projectEntries.test_cases.length === 0 ? (
              <div className="text-center py-8" style={{ color: 'var(--foreground-tertiary)' }}>
                <TestTube className="w-8 h-8 mx-auto mb-2 opacity-50" />
                <p className="text-sm">该项目暂无用例</p>
              </div>
            ) : (
              <div className="space-y-2">
                {projectEntries.test_cases.map((entry: any) => (
                  <div key={entry.id} className="flex items-center justify-between p-3 transition-colors" style={{ background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.6)' }}>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <TestTube className="w-4 h-4 text-accent-500 flex-shrink-0" />
                        <p className="text-sm font-medium truncate" style={{ color: 'var(--foreground-secondary)' }}>{entry.title || entry.id}</p>
                        <span className="text-[10px] px-1.5 py-0.5 rounded font-medium bg-accent-50 text-accent-600">用例</span>
                        {entry.priority && (
                          <span className="text-[10px] px-1.5 py-0.5 rounded font-medium" style={{ background: 'var(--background)', color: 'var(--foreground-secondary)' }}>
                            {entry.priority}
                          </span>
                        )}
                      </div>
                      {entry.req_title && (
                        <p className="text-xs mt-0.5 truncate" style={{ color: 'var(--foreground-tertiary)' }}>
                          所属需求：{entry.req_title}
                        </p>
                      )}
                    </div>
                    <div className="flex items-center gap-1 flex-shrink-0 ml-2">
                      <button onClick={() => handleDeleteEntry(entry, 'test_case')}
                        className="p-1.5 transition-all"
                        style={{ borderRadius: 'calc(var(--radius) * 0.5)', color: 'var(--foreground-tertiary)' }}
                        title="从知识库删除">
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )
          )}
        </div>
      )}

      {/* 修改上线时间弹窗（React.memo 隔离，避免 onChange 触发列表重渲染） */}
      {editingEntry && (
        <GoLiveEditModal
          entry={editingEntry}
          onClose={() => setEditingEntry(null)}
          onSaved={onSavedGoLive}
          toast={t => setToast(t)}
        />
      )}
    </div>
  )
}
