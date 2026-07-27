import { useState, useEffect, useRef, useCallback } from 'react'
import { createPortal } from 'react-dom'
import { Loader2, Trash2, Edit3, Save, X, Search, Layers, RefreshCw, AlertTriangle, Sparkles, CopyPlus, Archive, ChevronDown, Check, Package } from 'lucide-react'
import { requirements as reqApi, products as prodApi } from '../api/client'

interface Props {
  reqId: string
  productId?: string
  status?: string  // 需求状态：archived 时功能点锁定为只读
}

const FP_TYPE_LABELS: Record<string, string> = {
  workflow: '业务流程', constraint: '约束校验', calculation: '计算规则',
  api_behavior: 'API 行为', data_record: '数据记录', config: '配置项',
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

  // 模块下拉框位置（用于 Portal 渲染）
  const [pickerPos, setPickerPos] = useState<{ top: number; left: number; width: number; placement: 'bottom' | 'top' } | null>(null)
  const modulePickerBtnRef = useRef<HTMLButtonElement>(null)
  const pickerRef = useRef<HTMLDivElement>(null)

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

  const updateFP = async (fpId: string) => {
    await reqApi.fpUpdate(reqId, fpId, { name: editName || undefined, modules: editModules.length > 0 ? editModules : undefined })
    setFps(prev => prev.map(f => f.id === fpId ? { ...f, name: editName || f.name, modules: editModules.length > 0 ? editModules : f.modules } : f))
    setEditingId(null); setEditModules([]); setShowModulePicker(false); setPickerPos(null)
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

  /** 计算并打开模块选择器位置 */
  const openModulePicker = useCallback(() => {
    if (!modulePickerBtnRef.current) return
    const rect = modulePickerBtnRef.current.getBoundingClientRect()
    const viewportH = window.innerHeight
    // 判断下方空间是否足够（下拉框最大高度约 240px）
    const spaceBelow = viewportH - rect.bottom
    const placement: 'bottom' | 'top' = spaceBelow >= 260 ? 'bottom' : 'top'
    const top = placement === 'bottom' ? rect.bottom + 6 : rect.top - 6
    setPickerPos({
      top,
      left: rect.left,
      width: Math.max(rect.width, 220),
      placement,
    })
    setShowModulePicker(true)
  }, [])

  // 关闭下拉框：点击外部
  useEffect(() => {
    if (!showModulePicker) return
    const handler = (e: MouseEvent) => {
      // 点击按钮本身不关闭（由按钮 click 处理）
      if (modulePickerBtnRef.current && modulePickerBtnRef.current.contains(e.target as Node)) return
      if (pickerRef.current && pickerRef.current.contains(e.target as Node)) return
      setShowModulePicker(false)
      setPickerPos(null)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [showModulePicker])

  // 滚动或窗口尺寸变化时关闭下拉框
  useEffect(() => {
    if (!showModulePicker) return
    const close = () => { setShowModulePicker(false); setPickerPos(null) }
    window.addEventListener('scroll', close, true)
    window.addEventListener('resize', close)
    return () => {
      window.removeEventListener('scroll', close, true)
      window.removeEventListener('resize', close)
    }
  }, [showModulePicker])

  /** 获取 FP 的模块列表（兼容 modules 数组和旧 module_id） */
  const getFpModules = (fp: any): string[] => {
    return (fp.modules as string[]) || (fp.module_id ? [fp.module_id] : [])
  }

  /** 判断 FP 是否未分配模块 */
  const isFpUnassigned = (fp: any): boolean => {
    return fp.needs_module || getFpModules(fp).length === 0
  }

  // 分组：未分配模块 + 已分配模块（按模块分组）
  const unassignedFps = fps.filter(fp => isFpUnassigned(fp))
  const assignedFps = fps.filter(fp => !isFpUnassigned(fp))

  // 已分配模块的 FP 按模块分组
  const fpsByModule: Record<string, any[]> = {}
  assignedFps.forEach(fp => {
    const fpMods = getFpModules(fp)
    fpMods.forEach((mid: string) => {
      if (!fpsByModule[mid]) fpsByModule[mid] = []
      fpsByModule[mid].push(fp)
    })
    // 如果 FP 有多个模块，每个模块分组都会显示这个 FP
  })

  return (
    <div className="space-y-4">
      {/* v2.35: 归档状态提示 */}
      {isArchived && (
        <div className="flex items-center gap-2 p-3" style={{ background: 'rgba(0, 122, 255, 0.08)', border: '1px solid rgba(0, 122, 255, 0.2)', borderRadius: 'calc(var(--radius) * 0.5)', color: 'var(--brand-700)', fontSize: 13 }}>
          <AlertTriangle className="w-4 h-4 flex-shrink-0" />
          <span>此需求已归档。功能点仍可编辑，编辑后将同步更新总 SRR、向量库和关联用例。</span>
        </div>
      )}

      {/* 操作栏 */}
      {!isArchived && (
        <div className="flex items-center gap-3 flex-wrap">
          <button onClick={() => { setShowReuseModal(true); setReuseSearch(''); setReuseModuleFilter(''); loadReusable() }}
            className="btn-secondary text-sm">
            <CopyPlus className="w-4 h-4" />
            复用已有功能点
          </button>
          <span className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>
            上传需求时已自动拆分功能点。可从已归档需求复用。
          </span>
        </div>
      )}

      {/* 统计概览 */}
      {fps.length > 0 && (
        <div className="flex items-center gap-4 text-xs" style={{ color: 'var(--foreground-tertiary)' }}>
          <span className="flex items-center gap-1">
            <Sparkles className="w-3.5 h-3.5" />
            共 {fps.length} 个功能点
          </span>
          {unassignedFps.length > 0 && (
            <span className="flex items-center gap-1" style={{ color: 'var(--state-warning)' }}>
              <AlertTriangle className="w-3.5 h-3.5" />
              {unassignedFps.length} 个未分配模块
            </span>
          )}
          {assignedFps.length > 0 && (
            <span className="flex items-center gap-1" style={{ color: 'var(--state-success)' }}>
              <Check className="w-3.5 h-3.5" />
              {assignedFps.length} 个已分配模块
            </span>
          )}
        </div>
      )}

      {/* 未分配模块批量提示 */}
      {!isArchived && unassignedFps.length > 0 && (
        <div className="flex items-center gap-2 p-3" style={{ background: 'var(--state-warning-surface)', border: '1px solid var(--state-warning)', borderRadius: 'calc(var(--radius) * 0.5)', color: 'var(--state-warning)', fontSize: 13 }}>
          <AlertTriangle className="w-4 h-4 flex-shrink-0" />
          <span>
            有 {unassignedFps.length} 个功能点未分配模块，请编辑并选择对应模块后再审核通过。
          </span>
        </div>
      )}

      {/* FP 列表 */}
      {fpLoading ? (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="w-6 h-6 animate-spin" style={{ color: 'var(--brand-500)' }} />
        </div>
      ) : fps.length > 0 ? (
        <div className="space-y-5">
          {/* 组1：未分配模块 */}
          {unassignedFps.length > 0 && (
            <div>
              <div className="flex items-center gap-2 mb-2.5">
                <div className="w-1 h-4 rounded-full" style={{ background: 'var(--state-warning)' }} />
                <h4 className="text-sm font-semibold" style={{ color: 'var(--foreground-secondary)' }}>
                  未分配模块
                </h4>
                <span className="text-xs px-1.5 py-0.5 rounded-full font-medium" style={{ background: 'var(--state-warning-surface)', color: 'var(--state-warning)' }}>
                  {unassignedFps.length}
                </span>
              </div>
              <div className="space-y-2">
                {unassignedFps.map(fp => (
                  <FpItem
                    key={fp.id}
                    fp={fp}
                    isArchived={isArchived}
                    editingId={editingId}
                    editName={editName}
                    editModules={editModules}
                    nameSearch={nameSearch}
                    nameSuggestions={nameSuggestions}
                    showModulePicker={showModulePicker}
                    modulePickerBtnRef={modulePickerBtnRef}
                    modules={modules}
                    onEdit={() => { setEditingId(fp.id); setEditName(fp.name); setEditModules(getFpModules(fp)) }}
                    onEditName={setEditName}
                    onSearchNames={searchNames}
                    onSelectName={(name) => { setEditName(name); setNameSuggestions([]); setNameSearch('') }}
                    onTogglePicker={() => { showModulePicker ? (setShowModulePicker(false), setPickerPos(null)) : openModulePicker() }}
                    onModuleToggle={(id) => {
                      setEditModules(editModules.includes(id)
                        ? editModules.filter(x => x !== id)
                        : [...editModules, id])
                    }}
                    onSave={() => updateFP(fp.id)}
                    onCancel={() => { setEditingId(null); setShowModulePicker(false); setPickerPos(null) }}
                    onDelete={() => deleteFP(fp.id)}
                    getFpModules={getFpModules}
                  />
                ))}
              </div>
            </div>
          )}

          {/* 组2：已分配模块（按模块分组） */}
          {assignedFps.length > 0 && (
            <div>
              <div className="flex items-center gap-2 mb-2.5">
                <div className="w-1 h-4 rounded-full" style={{ background: 'var(--state-success)' }} />
                <h4 className="text-sm font-semibold" style={{ color: 'var(--foreground-secondary)' }}>
                  已分配模块
                </h4>
                <span className="text-xs px-1.5 py-0.5 rounded-full font-medium" style={{ background: 'var(--state-success-surface)', color: 'var(--state-success)' }}>
                  {assignedFps.length}
                </span>
              </div>

              {/* 按模块分组显示 */}
              {Object.keys(fpsByModule).length > 0 ? (
                <div className="space-y-4">
                  {Object.entries(fpsByModule).map(([modId, modFps]) => {
                    const modName = modules.find(m => m.id === modId)?.name || modId
                    return (
                      <div key={modId}>
                        {/* 模块分组标题 */}
                        <div className="flex items-center gap-2 mb-2 px-2 py-1.5" style={{ background: 'rgba(0, 122, 255, 0.06)', borderRadius: 'calc(var(--radius) * 0.4)', border: '1px solid rgba(0, 122, 255, 0.15)' }}>
                          <Package className="w-3.5 h-3.5" style={{ color: 'var(--brand-500)' }} />
                          <span className="text-xs font-semibold" style={{ color: 'var(--brand-700)' }}>{modName}</span>
                          <span className="text-[10px] px-1.5 py-0.5 rounded-full" style={{ background: 'var(--background)', color: 'var(--brand-500)', fontWeight: 500 }}>
                            {modFps.length} 个功能点
                          </span>
                        </div>
                        {/* 该模块下的 FP 列表 */}
                        <div className="space-y-2 ml-1">
                          {modFps.map(fp => (
                            <FpItem
                              key={fp.id}
                              fp={fp}
                              isArchived={isArchived}
                              editingId={editingId}
                              editName={editName}
                              editModules={editModules}
                              nameSearch={nameSearch}
                              nameSuggestions={nameSuggestions}
                              showModulePicker={showModulePicker}
                              modulePickerBtnRef={modulePickerBtnRef}
                              modules={modules}
                              onEdit={() => { setEditingId(fp.id); setEditName(fp.name); setEditModules(getFpModules(fp)) }}
                              onEditName={setEditName}
                              onSearchNames={searchNames}
                              onSelectName={(name) => { setEditName(name); setNameSuggestions([]); setNameSearch('') }}
                              onTogglePicker={() => { showModulePicker ? (setShowModulePicker(false), setPickerPos(null)) : openModulePicker() }}
                              onModuleToggle={(id) => {
                                setEditModules(editModules.includes(id)
                                  ? editModules.filter(x => x !== id)
                                  : [...editModules, id])
                              }}
                              onSave={() => updateFP(fp.id)}
                              onCancel={() => { setEditingId(null); setShowModulePicker(false); setPickerPos(null) }}
                              onDelete={() => deleteFP(fp.id)}
                              getFpModules={getFpModules}
                            />
                          ))}
                        </div>
                      </div>
                    )
                  })}
                </div>
              ) : (
                <div className="space-y-2">
                  {assignedFps.map(fp => (
                    <FpItem
                      key={fp.id}
                      fp={fp}
                      isArchived={isArchived}
                      editingId={editingId}
                      editName={editName}
                      editModules={editModules}
                      nameSearch={nameSearch}
                      nameSuggestions={nameSuggestions}
                      showModulePicker={showModulePicker}
                      modulePickerBtnRef={modulePickerBtnRef}
                      modules={modules}
                      onEdit={() => { setEditingId(fp.id); setEditName(fp.name); setEditModules(getFpModules(fp)) }}
                      onEditName={setEditName}
                      onSearchNames={searchNames}
                      onSelectName={(name) => { setEditName(name); setNameSuggestions([]); setNameSearch('') }}
                      onTogglePicker={() => { showModulePicker ? (setShowModulePicker(false), setPickerPos(null)) : openModulePicker() }}
                      onModuleToggle={(id) => {
                        setEditModules(editModules.includes(id)
                          ? editModules.filter(x => x !== id)
                          : [...editModules, id])
                      }}
                      onSave={() => updateFP(fp.id)}
                      onCancel={() => { setEditingId(null); setShowModulePicker(false); setPickerPos(null) }}
                      onDelete={() => deleteFP(fp.id)}
                      getFpModules={getFpModules}
                    />
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      ) : (
        <p className="text-sm py-8 text-center" style={{ color: 'var(--foreground-tertiary)' }}>
          {isArchived ? '此需求已归档，暂无功能点数据。' : '暂无功能点。点击「复用已有功能点」。'}
        </p>
      )}

      {/* 模块选择器 Portal —— 渲染到 body，避免被父容器 overflow 遮挡 */}
      {showModulePicker && pickerPos && createPortal(
        <div
          ref={pickerRef}
          className="fixed z-[100]"
          style={{
            top: pickerPos.placement === 'bottom' ? pickerPos.top : undefined,
            bottom: pickerPos.placement === 'top' ? (window.innerHeight - pickerPos.top) : undefined,
            left: pickerPos.left,
            width: pickerPos.width,
            maxHeight: 260,
            overflowY: 'auto',
            background: 'var(--background)',
            border: '1px solid var(--border)',
            borderRadius: 'calc(var(--radius) * 0.5)',
            boxShadow: 'var(--shadow-apple-xl)',
            padding: 6,
          }}
        >
          {modules.length === 0 ? (
            <p className="text-xs px-2 py-2" style={{ color: 'var(--foreground-tertiary)' }}>暂无模块</p>
          ) : (
            modules.map((m: any) => {
              const checked = editModules.includes(m.id)
              return (
                <button
                  key={m.id}
                  type="button"
                  onClick={() => {
                    setEditModules(checked
                      ? editModules.filter(x => x !== m.id)
                      : [...editModules, m.id])
                  }}
                  className="w-full flex items-center gap-2 px-2.5 py-2 transition-colors text-left"
                  style={{
                    borderRadius: 'calc(var(--radius) * 0.4)',
                    background: checked ? 'rgba(0, 122, 255, 0.08)' : 'transparent',
                    color: checked ? 'var(--brand-700)' : 'var(--foreground-secondary)',
                    fontSize: 13,
                  }}
                  onMouseEnter={(e) => { if (!checked) e.currentTarget.style.background = 'var(--background-muted)' }}
                  onMouseLeave={(e) => { if (!checked) e.currentTarget.style.background = 'transparent' }}
                >
                  <span
                    className="w-4 h-4 flex items-center justify-center flex-shrink-0 border-2 transition-all"
                    style={{
                      borderRadius: 'calc(var(--radius) * 0.2)',
                      borderColor: checked ? 'var(--brand-500)' : 'var(--input)',
                      background: checked ? 'var(--brand-500)' : 'transparent',
                    }}
                  >
                    {checked && <Check className="w-3 h-3" style={{ color: '#ffffff' }} />}
                  </span>
                  <span className="flex-1 truncate">{m.name}</span>
                </button>
              )
            })
          )}
          {modules.length > 0 && (
            <div style={{ borderTop: '1px solid var(--border)', marginTop: 4, paddingTop: 4 }}>
              <p className="text-[11px] px-2 py-1" style={{ color: 'var(--foreground-tertiary)' }}>
                已选 {editModules.length} 个模块
              </p>
            </div>
          )}
        </div>,
        document.body
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

// ============================================================================
// FP 单项组件
// ============================================================================

interface FpItemProps {
  fp: any
  isArchived: boolean
  editingId: string | null
  editName: string
  editModules: string[]
  nameSearch: string
  nameSuggestions: any[]
  showModulePicker: boolean
  modulePickerBtnRef: React.RefObject<HTMLButtonElement>
  modules: any[]
  onEdit: () => void
  onEditName: (v: string) => void
  onSearchNames: (v: string) => void
  onSelectName: (name: string) => void
  onTogglePicker: () => void
  onModuleToggle: (id: string) => void
  onSave: () => void
  onCancel: () => void
  onDelete: () => void
  getFpModules: (fp: any) => string[]
}

function FpItem({
  fp, isArchived, editingId, editName, editModules, nameSearch, nameSuggestions,
  showModulePicker, modulePickerBtnRef, modules,
  onEdit, onEditName, onSearchNames, onSelectName, onTogglePicker, onSave, onCancel, onDelete,
  getFpModules,
}: FpItemProps) {
  const isEditing = editingId === fp.id
  const fpModules = getFpModules(fp)

  return (
    <div
      className="p-3 transition-all"
      style={{
        background: isFpUnassigned(fp) ? 'rgba(255, 149, 0, 0.04)' : 'var(--background)',
        border: `1px solid ${isFpUnassigned(fp) ? 'var(--state-warning)' : 'var(--border)'}`,
        borderRadius: 'calc(var(--radius) * 0.5)',
      }}
    >
      {isEditing ? (
        <div className="space-y-2">
          {/* 第一行：名称 + 搜索 + 模块选择 + 操作 */}
          <div className="flex items-center gap-2 flex-wrap">
            <input
              value={editName}
              onChange={e => onEditName(e.target.value)}
              className="input-field text-sm flex-1 min-w-[180px]"
              placeholder="功能点名称"
              autoFocus
            />
            {/* 名称搜索 */}
            <div className="relative">
              <input
                value={nameSearch}
                onChange={e => onSearchNames(e.target.value)}
                className="input-field text-sm w-40"
                placeholder="搜索已有名称"
              />
              {nameSuggestions.length > 0 && (
                <div
                  className="absolute top-full left-0 right-0 z-30 max-h-32 overflow-y-auto"
                  style={{
                    background: 'var(--background)',
                    border: '1px solid var(--border)',
                    borderRadius: 'calc(var(--radius) * 0.4)',
                    boxShadow: 'var(--shadow-apple-md)',
                    marginTop: 4,
                  }}
                >
                  {nameSuggestions.map((s: any) => (
                    <button
                      key={s.id}
                      onClick={() => onSelectName(s.name)}
                      className="w-full text-left px-3 py-1.5 text-xs transition-colors"
                      style={{ color: 'var(--foreground-secondary)' }}
                      onMouseEnter={(e) => { e.currentTarget.style.background = 'rgba(0, 122, 255, 0.06)' }}
                      onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent' }}
                    >
                      {s.name}
                    </button>
                  ))}
                </div>
              )}
            </div>
            {/* 模块选择按钮 */}
            <button
              ref={modulePickerBtnRef}
              type="button"
              onClick={onTogglePicker}
              className="input-field text-sm flex items-center gap-1 min-w-[140px] cursor-pointer"
              style={{ justifyContent: 'space-between' }}
            >
              <span className="truncate">
                {editModules.length > 0
                  ? editModules.map(id => modules.find((m: any) => m.id === id)?.name || id).join(', ')
                  : '选择模块'}
              </span>
              <ChevronDown className={`w-3.5 h-3.5 flex-shrink-0 transition-transform ${showModulePicker ? 'rotate-180' : ''}`} style={{ color: 'var(--foreground-tertiary)' }} />
            </button>
            <button onClick={onSave} className="p-1.5 rounded transition-colors" style={{ color: 'var(--state-success)' }} title="保存"
              onMouseEnter={(e) => { e.currentTarget.style.background = 'var(--state-success-surface)' }}
              onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent' }}>
              <Save className="w-4 h-4" />
            </button>
            <button onClick={onCancel} className="p-1.5 rounded transition-colors" style={{ color: 'var(--foreground-tertiary)' }} title="取消"
              onMouseEnter={(e) => { e.currentTarget.style.background = 'var(--background-muted)' }}
              onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent' }}>
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>
      ) : (
        <div className="flex items-center justify-between gap-2">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-sm font-medium" style={{ color: 'var(--foreground)' }}>{fp.name}</span>
              <span className="text-[10px] px-1.5 py-0.5 rounded font-medium" style={{ background: 'var(--background-muted)', color: 'var(--foreground-tertiary)' }}>
                {FP_TYPE_LABELS[fp.fp_type] || fp.fp_type}
              </span>
              {/* 未分配模块警告标签 */}
              {isFpUnassigned(fp) && (
                <span className="text-[10px] px-1.5 py-0.5 rounded font-medium flex items-center gap-0.5" style={{ background: 'var(--state-warning-surface)', color: 'var(--state-warning)' }}>
                  <AlertTriangle className="w-3 h-3" /> 未分配模块
                </span>
              )}
              {/* 多模块显示 */}
              {fpModules.map((mid: string) => (
                <span key={mid} className="text-[10px] px-1.5 py-0.5 rounded font-medium" style={{ background: 'rgba(0, 122, 255, 0.08)', color: 'var(--brand-500)' }}>
                  {modules.find((m: any) => m.id === mid)?.name || mid}
                </span>
              ))}
              {/* FP 状态标签 */}
              {fp.status === 'archived' && (
                <span className="text-[10px] px-1.5 py-0.5 rounded font-medium flex items-center gap-0.5" style={{ background: 'var(--background-muted)', color: 'var(--foreground-tertiary)' }}>
                  <Archive className="w-3 h-3" /> 已归档
                </span>
              )}
            </div>
            {fp.test_hints?.length > 0 && (
              <p className="text-xs mt-1" style={{ color: 'var(--foreground-tertiary)' }}>
                测试提示：{fp.test_hints.slice(0, 3).join('；')}
              </p>
            )}
          </div>
          <div className="flex items-center gap-1 ml-2 flex-shrink-0">
            <button
              onClick={onEdit}
              className="p-1.5 rounded transition-colors"
              style={{ color: 'var(--foreground-tertiary)' }}
              title="编辑"
              onMouseEnter={(e) => { e.currentTarget.style.background = 'var(--background-muted)'; e.currentTarget.style.color = 'var(--brand-500)' }}
              onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = 'var(--foreground-tertiary)' }}
            >
              <Edit3 className="w-3.5 h-3.5" />
            </button>
            {!isArchived && (
              <button
                onClick={onDelete}
                className="p-1.5 rounded transition-colors"
                style={{ color: 'var(--foreground-tertiary)' }}
                title="删除"
                onMouseEnter={(e) => { e.currentTarget.style.background = 'var(--state-error-surface)'; e.currentTarget.style.color = 'var(--state-error)' }}
                onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = 'var(--foreground-tertiary)' }}
              >
                <Trash2 className="w-3.5 h-3.5" />
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

/** 判断 FP 是否未分配模块（提取到组件外以便复用） */
function isFpUnassigned(fp: any): boolean {
  const fpModules = (fp.modules as string[]) || (fp.module_id ? [fp.module_id] : [])
  return fp.needs_module || fpModules.length === 0
}
