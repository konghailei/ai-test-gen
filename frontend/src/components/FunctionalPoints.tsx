import { useState, useEffect } from 'react'
import { Loader2, Plus, Trash2, Edit3, Save, X, Search, Layers, GitBranch } from 'lucide-react'
import { requirements as reqApi, products as prodApi } from '../api/client'

interface Props {
  reqId: string
  productId?: string
}

export default function FunctionalPoints({ reqId, productId }: Props) {
  const [fps, setFps] = useState<any[]>([])
  const [bps, setBps] = useState<any[]>([])
  const [selectedBPs, setSelectedBPs] = useState<string[]>([])
  const [fpLoading, setFpLoading] = useState(false)
  const [fpSplitting, setFpSplitting] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editName, setEditName] = useState('')
  const [editModule, setEditModule] = useState('')
  const [modules, setModules] = useState<any[]>([])
  const [nameSearch, setNameSearch] = useState('')
  const [nameSuggestions, setNameSuggestions] = useState<any[]>([])

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

  const extractBP = async () => {
    setFpLoading(true)
    try {
      const r = await reqApi.fpExtractBP(reqId)
      setBps(r.business_points || [])
    } catch { }
    setFpLoading(false)
  }

  const splitFP = async () => {
    if (selectedBPs.length === 0) return
    setFpSplitting(true)
    try {
      const r = await reqApi.fpSplit(reqId, selectedBPs)
      setFps(r.functional_points || [])
      setBps([]); setSelectedBPs([])
    } catch { }
    setFpSplitting(false)
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
      {/* BP 提取 */}
      <div className="flex items-center gap-3">
        <button onClick={extractBP} disabled={fpLoading}
          className="btn-secondary text-sm flex items-center gap-1.5">
          {fpLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Layers className="w-4 h-4" />}
          提取业务点
        </button>
        {bps.length > 0 && (
          <span className="text-xs text-slate-400">检测到 {bps.length} 个业务点，勾选后拆分功能点：</span>
        )}
      </div>

      {/* BP 选择 */}
      {bps.length > 0 && (
        <div className="p-3 bg-slate-50 rounded-xl space-y-2">
          {bps.map((bp: any) => {
            const sel = selectedBPs.includes(bp.name)
            return (
              <label key={bp.name} className="flex items-start gap-2 cursor-pointer py-1">
                <input type="checkbox" checked={sel} onChange={() => {
                  setSelectedBPs(prev => sel ? prev.filter(n => n !== bp.name) : [...prev, bp.name])
                }} className="mt-0.5" />
                <div>
                  <span className="text-sm font-medium text-slate-700">{bp.name}</span>
                  <p className="text-xs text-slate-400">{bp.summary}</p>
                </div>
              </label>
            )
          })}
          <button onClick={splitFP} disabled={selectedBPs.length === 0 || fpSplitting}
            className="btn-primary text-xs flex items-center gap-1 mt-2">
            {fpSplitting ? <Loader2 className="w-3 h-3 animate-spin" /> : <Plus className="w-3 h-3" />}
            拆分 {selectedBPs.length} 个 BP 为功能点
          </button>
        </div>
      )}

      {/* FP 列表 */}
      {fps.length > 0 && (
        <div>
          <p className="text-xs text-slate-400 mb-2">共 {fps.length} 个功能点</p>
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
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {!fpLoading && fps.length === 0 && bps.length === 0 && (
        <p className="text-sm text-slate-400 py-8 text-center">
          尚未拆分功能点。点击「提取业务点」开始。
        </p>
      )}
    </div>
  )
}
