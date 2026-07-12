import { useState, useEffect } from 'react'
import { Database, FileText, TestTube, Trash2, Filter, Loader2, CheckCircle2, AlertTriangle, Clock } from 'lucide-react'
import { knowledge } from '../api/client'
import type { KbEntry } from '../types'
import { useProduct } from '../store/ProductContext'
import LoadingSpinner from '../components/LoadingSpinner'

function formatDate(iso?: string): string {
  if (!iso) return ''
  try {
    const d = new Date(iso)
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  } catch {
    return iso.slice(0, 10)
  }
}

export default function KnowledgeBase() {
  const { selectedProductId, products } = useProduct()
  const [entries, setEntries] = useState<KbEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [filterType, setFilterType] = useState('')
  const [toast, setToast] = useState<{ type: 'success' | 'error' | 'warning'; msg: string } | null>(null)

  const loadEntries = async () => {
    setLoading(true)
    try {
      const r = await knowledge.listEntries(selectedProductId || undefined, filterType || undefined)
      setEntries(r.items || [])
    } catch {}
    setLoading(false)
  }

  useEffect(() => { loadEntries() }, [selectedProductId, filterType])

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
      // 精确展示删除结果
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

  if (loading && entries.length === 0) return <LoadingSpinner text="加载知识库..." />

  return (
    <div className="space-y-6">
      <div>
        <h1 className="page-title">知识库</h1>
        <p className="text-sm text-slate-500 mt-1">已归档需求与测试用例管理 · 显示向量库与关系库双状态</p>
      </div>

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
          <button onClick={loadEntries} disabled={loading} className="btn-ghost text-xs">
            <Loader2 className={`w-3 h-3 ${loading ? 'animate-spin' : ''}`} /> 刷新
          </button>
        </div>

        <div className="flex items-center gap-3 mb-4 flex-wrap">
          <Filter className="w-4 h-4 text-slate-400" />
          <span className="text-sm text-slate-500">
            {selectedProductId
              ? `产品：${products.find(p => p.id === selectedProductId)?.name || selectedProductId}`
              : '提示：请在顶部栏选择产品'}
          </span>
          <select value={filterType} onChange={e => setFilterType(e.target.value)}
            className="select-field text-sm max-w-[140px]">
            <option value="">全部类型</option>
            <option value="requirement">需求</option>
            <option value="test_case">测试用例</option>
          </select>
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
                  </div>
                  <p className="text-xs text-slate-400 mt-0.5 truncate flex items-center gap-1">
                    {entry.product_id && (products.find(p => p.id === entry.product_id)?.name || entry.product_id)}
                    {entry.archived_at && (
                      <span className="flex items-center gap-0.5">
                        · <Clock className="w-2.5 h-2.5" /> {formatDate(entry.archived_at)} 归档
                      </span>
                    )}
                    {entry.summary ? ` · ${entry.summary}` : ''}
                  </p>
                </div>
                <button onClick={() => handleDeleteEntry(entry)}
                  className="p-1.5 rounded-lg text-slate-400 hover:text-red-500 hover:bg-red-50 transition-all flex-shrink-0 ml-2"
                  title="从知识库删除">
                  <Trash2 className="w-3.5 h-3.5" />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
