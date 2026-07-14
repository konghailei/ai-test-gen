import { useState, useEffect, useCallback, memo } from 'react'
import { Database, FileText, TestTube, Trash2, Filter, Loader2, CheckCircle2, AlertTriangle, RefreshCw, Search, Calendar, X, Save, Layers, XCircle } from 'lucide-react'
import { knowledge, requirements as reqApi, products as prodApi } from '../api/client'
import type { KbEntry, KbModelStatus, Module } from '../types'
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
    <div className="fixed inset-0 z-50 bg-black/30 backdrop-blur-sm flex items-center justify-center p-4" onClick={() => !saving && onClose()}>
      <div className="bg-white rounded-2xl shadow-xl max-w-sm w-full p-6 animate-slide-up" onClick={e => e.stopPropagation()}>
        <div className="flex items-center gap-3 mb-4">
          <Calendar className="w-5 h-5 text-primary-500" />
          <div>
            <h2 className="text-lg font-semibold text-slate-800">修改上线时间</h2>
            <p className="text-xs text-slate-400 truncate max-w-[240px]">{entry.title}</p>
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
            <p className="text-xs text-slate-400 mt-1">
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
  const [entries, setEntries] = useState<KbEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [filterType, setFilterType] = useState('')
  const [filterVectorized, setFilterVectorized] = useState('')
  const [filterModules, setFilterModules] = useState<string[]>([])
  const [availableModules, setAvailableModules] = useState<Module[]>([])
  const [searchKeyword, setSearchKeyword] = useState('')
  const [toast, setToast] = useState<{ type: 'success' | 'error' | 'warning'; msg: string } | null>(null)
  const [modelStatus, setModelStatus] = useState<KbModelStatus | null>(null)
  const [revectorizingIds, setRevectorizingIds] = useState<Set<string>>(new Set())
  const [batchBusy, setBatchBusy] = useState(false)

  // 修改上线时间弹窗 — 只保留 entry 引用（日期/保存状态由 memo 组件内部管理）
  const [editingEntry, setEditingEntry] = useState<KbEntry | null>(null)

  // 加载当前产品的模块列表（用于模块筛选 chips）
  useEffect(() => {
    setFilterModules([])  // 切换产品时清空已选模块
    if (!selectedProductId) { setAvailableModules([]); return }
    prodApi.listModules(selectedProductId).then((mods: Module[]) => {
      setAvailableModules(mods || [])
    }).catch(() => setAvailableModules([]))
  }, [selectedProductId])

  const loadEntries = async () => {
    setLoading(true)
    try {
      const r = await knowledge.listEntries(
        selectedProductId || undefined,
        filterType || undefined,
        {
          search: searchKeyword || undefined,
          vectorized: filterVectorized || undefined,
          modules: filterModules.length > 0 ? filterModules : undefined,
        },
      )
      setEntries(r.items || [])
    } catch {}
    setLoading(false)
  }

  // 切换模块筛选（多选，按模块 ID 匹配）
  const toggleFilterModule = (moduleId: string) => {
    setFilterModules(prev =>
      prev.includes(moduleId)
        ? prev.filter(m => m !== moduleId)
        : [...prev, moduleId]
    )
  }

  const loadModelStatus = async () => {
    try {
      const s = await knowledge.modelStatus()
      setModelStatus(s)
    } catch {}
  }

  useEffect(() => { loadEntries() }, [selectedProductId, filterType, filterVectorized, filterModules])
  useEffect(() => { loadModelStatus() }, [])
  // 搜索防抖
  useEffect(() => {
    const t = setTimeout(() => { loadEntries() }, 300)
    return () => clearTimeout(t)
  }, [searchKeyword])

  // 自动消失 toast
  useEffect(() => {
    if (!toast) return
    const t = setTimeout(() => setToast(null), 5000)
    return () => clearTimeout(t)
  }, [toast])

  const handleDeleteEntry = async (entry: KbEntry) => {
    if (!confirm(`确定从知识库删除「${entry.title}」吗？`)) return
    try {
      const r = await knowledge.deleteEntry(entry.id, entry.type)
      setEntries(prev => prev.filter(e => e.id !== entry.id))
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

  const handleRevectorize = async (entry: KbEntry) => {
    setRevectorizingIds(prev => new Set(prev).add(entry.id))
    try {
      const r = await knowledge.revectorize(entry.id)
      if (r.queued) {
        setToast({ type: 'success', msg: `已派发「${entry.title}」的向量化任务，稍后刷新查看` })
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
      await loadEntries()
    } catch (e: any) {
      const errBody = e?.data?.detail || e?.detail || e?.message || ''
      setToast({ type: 'error', msg: `批量补向量化失败：${errBody || '未知错误'}` })
    } finally {
      setBatchBusy(false)
    }
  }

  // 弹窗打开回调（memo 组件内部管理日期状态，仅保留 entry 引用控制显隐）
  const handleEditGoLive = (entry: KbEntry) => setEditingEntry(entry)

  const onSavedGoLive = useCallback((id: string, newDate: string) => {
    setEntries(prev => prev.map(e => e.id === id ? { ...e, go_live_date: newDate } : e))
  }, [])

  if (loading && entries.length === 0) return <LoadingSpinner text="加载知识库..." />

  return (
    <div className="space-y-6">
      <div>
        <h1 className="page-title">知识库</h1>
        <p className="text-sm text-slate-500 mt-1">已归档需求与测试用例管理 · 显示向量库与关系库双状态</p>
      </div>

      {modelStatus && !modelStatus.ready && (
        <div className="p-3 rounded-xl bg-amber-50 text-amber-700 text-sm flex items-start gap-2">
          <AlertTriangle className="w-4 h-4 mt-0.5 flex-shrink-0" />
          <div className="flex-1">
            <p className="font-medium">嵌入模型未就绪，已归档需求将无法向量化</p>
            <p className="mt-0.5 text-xs leading-relaxed">{modelStatus.error || '请确认容器已挂载本地模型目录，且嵌入模型权重文件完整。'}</p>
          </div>
        </div>
      )}

      {/* Toast 提示 */}
      {toast && (
        <div className={`p-3 rounded-xl flex items-start gap-2 text-sm ${
          toast.type === 'success' ? 'bg-success-50 text-success-700' :
          toast.type === 'warning' ? 'bg-amber-50 text-amber-700' :
          'bg-red-50 text-red-700'
        }`}>
          {toast.type === 'success' ? <CheckCircle2 className="w-4 h-4 mt-0.5 flex-shrink-0" /> :
           toast.type === 'warning' ? <AlertTriangle className="w-4 h-4 mt-0.5 flex-shrink-0" /> :
           <AlertTriangle className="w-4 h-4 mt-0.5 flex-shrink-0" />}
          <span className="flex-1">{toast.msg}</span>
          <button onClick={() => setToast(null)} className="text-current opacity-60 hover:opacity-100">×</button>
        </div>
      )}

      <div className="card">
        <div className="flex items-center justify-between mb-4">
          <h2 className="section-title flex items-center gap-2">
            <Database className="w-4 h-4 text-primary-500" /> 知识库条目
          </h2>
          <div className="flex items-center gap-2">
            <button onClick={loadEntries} disabled={loading} className="btn-ghost text-xs">
              <Loader2 className={`w-3 h-3 ${loading ? 'animate-spin' : ''}`} /> 刷新
            </button>
            <button onClick={handleRevectorizeMissing} disabled={batchBusy} className="btn-ghost text-xs">
              <RefreshCw className={`w-3 h-3 ${batchBusy ? 'animate-spin' : ''}`} /> 全部补向量化
            </button>
          </div>
        </div>

        {/* 查询条件 */}
        <div className="flex items-center gap-3 mb-4 flex-wrap">
          <Filter className="w-4 h-4 text-slate-400" />
          <span className="text-sm text-slate-500">
            {selectedProductId
              ? `产品：${products.find(p => p.id === selectedProductId)?.name || selectedProductId}`
              : '提示：请在顶部栏选择产品'}
          </span>
          <select value={filterType} onChange={e => setFilterType(e.target.value)}
            className="select-field text-sm max-w-[120px]">
            <option value="">全部类型</option>
            <option value="requirement">需求</option>
            <option value="test_case">测试用例</option>
          </select>
          <select value={filterVectorized} onChange={e => setFilterVectorized(e.target.value)}
            className="select-field text-sm max-w-[130px]">
            <option value="">向量化状态</option>
            <option value="true">已向量化</option>
            <option value="processing">向量化中</option>
            <option value="failed">向量化失败</option>
            <option value="false">未向量化</option>
          </select>
          <div className="relative flex-1 min-w-[180px] max-w-xs">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-400" />
            <input
              type="text"
              value={searchKeyword}
              onChange={e => setSearchKeyword(e.target.value)}
              placeholder="搜索标题..."
              className="input-field text-sm pl-8 py-1.5"
            />
          </div>
          {entries.length > 0 && <span className="text-xs text-slate-400">共 {entries.length} 条</span>}
        </div>

        {/* 模块筛选（多选 chips） */}
        {availableModules.length > 0 && (
          <div className="flex items-center gap-2 mb-4 flex-wrap">
            <Layers className="w-3.5 h-3.5 text-slate-400 flex-shrink-0" />
            <span className="text-xs text-slate-500">模块筛选：</span>
            {filterModules.length > 0 && (
              <button
                onClick={() => setFilterModules([])}
                className="px-2 py-0.5 text-[11px] rounded-full bg-slate-200 text-slate-600 hover:bg-slate-300"
              >
                全部
              </button>
            )}
            {availableModules.map(m => {
              const selected = filterModules.includes(m.id)
              return (
                <button
                  key={m.id}
                  onClick={() => toggleFilterModule(m.id)}
                  className={`px-2 py-0.5 text-[11px] rounded-full transition-colors ${
                    selected
                      ? 'bg-primary-500 text-white'
                      : 'bg-slate-100 text-slate-600 hover:bg-primary-50 hover:text-primary-600'
                  }`}
                >
                  {m.name}
                </button>
              )
            })}
            {filterModules.length > 0 && (
              <span className="text-[11px] text-primary-500">已选 {filterModules.length} 个</span>
            )}
          </div>
        )}

        {/* 图例 */}
        <div className="flex items-center gap-4 mb-3 text-[11px] text-slate-400 flex-wrap">
          <span className="flex items-center gap-1"><span className="inline-block w-2 h-2 rounded-full bg-emerald-400" /> 已归档</span>
          <span className="flex items-center gap-1"><span className="inline-block w-2 h-2 rounded-full bg-emerald-400" /> 已向量化</span>
          <span className="flex items-center gap-1"><span className="inline-block w-2 h-2 rounded-full bg-blue-400" /> 向量化中</span>
          <span className="flex items-center gap-1"><span className="inline-block w-2 h-2 rounded-full bg-red-400" /> 向量化失败</span>
          <span className="flex items-center gap-1"><span className="inline-block w-2 h-2 rounded-full bg-amber-400" /> 未向量化</span>
        </div>

        {loading ? (
          <div className="space-y-2">{[1,2,3].map(i => <div key={i} className="skeleton h-14" />)}</div>
        ) : entries.length === 0 ? (
          <div className="text-center py-8 text-slate-400">
            <Database className="w-8 h-8 mx-auto mb-2 opacity-50" />
            <p className="text-sm">知识库暂无数据</p>
            <p className="text-xs mt-1">归档需求后数据将自动导入知识库</p>
          </div>
        ) : (
          <div className="space-y-2">
            {entries.map(entry => (
              <div key={entry.id} className="flex items-center justify-between p-3 bg-slate-50/50 rounded-xl hover:bg-slate-50 transition-colors">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    {entry.type === 'requirement'
                      ? <FileText className="w-4 h-4 text-primary-500 flex-shrink-0" />
                      : <TestTube className="w-4 h-4 text-accent-500 flex-shrink-0" />}
                    <p className="text-sm font-medium text-slate-700 truncate">{entry.title || entry.id}</p>
                    {/* 双库状态徽标 */}
                    {entry.type === 'requirement' ? (
                      <>
                        <span className="text-[10px] px-1.5 py-0.5 rounded font-medium bg-emerald-50 text-emerald-600 flex items-center gap-0.5">
                          <CheckCircle2 className="w-2.5 h-2.5" /> 已归档
                        </span>
                        {entry.vectorization_status === 'processing' ? (
                          <span className="text-[10px] px-1.5 py-0.5 rounded font-medium bg-blue-50 text-blue-600 flex items-center gap-0.5" title="Celery 任务进行中，写入 Milvus…">
                            <Loader2 className="w-2.5 h-2.5 animate-spin" /> 向量化中
                          </span>
                        ) : entry.vectorized ? (
                          <span className="text-[10px] px-1.5 py-0.5 rounded font-medium bg-emerald-50 text-emerald-600 flex items-center gap-0.5">
                            <CheckCircle2 className="w-2.5 h-2.5" /> 已向量化
                          </span>
                        ) : entry.vectorization_status === 'failed' ? (
                          <span className="text-[10px] px-1.5 py-0.5 rounded font-medium bg-red-50 text-red-600 flex items-center gap-0.5" title={entry.vectorization_error || '向量化任务失败（Celery 重试耗尽）'}>
                            <XCircle className="w-2.5 h-2.5" /> 向量化失败
                          </span>
                        ) : (
                          <span className="text-[10px] px-1.5 py-0.5 rounded font-medium bg-amber-50 text-amber-600 flex items-center gap-0.5" title="需求已归档但向量库中未找到向量，可能归档时写入失败">
                            <AlertTriangle className="w-2.5 h-2.5" /> 未向量化
                          </span>
                        )}
                      </>
                    ) : (
                      <span className="text-[10px] px-1.5 py-0.5 rounded font-medium bg-accent-50 text-accent-600">用例</span>
                    )}
                    {entry.type === 'requirement' && !entry.vectorized && entry.vectorization_status !== 'processing' && (
                      <button
                        onClick={() => handleRevectorize(entry)}
                        disabled={revectorizingIds.has(entry.id)}
                        className="text-[10px] px-1.5 py-0.5 rounded font-medium bg-slate-100 text-slate-600 hover:bg-slate-200 disabled:opacity-50 flex items-center gap-0.5"
                        title="重新派发向量化任务"
                      >
                        {revectorizingIds.has(entry.id) ? <Loader2 className="w-2.5 h-2.5 animate-spin" /> : <RefreshCw className="w-2.5 h-2.5" />}
                        重新向量化
                      </button>
                    )}
                  </div>
                  <p className="text-xs text-slate-400 mt-0.5 truncate flex items-center gap-1">
                    {entry.product_id && (products.find(p => p.id === entry.product_id)?.name || entry.product_id)}
                    {entry.summary ? ` · ${entry.summary}` : ''}
                  </p>
                  {entry.go_live_date && (
                    <p className="text-xs text-primary-500 mt-0.5 flex items-center gap-0.5">
                      <Calendar className="w-2.5 h-2.5" /> 上线时间: {formatDateTime(entry.go_live_date)}
                    </p>
                  )}
                </div>
                <div className="flex items-center gap-1 flex-shrink-0 ml-2">
                  {entry.type === 'requirement' && (
                    <button
                      onClick={() => handleEditGoLive(entry)}
                      className="p-1.5 rounded-lg text-slate-400 hover:text-primary-500 hover:bg-primary-50 transition-all"
                      title="修改上线时间"
                    >
                      <Calendar className="w-3.5 h-3.5" />
                    </button>
                  )}
                  <button onClick={() => handleDeleteEntry(entry)}
                    className="p-1.5 rounded-lg text-slate-400 hover:text-red-500 hover:bg-red-50 transition-all"
                    title="从知识库删除">
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

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
