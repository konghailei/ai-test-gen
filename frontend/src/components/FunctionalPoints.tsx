import { useState, useEffect } from 'react'
import { Loader2, Trash2, Edit3, Save, X, Search, Layers, RefreshCw, AlertTriangle, Sparkles } from 'lucide-react'
import { requirements as reqApi, products as prodApi } from '../api/client'

interface Props {
  reqId: string
  productId?: string
  status?: string  // 需求状态：archived 时功能点锁定为只读
}

export default function FunctionalPoints({ reqId, productId, status }: Props) {
  const [fps, setFps] = useState<any[]>([])
  const [fpLoading, setFpLoading] = useState(false)
  const [resplitting, setResplitting] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editName, setEditName] = useState('')
  const [editModule, setEditModule] = useState('')
  const [modules, setModules] = useState<any[]>([])
  const [nameSearch, setNameSearch] = useState('')
  const [nameSuggestions, setNameSuggestions] = useState<any[]>([])
  const isArchived = status === 'archived'

  useEffect(() => { loadFP(); loadModules() }, [])

  const loadFP = async () => {
    setFpLoading(true)
    try { const r = await reqApi.fpList(reqId); setFps(r.items || []) } catch { }
    setFpLoading(false)
  }

  const loadModules = async () => {
    if (!productId) return
    try { setModules(await prodApi.listModules(productId) || []) } catch { }
  }

  // v1.5.1: "重新拆分" — 用一体式 prompt 重新生成 FP（需先删除旧的）
  const handleResplit = async () => {
    if (!confirm('重新拆分会先删除当前所有功能点，然后基于原始文档重新生成。确定继续？')) return
    setResplitting(true)
    try {
      await reqApi.fpSplit(reqId, [])  // 空 BP 列表=全量重新拆分
      await loadFP()
    } catch { }
    setResplitting(false)
  }

  const updateFP = async (fpId: string) => {
    await reqApi.fpUpdate(reqId, fpId, { name: editName || undefined, module_id: editModule || undefined })
    setFps(prev => prev.map(f => f.id === fpId ? { ...f, name: editName || f.name, module_id: editModule || f.module_id } : f))
    setEditingId(null)
  }

  const deleteFP = async (fpId: string) => {
    if (!confirm('确定删除该功能点？关联依赖会被一并删除。')) return
    await reqApi.fpDelete(reqId, fpId)
    setFps(prev => prev.filter(f => f.id !== fpId))
  }

  const searchNames = async (q: string) => {
    setNameSearch(q)
    if (q.length < 1) { setNameSuggestions([]); return }
    try {
      const r = await reqApi.fpSearchNames(reqId, q, productId)
      setNameSuggestions(r.names || [])
    } catch { setNameSuggestions([]) }
  }

  const FP_TYPE_LABELS: Record<string, string> = {
    workflow: '业务流程', constraint: '约束校验', calculation: '计算规则',
    api_behavior: 'API 行为', data_record: '数据记录', config: '配置项',
  }

  return (
    <div className="space-y-4">
      {/* 归档锁定提示 */}
      {isArchived && (
        <div className="flex items-center gap-2 p-3 bg-amber-50 border border-amber-200 rounded-xl text-sm text-amber-700">
          <AlertTriangle className="w-4 h-4 flex-shrink-0" />
          <span>此需求已归档，功能点已锁定为只读。</span>
        </div>
      )}

      {/* 操作栏 */}
      {!isArchived && (
        <div className="flex items-center gap-3">
          <button onClick={handleResplit} disabled={resplitting || fpLoading}
            className="btn-secondary text-sm flex items-center gap-1.5">
            {resplitting ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
            重新拆分
          </button>
          <span className="text-xs text-slate-400">
            上传需求时已自动拆分功能点。如不准确可重新拆分。
          </span>
        </div>
      )}

      {/* FP 列表 */}
      {fpLoading ? (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="w-6 h-6 text-primary-500 animate-spin" />
        </div>
      ) : fps.length > 0 ? (
        <div>
          <p className="text-xs text-slate-400 mb-2">
            <Sparkles className="w-3.5 h-3.5 inline mr-1 -mt-0.5" />
            共 {fps.length} 个功能点
          </p>
          <div className="space-y-2">
            {fps.map((fp: any) => (
              <div key={fp.id} className="p-3 bg-white border border-slate-200 rounded-xl">
                {editingId === fp.id ? (
                  <div className="space-y-2">
                    <div className="flex items-center gap-2">
                      <input value={editName} onChange={e => setEditName(e.target.value)}
                        className="input-field text-sm flex-1" placeholder="功能点名称" autoFocus />
                      <div className="relative">
                        <input value={nameSearch} onChange={e => searchNames(e.target.value)}
                          className="input-field text-sm w-40" placeholder="搜索已有名称" />
                        {nameSuggestions.length > 0 && (
                          <div className="absolute top-full left-0 right-0 bg-white border border-slate-200 rounded-lg shadow-lg z-10 max-h-32 overflow-y-auto">
                            {nameSuggestions.map((s: any) => (
                              <button key={s.id} onClick={() => { setEditName(s.name); setNameSuggestions([]); setNameSearch('') }}
                                className="w-full text-left px-3 py-1.5 text-xs hover:bg-primary-50 text-slate-700">
                                {s.name}
                              </button>
                            ))}
                          </div>
                        )}
                      </div>
                      <select value={editModule} onChange={e => setEditModule(e.target.value)}
                        className="select-field text-sm max-w-[140px]">
                        <option value="">选择模块</option>
                        {modules.map((m: any) => (
                          <option key={m.id} value={m.id}>{m.name}</option>
                        ))}
                      </select>
                      <button onClick={() => updateFP(fp.id)} className="p-1 text-emerald-600 hover:bg-emerald-50 rounded"><Save className="w-4 h-4" /></button>
                      <button onClick={() => setEditingId(null)} className="p-1 text-slate-400 hover:bg-slate-100 rounded"><X className="w-4 h-4" /></button>
                    </div>
                  </div>
                ) : (
                  <div className="flex items-center justify-between">
                    <div className="flex-1">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-medium text-slate-700">{fp.name}</span>
                        <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-500">
                          {FP_TYPE_LABELS[fp.fp_type] || fp.fp_type}
                        </span>
                        {fp.module_id && (
                          <span className="text-[10px] px-1.5 py-0.5 rounded bg-primary-50 text-primary-600">
                            {modules.find((m: any) => m.id === fp.module_id)?.name || fp.module_id}
                          </span>
                        )}
                      </div>
                      {fp.test_hints?.length > 0 && (
                        <p className="text-xs text-slate-400 mt-0.5">
                          测试提示：{fp.test_hints.slice(0, 3).join('；')}
                        </p>
                      )}
                    </div>
                    {!isArchived && (
                    <div className="flex items-center gap-1 ml-2">
                      <button onClick={() => { setEditingId(fp.id); setEditName(fp.name); setEditModule(fp.module_id || '') }}
                        className="p-1 text-slate-400 hover:text-primary-500 rounded" title="编辑">
                        <Edit3 className="w-3.5 h-3.5" />
                      </button>
                      <button onClick={() => deleteFP(fp.id)}
                        className="p-1 text-slate-400 hover:text-red-500 rounded" title="删除">
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    </div>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      ) : (
        <p className="text-sm text-slate-400 py-8 text-center">
          {isArchived ? '此需求已归档，暂无功能点数据。' : '暂无功能点。点击「重新拆分」生成。'}
        </p>
      )}
    </div>
  )
}
