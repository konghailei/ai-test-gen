import { useState, useEffect } from 'react'
import { Loader2, Trash2, Edit3, Save, X, Search, Layers, RefreshCw, AlertTriangle, Sparkles, CopyPlus, Archive } from 'lucide-react'
import { requirements as reqApi, products as prodApi } from '../api/client'

interface Props {
  reqId: string
  productId?: string
  status?: string  // 需求状态：archived 时功能点锁定为只读
}

export default function FunctionalPoints({ reqId, productId, status }: Props) {
  const [fps, setFps] = useState<any[]>([])
  const [fpLoading, setFpLoading] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editName, setEditName] = useState('')
  const [editModules, setEditModules] = useState<string[]>([])
  const [showModulePicker, setShowModulePicker] = useState(false)
  const [modules, setModules] = useState<any[]>([])
  const [nameSearch, setNameSearch] = useState('')
  const [nameSuggestions, setNameSuggestions] = useState<any[]>([])
  // v2.3: 复用功能点 modal
  const [showReuseModal, setShowReuseModal] = useState(false)
  const [reusableList, setReusableList] = useState<any[]>([])
  const [reuseLoading, setReuseLoading] = useState(false)
  const [reuseSearch, setReuseSearch] = useState('')
  const [reuseModuleFilter, setReuseModuleFilter] = useState('')
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

  // v2.20: 移除"重新拆分"功能，只保留合并和复用能力

  const updateFP = async (fpId: string) => {
    await reqApi.fpUpdate(reqId, fpId, { name: editName || undefined, modules: editModules.length > 0 ? editModules : undefined })
    setFps(prev => prev.map(f => f.id === fpId ? { ...f, name: editName || f.name, modules: editModules.length > 0 ? editModules : f.modules } : f))
    setEditingId(null); setEditModules([]); setShowModulePicker(false)
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

  // v2.3: 加载可复用功能点列表
  const loadReusable = async () => {
    setReuseLoading(true)
    try {
      const r = await reqApi.fpListReusable(reqId, reuseModuleFilter || undefined, reuseSearch || undefined)
      setReusableList(r.items || [])
    } catch {
      setReusableList([])
    }
    setReuseLoading(false)
  }

  // v2.3: 复用功能点
  const handleReuse = async (sourceFpId: string, fpName: string) => {
    if (!confirm(`确认复用功能点「${fpName}」？\n模块信息将自动继承。复用后需重新审核。`)) return
    try {
      await reqApi.fpReuse(reqId, sourceFpId)
      await loadFP()
      setShowReuseModal(false)
      alert(`功能点「${fpName}」已复用，模块已继承`)
    } catch (e: any) {
      alert('复用失败: ' + (e?.message || '未知错误'))
    }
  }

  const FP_TYPE_LABELS: Record<string, string> = {
    workflow: '业务流程', constraint: '约束校验', calculation: '计算规则',
    api_behavior: 'API 行为', data_record: '数据记录', config: '配置项',
  }

  return (
    <div className="space-y-4">
      {/* v2.35: 归档状态提示（归档功能点仍可编辑，编辑后同步更新关联数据） */}
      {isArchived && (
        <div className="flex items-center gap-2 p-3 bg-blue-50 border border-blue-200 rounded-xl text-sm text-blue-700">
          <AlertTriangle className="w-4 h-4 flex-shrink-0" />
          <span>此需求已归档。功能点仍可编辑，编辑后将同步更新总 SRR、向量库和关联用例。</span>
        </div>
      )}

      {/* 操作栏（v2.20: 移除重新拆分，只保留复用） */}
      {!isArchived && (
        <div className="flex items-center gap-3 flex-wrap">
          {/* v2.3: 复用功能点按钮 */}
          <button onClick={() => { setShowReuseModal(true); setReuseSearch(''); setReuseModuleFilter(''); loadReusable() }}
            className="btn-secondary text-sm flex items-center gap-1.5">
            <CopyPlus className="w-4 h-4" />
            复用已有功能点
          </button>
          <span className="text-xs text-slate-400">
            上传需求时已自动拆分功能点。可从已归档需求复用。
          </span>
        </div>
      )}

      {/* v2.3: 未分配模块的批量提示 */}
      {!isArchived && fps.some((fp: any) => fp.needs_module) && (
        <div className="flex items-center gap-2 p-3 bg-orange-50 border border-orange-200 rounded-xl text-sm text-orange-700">
          <AlertTriangle className="w-4 h-4 flex-shrink-0" />
          <span>
            有 {fps.filter((fp: any) => fp.needs_module).length} 个功能点未分配模块，请编辑并选择对应模块后再审核通过。
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
              <div key={fp.id} className={[
                'p-3 bg-white border rounded-xl',
                fp.needs_module ? 'border-orange-200 bg-orange-50/30' : 'border-slate-200',
              ].join(' ')}>
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
                      {/* 模块多选 */}
                      <div className="relative">
                        <button type="button" onClick={() => setShowModulePicker(!showModulePicker)}
                          className="input-field text-sm flex items-center gap-1 min-w-[120px]">
                          {editModules.length > 0
                            ? editModules.map(id => modules.find((m: any) => m.id === id)?.name || id).join(', ')
                            : '选择模块'}
                          <span className="text-slate-400 ml-auto">▾</span>
                        </button>
                        {showModulePicker && (
                          <div className="absolute top-full left-0 bg-white border border-slate-200 rounded-lg shadow-lg z-20 max-h-48 overflow-y-auto min-w-[200px] p-2">
                            {modules.map((m: any) => (
                              <label key={m.id} className="flex items-center gap-2 px-2 py-1 hover:bg-slate-50 rounded cursor-pointer text-sm">
                                <input type="checkbox"
                                  checked={editModules.includes(m.id)}
                                  onChange={e => {
                                    setEditModules(e.target.checked
                                      ? [...editModules, m.id]
                                      : editModules.filter(id => id !== m.id))
                                  }}
                                  className="w-3.5 h-3.5 text-primary-600 rounded" />
                                {m.name}
                              </label>
                            ))}
                            {modules.length === 0 && <p className="text-xs text-slate-400 px-2 py-1">暂无模块</p>}
                          </div>
                        )}
                      </div>
                      <button onClick={() => updateFP(fp.id)} className="p-1 text-emerald-600 hover:bg-emerald-50 rounded"><Save className="w-4 h-4" /></button>
                      <button onClick={() => setEditingId(null)} className="p-1 text-slate-400 hover:bg-slate-100 rounded"><X className="w-4 h-4" /></button>
                    </div>
                  </div>
                ) : (
                  <div className="flex items-center justify-between">
                    <div className="flex-1">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-sm font-medium text-slate-700">{fp.name}</span>
                        <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-500">
                          {FP_TYPE_LABELS[fp.fp_type] || fp.fp_type}
                        </span>
                        {/* v2.3: 未分配模块警告标签 */}
                        {fp.needs_module && (
                          <span className="text-[10px] px-1.5 py-0.5 rounded bg-orange-100 text-orange-700 font-medium flex items-center gap-0.5">
                            <AlertTriangle className="w-3 h-3" /> 未分配模块
                          </span>
                        )}
                        {/* v2.2: 多模块显示（兼容旧 module_id） */}
                        {(() => {
                          const fpModules = (fp.modules as string[]) || (fp.module_id ? [fp.module_id] : [])
                          return fpModules.map((mid: string) => (
                            <span key={mid} className="text-[10px] px-1.5 py-0.5 rounded bg-primary-50 text-primary-600">
                              {modules.find((m: any) => m.id === mid)?.name || mid}
                            </span>
                          ))
                        })()}
                        {/* v2.3: FP 状态标签 */}
                        {fp.status === 'archived' && (
                          <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-200 text-slate-500 flex items-center gap-0.5">
                            <Archive className="w-3 h-3" /> 已归档
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
                      {/* v2.35: 归档功能点也允许编辑（名称/模块变更后同步更新关联数据） */}
                      <button onClick={() => { setEditingId(fp.id); setEditName(fp.name); setEditModules((fp.modules || (fp.module_id ? [fp.module_id] : [])) as string[]) }}
                        className="p-1 text-slate-400 hover:text-primary-500 rounded" title="编辑">
                        <Edit3 className="w-3.5 h-3.5" />
                      </button>
                      {!isArchived && (
                        <button onClick={() => deleteFP(fp.id)}
                          className="p-1 text-slate-400 hover:text-red-500 rounded" title="删除">
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      )}
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      ) : (
        <p className="text-sm text-slate-400 py-8 text-center">
          {isArchived ? '此需求已归档，暂无功能点数据。' : '暂无功能点。点击「复用已有功能点」。'}
        </p>
      )}

      {/* v2.3: 复用功能点 Modal */}
      {showReuseModal && (
        <div className="fixed inset-0 z-50 bg-black/30 backdrop-blur-sm flex items-center justify-center p-4"
          onClick={() => setShowReuseModal(false)}>
          <div className="bg-white rounded-2xl shadow-xl max-w-3xl w-full max-h-[80vh] flex flex-col"
            onClick={e => e.stopPropagation()}>
            {/* Header */}
            <div className="flex items-center justify-between px-5 py-4 border-b border-slate-100">
              <div className="flex items-center gap-2">
                <CopyPlus className="w-5 h-5 text-primary-500" />
                <h2 className="text-base font-semibold text-slate-800">复用已归档功能点</h2>
              </div>
              <button onClick={() => setShowReuseModal(false)} className="p-1 rounded-lg hover:bg-slate-100">
                <X className="w-4 h-4 text-slate-400" />
              </button>
            </div>

            {/* 筛选器 */}
            <div className="px-5 py-3 border-b border-slate-100 flex items-center gap-2 flex-wrap">
              <div className="relative flex-1 min-w-[200px]">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
                <input
                  value={reuseSearch}
                  onChange={e => setReuseSearch(e.target.value)}
                  onKeyDown={e => e.key === 'Enter' && loadReusable()}
                  className="w-full pl-9 pr-3 py-2 text-sm border border-slate-200 rounded-xl focus:outline-none focus:border-primary-400"
                  placeholder="按名称搜索..."
                />
              </div>
              <select
                value={reuseModuleFilter}
                onChange={e => setReuseModuleFilter(e.target.value)}
                className="px-3 py-2 text-sm border border-slate-200 rounded-xl focus:outline-none focus:border-primary-400"
              >
                <option value="">全部模块</option>
                {modules.map((m: any) => (
                  <option key={m.id} value={m.id}>{m.name}</option>
                ))}
              </select>
              <button onClick={loadReusable}
                className="btn-primary text-sm flex items-center gap-1.5">
                <Search className="w-4 h-4" /> 搜索
              </button>
            </div>

            {/* 列表 */}
            <div className="flex-1 overflow-y-auto p-3">
              {reuseLoading ? (
                <div className="flex items-center justify-center py-12">
                  <Loader2 className="w-6 h-6 text-primary-500 animate-spin" />
                </div>
              ) : reusableList.length === 0 ? (
                <div className="text-center py-12 text-slate-400">
                  <Archive className="w-10 h-10 mx-auto mb-2 opacity-30" />
                  <p className="text-sm">暂无可复用的已归档功能点</p>
                  <p className="text-xs mt-1">需求归档后，其功能点将自动进入可复用列表</p>
                </div>
              ) : (
                <div className="space-y-2">
                  {reusableList.map((fp: any) => (
                    <div key={fp.id} className="p-3 border border-slate-200 rounded-xl hover:border-primary-300 hover:shadow-sm transition-all">
                      <div className="flex items-start justify-between gap-3">
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2 flex-wrap">
                            <span className="text-sm font-medium text-slate-800">{fp.name}</span>
                            <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-500">
                              {FP_TYPE_LABELS[fp.fp_type] || fp.fp_type}
                            </span>
                            {/* 模块标签（继承） */}
                            {fp.module_names?.map((mn: string, idx: number) => (
                              <span key={idx} className="text-[10px] px-1.5 py-0.5 rounded bg-primary-50 text-primary-600">
                                {mn}
                              </span>
                            ))}
                          </div>
                          <p className="text-xs text-slate-500 mt-1">
                            源需求：{fp.source_req_title}
                          </p>
                          {fp.test_hints?.length > 0 && (
                            <p className="text-xs text-slate-400 mt-0.5">
                              测试提示：{fp.test_hints.slice(0, 2).join('；')}
                            </p>
                          )}
                        </div>
                        <button
                          onClick={() => handleReuse(fp.id, fp.name)}
                          className="btn-primary text-xs flex items-center gap-1 flex-shrink-0"
                        >
                          <CopyPlus className="w-3.5 h-3.5" /> 复用
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* Footer */}
            <div className="px-5 py-3 border-t border-slate-100 text-xs text-slate-400">
              复用功能点将自动继承原始模块信息，复用后需重新审核。
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
