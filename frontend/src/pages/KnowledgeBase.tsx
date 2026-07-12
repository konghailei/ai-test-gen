import { useState, useEffect } from 'react'
import { Database, FileText, TestTube, Trash2, Filter, Loader2, CheckCircle2, AlertTriangle, Clock, RefreshCw, Search, Calendar, X, Save } from 'lucide-react'
import { knowledge, requirements as reqApi } from '../api/client'
import type { KbEntry, KbModelStatus } from '../types'
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

export default function KnowledgeBase() {
  const { selectedProductId, products } = useProduct()
  const [entries, setEntries] = useState<KbEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [filterType, setFilterType] = useState('')
  const [filterVectorized, setFilterVectorized] = useState('')
  const [searchKeyword, setSearchKeyword] = useState('')
  const [toast, setToast] = useState<{ type: 'success' | 'error' | 'warning'; msg: string } | null>(null)
  const [modelStatus, setModelStatus] = useState<KbModelStatus | null>(null)
  const [revectorizingIds, setRevectorizingIds] = useState<Set<string>>(new Set())
  const [batchBusy, setBatchBusy] = useState(false)

  // 修改上线时间弹窗
  const [editingEntry, setEditingEntry] = useState<KbEntry | null>(null)
  const [editGoLiveDate, setEditGoLiveDate] = useState('')
  const [savingGoLive, setSavingGoLive] = useState(false)

  const loadEntries = async () => {
    setLoading(true)
    try {
      const r = await knowledge.listEntries(
        selectedProductId || undefined,
        filterType || undefined,
        { search: searchKeyword || undefined, vectorized: filterVectorized || undefined },
      )
      setEntries(r.items || [])
    } catch {}
    setLoading(false)
  }

  const loadModelStatus = async () => {
    try {
      const s = await knowledge.modelStatus()
      setModelStatus(s)
    } catch {}
  }

  useEffect(() => { loadEntries() }, [selectedProductId, filterType, filterVectorized])
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

  const handleEditGoLive = (entry: KbEntry) => {
    setEditingEntry(entry)
    setEditGoLiveDate(isoToDatetimeLocal(entry.go_live_date))
  }

  const handleSaveGoLive = async () => {
    if (!editingEntry || !editGoLiveDate) return
    setSavingGoLive(true)
    try {
      const isoDate = datetimeLocalToISO(editGoLiveDate)
      const r = await reqApi.updateGoLiveDate(editingEntry.id, isoDate)
      // 更新本地列表
      setEntries(prev => prev.map(e =>
        e.id === editingEntry.id ? { ...e, go_live_date: r.go_live_date } : e
      ))
      setToast({
        type: r.milvus_updated ? 'success' : 'warning',
        msg: r.milvus_updated
          ? `「${editingEntry.title}」上线时间已更新，向量库元数据已同步`
          : `「${editingEntry.title}」上线时间已更新，但向量库元数据同步失败`,
      })
      setEditingEntry(null)
      setEditGoLiveDate('')
    } catch (e: any) {
      const errBody = e?.data?.detail || e?.detail || e?.message || ''
      setToast({ type: 'error', msg: `修改上线时间失败：${errBody || '未知错误'}` })
    } finally {
      setSavingGoLive(false)
    }
  }

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
            className="select-field text-sm max-w-[120px]">
            <option value="">向量化状态</option>
            <option value="true">已向量化</option>
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

        {/* 图例 */}
        <div className="flex items-center gap-4 mb-3 text-[11px] text-slate-400 flex-wrap">
          <span className="flex items-center gap-1"><span className="inline-block w-2 h-2 rounded-full bg-emerald-400" /> 已归档（关系库）</span>
          <span className="flex items-center gap-1"><span className="inline-block w-2 h-2 rounded-full bg-blue-400" /> 已向量化（Milvus）</span>
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
                        {entry.vectorized ? (
                          <span className="text-[10px] px-1.5 py-0.5 rounded font-medium bg-blue-50 text-blue-600 flex items-center gap-0.5">
                            <CheckCircle2 className="w-2.5 h-2.5" /> 已向量化
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
                    {entry.type === 'requirement' && !entry.vectorized && (
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
                    {entry.archived_at && (
                      <span className="flex items-center gap-0.5">
                        · <Clock className="w-2.5 h-2.5" /> {formatDate(entry.archived_at)} 归档
                      </span>
                    )}
                    {entry.go_live_date && (
                      <span className="flex items-center gap-0.5 text-primary-500">
                        · <Calendar className="w-2.5 h-2.5" /> 上线时间: {formatDateTime(entry.go_live_date)}
                      </span>
                    )}
                    {entry.summary ? ` · ${entry.summary}` : ''}
                  </p>
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

      {/* 修改上线时间弹窗 */}
      {editingEntry && (
        <div className="fixed inset-0 z-50 bg-black/30 backdrop-blur-sm flex items-center justify-center p-4" onClick={() => !savingGoLive && setEditingEntry(null)}>
          <div className="bg-white rounded-2xl shadow-xl max-w-sm w-full p-6 animate-slide-up" onClick={e => e.stopPropagation()}>
            <div className="flex items-center gap-3 mb-4">
              <Calendar className="w-5 h-5 text-primary-500" />
              <div>
                <h2 className="text-lg font-semibold text-slate-800">修改上线时间</h2>
                <p className="text-xs text-slate-400 truncate max-w-[240px]">{editingEntry.title}</p>
              </div>
            </div>
            <div className="space-y-3">
              <div>
                <label className="label">上线时间</label>
                <input
                  type="datetime-local"
                  value={editGoLiveDate}
                  onChange={e => setEditGoLiveDate(e.target.value)}
                  className="input-field text-sm"
                  autoFocus
                />
                <p className="text-xs text-slate-400 mt-1">
                  用于知识库检索时按上线时间排序，同一功能多个版本时以靠后时间为准
                </p>
              </div>
            </div>
            <div className="flex justify-end gap-3 mt-5">
              <button onClick={() => { setEditingEntry(null); setEditGoLiveDate('') }}
                className="btn-secondary" disabled={savingGoLive}>
                <X className="w-4 h-4" /> 取消
              </button>
              <button onClick={handleSaveGoLive}
                disabled={!editGoLiveDate || savingGoLive} className="btn-primary">
                {savingGoLive ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />} 确认修改
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
