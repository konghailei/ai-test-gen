import { useState, useEffect } from 'react'
import { Database, FileText, TestTube, Trash2, Filter, Loader2 } from 'lucide-react'
import { knowledge } from '../api/client'
import type { KbEntry } from '../types'
import { useProduct } from '../store/ProductContext'
import LoadingSpinner from '../components/LoadingSpinner'

export default function KnowledgeBase() {
  const { selectedProductId, products } = useProduct()
  const [entries, setEntries] = useState<KbEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [filterType, setFilterType] = useState('')

  const loadEntries = async () => {
    setLoading(true)
    try {
      const r = await knowledge.listEntries(selectedProductId || undefined, filterType || undefined)
      setEntries(r.items || [])
    } catch {}
    setLoading(false)
  }

  useEffect(() => { loadEntries() }, [selectedProductId, filterType])

  const handleDeleteEntry = async (entry: KbEntry) => {
    if (!confirm(`确定从知识库删除「${entry.title}」吗？`)) return
    try {
      await knowledge.deleteEntry(entry.id, entry.type)
      setEntries(prev => prev.filter(e => e.id !== entry.id))
    } catch (e: any) {
      alert('删除失败: ' + (e.message || ''))
    }
  }

  if (loading && entries.length === 0) return <LoadingSpinner text="加载知识库..." />

  return (
    <div className="space-y-6">
      <div>
        <h1 className="page-title">知识库</h1>
        <p className="text-sm text-slate-500 mt-1">已归档需求与测试用例管理</p>
      </div>

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
                  <div className="flex items-center gap-2">
                    {entry.type === 'requirement'
                      ? <FileText className="w-4 h-4 text-primary-500 flex-shrink-0" />
                      : <TestTube className="w-4 h-4 text-accent-500 flex-shrink-0" />}
                    <p className="text-sm font-medium text-slate-700 truncate">{entry.title || entry.id}</p>
                    <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${
                      entry.type === 'requirement' ? 'bg-primary-50 text-primary-600' : 'bg-accent-50 text-accent-600'
                    }`}>
                      {entry.type === 'requirement' ? '需求' : '用例'}
                    </span>
                  </div>
                  <p className="text-xs text-slate-400 mt-0.5 truncate">
                    {entry.product_id && (products.find(p => p.id === entry.product_id)?.name || entry.product_id)}
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
