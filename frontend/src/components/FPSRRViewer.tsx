import { useState, useEffect, useRef, useCallback } from 'react'
import { createPortal } from 'react-dom'
import {
  ChevronDown, ChevronRight, Check, AlertTriangle,
  Loader2, Edit3, Save, X, Clock, ExternalLink, Layers, GitBranch, Share2, FileCode,
} from 'lucide-react'
import { requirements as reqApi, dependencyGraph, products as prodApi } from '../api/client'
import { useNavigate } from 'react-router-dom'
import type { FPItem } from '../types'
import { brTypeLabel, fpTypeLabel, acTypeLabel, dataTypeLabel } from '../utils/labels'
import FieldEditor from '../components/FieldEditor'

interface Props {
  reqId: string
  productId?: string
  status?: string
  /** v2.33: FP 变更（合并/编辑/删除/重新生成）后通知父组件刷新 revision 基线 */
  onChanged?: () => void
}

interface FPDepEdge { source_fp_id: string; target_fp_id: string; relation_type: string }

const RELATION_LABELS: Record<string, string> = {
  depends_on: '前置依赖', data_flows: '数据流转', invariants: '共享约束',
  lifecycle_scenarios: '同流程', conflicts_with: '互斥',
}

export default function FPSRRViewer({ reqId, productId, status, onChanged }: Props) {
  const [fps, setFps] = useState<FPItem[]>([])
  const [deps, setDeps] = useState<{ dependencies: FPDepEdge[] }>({ dependencies: [] })
  const [modules, setModules] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editName, setEditName] = useState('')
  const [editModule, setEditModule] = useState('')
  const [editModules, setEditModules] = useState<string[]>([])
  const [showFpModulePicker, setShowFpModulePicker] = useState(false)
  // v2.42: 模块下拉框位置（Portal 渲染到 body，避免被父容器 overflow 遮挡）
  const [fpPickerPos, setFpPickerPos] = useState<{ top: number; left: number; width: number; placement: 'bottom' | 'top' } | null>(null)
  const fpModuleBtnRef = useRef<HTMLButtonElement>(null)
  const fpPickerRef = useRef<HTMLDivElement>(null)

  /** 计算模块选择器位置并打开（下方空间不足时向上展开） */
  const openFpModulePicker = useCallback(() => {
    if (!fpModuleBtnRef.current) return
    const rect = fpModuleBtnRef.current.getBoundingClientRect()
    const viewportH = window.innerHeight
    const spaceBelow = viewportH - rect.bottom
    const placement: 'bottom' | 'top' = spaceBelow >= 260 ? 'bottom' : 'top'
    const top = placement === 'bottom' ? rect.bottom + 6 : rect.top - 6
    setFpPickerPos({
      top,
      left: rect.left,
      width: Math.max(rect.width, 220),
      placement,
    })
    setShowFpModulePicker(true)
  }, [])

  // 点击外部或滚动时关闭模块选择器
  useEffect(() => {
    if (!showFpModulePicker) return
    const handleClick = (e: MouseEvent) => {
      if (fpPickerRef.current && !fpPickerRef.current.contains(e.target as Node) &&
          fpModuleBtnRef.current && !fpModuleBtnRef.current.contains(e.target as Node)) {
        setShowFpModulePicker(false)
      }
    }
    const handleScroll = () => setShowFpModulePicker(false)
    document.addEventListener('mousedown', handleClick)
    window.addEventListener('scroll', handleScroll, true)
    return () => {
      document.removeEventListener('mousedown', handleClick)
      window.removeEventListener('scroll', handleScroll, true)
    }
  }, [showFpModulePicker])
  const [impactModal, setImpactModal] = useState<any>(null)
  const [impactLoading, setImpactLoading] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [merging, setMerging] = useState(false)
  const [editSrrFpId, setEditSrrFpId] = useState<string | null>(null)
  const [editSrrJson, setEditSrrJson] = useState('')
  // v2.23: 功能点审核通过未分配模块的警告提示
  const [approveWarning, setApproveWarning] = useState<{ fpId: string; fpName: string; message: string } | null>(null)
  const isArchived = status === 'archived'
  const navigate = useNavigate()

  useEffect(() => { load(); loadModules(); loadDeps() }, [reqId, productId])

  const load = async () => {
    setLoading(true)
    try { const r = await reqApi.fpList(reqId); setFps(r.items || []) } catch { }
    setLoading(false)
  }
  const loadModules = async () => {
    if (!productId) return
    try { setModules(await prodApi.listModules(productId) || []) } catch { }
  }
  const loadDeps = async () => {
    try { const r = await reqApi.fpDependencies(reqId); setDeps(r) } catch { }
  }

  // 兼容 modules 数组与旧 module_id 字段
  const getFpModules = (fp: FPItem): string[] => {
    return (fp.modules as string[]) || (fp.module_id ? [fp.module_id] : [])
  }
  // 判断功能点是否未分配模块
  const isFpUnassigned = (fp: FPItem): boolean => {
    return (fp as any).needs_module || getFpModules(fp).length === 0
  }

  // 分组：未分配模块 + 已分配模块（按模块分组）
  const unassignedFps = fps.filter(fp => isFpUnassigned(fp))
  const assignedFps = fps.filter(fp => !isFpUnassigned(fp))

  // 已分配模块的功能点按模块分组（一个 FP 若关联多模块，在每个模块分组中都显示）
  const fpsByModule: Record<string, FPItem[]> = {}
  assignedFps.forEach(fp => {
    const mods = getFpModules(fp)
    mods.forEach((mid: string) => {
      if (!fpsByModule[mid]) fpsByModule[mid] = []
      fpsByModule[mid].push(fp)
    })
  })

  const approved = fps.filter(f => f.status === 'approved').length
  const allApproved = fps.length > 0 && approved === fps.length

  // 编辑 FP 子 SRR
  const handleEditSrr = (fpId: string, srrJson: any) => {
    setEditSrrFpId(fpId)
    setEditSrrJson(JSON.stringify(srrJson || {}, null, 2))
  }
  // FieldEditor 保存回调 — 更新 FP 子 SRR 的指定字段
  const saveFpField = (fpId: string) => async (fieldPath: string, newValue: unknown) => {
    const fp = fps.find(f => f.id === fpId)
    if (!fp) return
    const srr = JSON.parse(JSON.stringify(fp.srr_json || {}))
    const path = fieldPath.split('.')
    let target: any = srr
    for (let i = 0; i < path.length - 1; i++) {
      target = target[path[i]] = target[path[i]] || {}
    }
    target[path[path.length - 1]] = newValue
    await reqApi.fpUpdate(reqId, fpId, { srr_json: srr })
    // 更新本地 fps 状态
    setFps(prev => prev.map(f => f.id === fpId ? { ...f, srr_json: srr } : f))
    onChanged?.()
  }

  const handleSaveSrr = async () => {
    if (!editSrrFpId) return
    try {
      const parsed = JSON.parse(editSrrJson)
      await reqApi.fpUpdate(reqId, editSrrFpId, { srr_json: parsed })
      await load()
      setEditSrrFpId(null)
      onChanged?.()
    } catch { }
  }

  // v2.20: 移除"重新拆分"功能，只保留合并能力

  const [mergeModal, setMergeModal] = useState(false)
  const [mergeName, setMergeName] = useState('')

  // 多选合并
  const toggleSelect = (fpId: string) => {
    const next = new Set(selected)
    if (next.has(fpId)) next.delete(fpId); else next.add(fpId)
    setSelected(next)
  }
  const openMergeDialog = () => {
    // 预填：取已选中 FP 中名字最短的
    const names = fps.filter(f => selected.has(f.id)).map(f => f.name)
    setMergeName(names.sort((a,b) => a.length - b.length)[0] || '')
    setMergeModal(true)
  }
  const handleMerge = async () => {
    const ids = [...selected]
    if (ids.length < 2) return
    setMerging(true)
    setMergeModal(false)
    try {
      await reqApi.fpMerge(reqId, ids, mergeName)
      setSelected(new Set())
      await load()
      await loadDeps()
      onChanged?.()
    } catch { }
    setMerging(false)
  }

  const toggle = (id: string) => {
    const next = new Set(expanded)
    if (next.has(id)) next.delete(id); else next.add(id)
    setExpanded(next)
  }

  const approve = async (fpId: string) => {
    const fp = fps.find(f => f.id === fpId)
    if (!fp) return
    // v2.23: 前置校验 modules — 未分配模块不允许审核通过，显示提示而非无反应
    if (!fp.module_id && !(fp.modules?.length)) {
      setApproveWarning({
        fpId,
        fpName: fp.name,
        message: `功能点「${fp.name}」未分配模块，请先点击编辑按钮选择对应模块后再审核通过`,
      })
      // 5 秒后自动清除警告
      setTimeout(() => setApproveWarning(prev => prev?.fpId === fpId ? null : prev), 5000)
      return
    }
    setApproveWarning(null)
    try {
      await reqApi.fpApprove(reqId, fpId)
      setFps(prev => prev.map(f => f.id === fpId ? { ...f, status: 'approved' } : f))
    } catch (e: any) {
      // 后端返回 409 等错误时显示友好提示
      const errMsg = e?.message || e?.detail || '审核通过失败，请重试'
      setApproveWarning({
        fpId,
        fpName: fp.name,
        message: errMsg,
      })
      setTimeout(() => setApproveWarning(prev => prev?.fpId === fpId ? null : prev), 5000)
    }
  }

  const updateFP = async (fpId: string) => {
    await reqApi.fpUpdate(reqId, fpId, { name: editName || undefined, modules: editModules.length > 0 ? editModules : undefined })
    setFps(prev => prev.map(f => f.id === fpId ? { ...f, name: editName || f.name, modules: editModules.length > 0 ? editModules : f.modules, module_id: editModules.length > 0 ? editModules[0] : f.module_id } : f))
    setEditingId(null); setEditModules([]); setShowFpModulePicker(false); setFpPickerPos(null)
    onChanged?.()
  }

  // 影响分析
  const runImpact = async (fpId: string) => {
    if (!productId) return
    setImpactLoading(true)
    try {
      const result = await dependencyGraph.fpImpact(productId, fpId)
      setImpactModal(result)
    } catch (e: any) {
      alert(e?.message || '影响分析失败，请重试')
    }
    setImpactLoading(false)
  }

  // 依赖图谱：找到当前 FP 相关的边
  const relatedDeps = (fpId: string) =>
    deps.dependencies?.filter(d => d.source_fp_id === fpId || d.target_fp_id === fpId) || []

  // 获取 FP 名称映射
  const fpNameMap: Record<string, string> = {}
  fps.forEach(f => { fpNameMap[f.id] = f.name })

  if (loading) return <div className="flex items-center justify-center py-12"><Loader2 className="w-6 h-6 text-primary-500 animate-spin" /></div>

  return (
    <div className="space-y-4">
      {/* 审核进度 + 合并操作（v2.20: 移除重新拆分，只保留合并） */}
      {!isArchived && (
        <>
        {fps.length > 0 && (
          <div className={[
            'flex items-center gap-3 p-3 rounded-xl text-sm',
            allApproved ? 'bg-emerald-50 text-emerald-700' : 'bg-amber-50 text-amber-700',
          ].join(' ')}>
            {allApproved ? <Check className="w-5 h-5" /> : <Clock className="w-5 h-5" />}
            <span>
              {allApproved
                ? '全部功能点已审核通过，可进行下一步：生成测试用例'
                : `审核进度：${approved}/${fps.length} 个已通过，全部审核通过后可生成测试用例`}
            </span>
          </div>
        )}
        <div className="flex items-center justify-between">
          <span className="text-xs text-slate-400">
            上传时自动拆分。拆分不准确可多选合并调整。
          </span>
          <div className="flex items-center gap-2">
            {selected.size >= 2 && (
              <button onClick={openMergeDialog} disabled={merging}
                className="btn-primary text-xs flex items-center gap-1.5">
                {merging ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <GitBranch className="w-3.5 h-3.5" />}
                合并 {selected.size} 个
              </button>
            )}
          </div>
        </div>
        </>
      )}

      {fps.length === 0 ? (
        <p className="text-sm text-slate-400 py-8 text-center">
          {isArchived ? '此需求已归档，暂无功能点数据。' : '暂无功能点数据。'}
        </p>
      ) : (
        ([
          ...(unassignedFps.length > 0
            ? [{ key: '_unassigned', title: '未分配模块', fpList: unassignedFps, isUnassigned: true }]
            : []),
          ...Object.entries(fpsByModule).map(([modId, modFps]) => ({
            key: modId,
            title: modules.find(m => m.id === modId)?.name || modId,
            fpList: modFps,
            isUnassigned: false,
          })),
        ]).map(section => {
          const { key: modId, title: modName, fpList } = section
          return (
            <div key={modId} className="border border-slate-200 rounded-xl overflow-hidden">
              {/* 模块标题 */}
              <div className="px-4 py-2.5 bg-slate-50 border-b border-slate-100 flex items-center gap-2">
                <Layers className="w-4 h-4 text-slate-400" />
                <span className="text-sm font-medium text-slate-700">{modName}</span>
                <span className="text-xs text-slate-400">{fpList.length} 个功能点</span>
              </div>

              {/* FP 列表 */}
              {fpList.map(fp => {
                const isExpanded = expanded.has(fp.id)
                const depsForFp = relatedDeps(fp.id)
                return (
                  <div key={fp.id} className="border-b border-slate-100 last:border-b-0">
                    {/* FP 标题行 */}
                    <div className={[
                      'flex items-center justify-between px-4 py-3 hover:bg-slate-50 cursor-pointer transition-colors',
                      fp.status === 'approved' ? '' : 'bg-amber-50/30',
                    ].join(' ')}
                      onClick={() => toggle(fp.id)}
                    >
                      <div className="flex items-center gap-3">
                        {!isArchived && (
                          <input type="checkbox"
                            checked={selected.has(fp.id)}
                            onChange={() => toggleSelect(fp.id)}
                            onClick={e => e.stopPropagation()}
                            className="w-4 h-4 text-primary-600 border-slate-300 rounded focus:ring-primary-500"
                          />
                        )}
                        <button className="text-slate-400">
                          {isExpanded ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
                        </button>
                        <div>
                          <div className="flex items-center gap-2">
                            <span className="text-sm font-medium text-slate-700">{fp.name}</span>
                            <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-500">
                              {fpTypeLabel(fp.fp_type)}
                            </span>
                            {fp.status === 'approved' ? (
                              <span className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-100 text-emerald-600">
                                已审核
                              </span>
                            ) : (
                              <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-100 text-amber-600">
                                待审核
                              </span>
                            )}
                            {/* v2.2: 多模块标签 */}
                            {(() => {
                              const fpMods = (fp.modules as string[]) || (fp.module_id ? [fp.module_id] : [])
                              return fpMods.map((mid: string) => (
                                <span key={mid} className="text-[10px] px-1.5 py-0.5 rounded bg-primary-50 text-primary-600">
                                  {modules.find((m: any) => m.id === mid)?.name || mid}
                                </span>
                              ))
                            })()}
                          </div>
                          {/* v2.48: 移除 test_hints 预览（不再生成测试提示） */}
                        </div>
                      </div>
                      <div className="flex items-center gap-1" onClick={e => e.stopPropagation()}>
                        {fp.status !== 'approved' && !isArchived && (
                          <button
                            onClick={() => approve(fp.id)}
                            className="p-1.5 text-emerald-600 hover:bg-emerald-50 rounded-lg text-xs flex items-center gap-1"
                            title="审核通过"
                          >
                            <Check className="w-3.5 h-3.5" /> 通过
                          </button>
                        )}
                        {/* v2.35: 归档功能点也允许编辑子 SRR（编辑后同步更新总 SRR/向量库/用例） */}
                        <button
                          onClick={() => handleEditSrr(fp.id, fp.srr_json)}
                          className="p-1.5 text-indigo-500 hover:bg-indigo-50 rounded-lg"
                          title="编辑子 SRR"
                        >
                          <FileCode className="w-3.5 h-3.5" />
                        </button>
                        {editingId === fp.id ? null : (
                          <button
                            onClick={() => { setEditingId(fp.id); setEditName(fp.name); setEditModules((fp.modules || (fp.module_id ? [fp.module_id] : [])) as string[]) }}
                            className="p-1.5 text-slate-400 hover:text-primary-500 rounded-lg"
                          >
                            <Edit3 className="w-3.5 h-3.5" />
                          </button>
                        )}
                        {productId && (
                          <button
                            onClick={() => runImpact(fp.id)}
                            className="p-1.5 text-indigo-400 hover:text-indigo-600 hover:bg-indigo-50 rounded-lg"
                            title="影响分析"
                          >
                            <Share2 className="w-3.5 h-3.5" />
                          </button>
                        )}
                      </div>
                    </div>

                    {/* v2.23: 审核通过未分配模块的警告提示 */}
                    {approveWarning?.fpId === fp.id && (
                      <div className="mx-4 mb-2 p-2.5 bg-amber-50 border border-amber-200 rounded-lg flex items-start gap-2 animate-slide-up">
                        <AlertTriangle className="w-4 h-4 text-amber-500 flex-shrink-0 mt-0.5" />
                        <div className="flex-1 min-w-0">
                          <p className="text-xs text-amber-700 font-medium">{approveWarning.message}</p>
                        </div>
                        <button
                          onClick={(e) => {
                            e.stopPropagation()
                            setApproveWarning(null)
                            // 自动展开编辑模式，引导用户选择模块
                            if (!isExpanded) toggle(fp.id)
                          }}
                          className="text-xs text-amber-600 hover:text-amber-800 font-medium flex-shrink-0"
                        >
                          去编辑
                        </button>
                      </div>
                    )}

                    {/* 编辑态 */}
                    {editingId === fp.id && (
                      <div className="px-4 pb-3 flex items-center gap-2" onClick={e => e.stopPropagation()}>
                        <input value={editName} onChange={e => setEditName(e.target.value)}
                          className="input-field text-sm flex-1" placeholder="功能点名称" autoFocus />
                        <div className="relative">
                          <button type="button" ref={fpModuleBtnRef} onClick={openFpModulePicker}
                            className="select-field text-sm flex items-center gap-1 max-w-[200px]">
                            {editModules.length > 0
                              ? editModules.map(id => modules.find((m: any) => m.id === id)?.name || id).join(', ')
                              : '选择模块'}
                            <span className="text-slate-400 ml-auto">▾</span>
                          </button>
                        </div>
                        {/* v2.8.1: 保存/取消按钮移到下拉框旁，选完模块直接保存，无需上移视线 */}
                        <button onClick={() => updateFP(fp.id)}
                          className="p-1.5 text-emerald-600 hover:bg-emerald-50 rounded-lg flex items-center gap-1 text-xs font-medium"
                          title="保存">
                          <Save className="w-3.5 h-3.5" /> 保存
                        </button>
                        <button onClick={() => { setEditingId(null); setShowFpModulePicker(false); setFpPickerPos(null) }}
                          className="p-1.5 text-slate-400 hover:bg-slate-100 rounded-lg"
                          title="取消">
                          <X className="w-3.5 h-3.5" />
                        </button>
                      </div>
                    )}

                    {/* SRR 展开内容 */}
                    {isExpanded && (
                      <div className="px-4 pb-4 bg-slate-50/50 space-y-3">
                        {/* 子 SRR 内容（v2.28 精简为 6 个核心字段，兼容旧格式） */}
                        {fp.srr_json && Object.keys(fp.srr_json).length > 0 && (
                          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                            {/* 1. 概述 summary（兼容旧格式 requirement.summary） */}
                            <div className="md:col-span-2">
                              <FieldEditor
                                label="概述"
                                fieldPath="summary"
                                value={(fp.srr_json as any).summary || (fp.srr_json as any).requirement?.summary || (fp.srr_json as any).description || ''}
                                valueType="string"
                                onSave={saveFpField(fp.id)}
                                disabled={false}
                                render={(v) => <p className="text-sm text-slate-600 mt-0.5">{String(v || '')}</p>}
                              />
                            </div>
                            {/* 2. 正常流程 */}
                            <div className="md:col-span-2">
                              <FieldEditor
                                label="正常流程"
                                fieldPath="flows.happy_path.steps"
                                value={fp.srr_json.flows?.happy_path?.steps || []}
                                valueType="object[]"
                                onSave={saveFpField(fp.id)}
                                disabled={false}
                                render={(v) => {
                                  const steps = Array.isArray(v) ? v : []
                                  if (!steps.length) return <p className="text-xs text-slate-400 italic">暂无流程步骤</p>
                                  return (
                                    <ol className="mt-1 space-y-1">
                                      {steps.map((s: any) => (
                                        <li key={s.order} className="text-sm text-slate-600 pl-2">
                                          <span className="text-slate-400 text-xs mr-1">{s.order}.</span>
                                          {s.action}
                                          <span className="text-slate-400"> → </span>
                                          <span className="text-slate-500">{s.expect}</span>
                                        </li>
                                      ))}
                                    </ol>
                                  )
                                }}
                              />
                            </div>
                            {/* 2b. 异常分支流程（flows.alternate_flows） */}
                            {(() => {
                              const altFlows = (fp.srr_json as any).flows?.alternate_flows || []
                              if (!altFlows.length) return null
                              return (
                                <div className="md:col-span-2">
                                  <FieldEditor
                                    label="异常分支流程"
                                    fieldPath="flows.alternate_flows"
                                    value={altFlows}
                                    valueType="object[]"
                                    onSave={saveFpField(fp.id)}
                                    disabled={false}
                                    render={(v) => {
                                      const flows = Array.isArray(v) ? v : []
                                      if (!flows.length) return <p className="text-xs text-slate-400 italic">暂无异常分支</p>
                                      return (
                                        <div className="mt-1 space-y-1">
                                          {flows.map((f: any, i: number) => (
                                            <div key={i} className="text-xs text-slate-600 pl-3 border-l-2 border-amber-200">
                                              <span className="font-medium text-slate-700">{f.name}</span>
                                              <span className="text-slate-400 mx-1">·</span>
                                              <span className="text-slate-500">触发：{f.trigger}</span>
                                              <span className="text-slate-400 mx-1">→</span>
                                              <span className="text-amber-600">{f.expect}</span>
                                            </div>
                                          ))}
                                        </div>
                                      )
                                    }}
                                  />
                                </div>
                              )
                            })()}
                            {/* 3. 业务规则（v2.48: 合并了原验收标准内容） */}
                            <FieldEditor
                              label="业务规则"
                              fieldPath="business_rules"
                              value={(fp.srr_json as any).business_rules || []}
                              valueType="object[]"
                              onSave={saveFpField(fp.id)}
                              disabled={false}
                              render={(v) => {
                                const rules = Array.isArray(v) ? v : []
                                if (!rules.length) return <p className="text-xs text-slate-400 italic">暂无</p>
                                return (
                                  <ul className="mt-1 space-y-0.5">
                                    {rules.map((r: any, i: number) => (
                                      <li key={i} className="text-xs text-slate-600 pl-3 border-l-2 border-slate-200">
                                        {typeof r === 'string' ? r : <><span className="text-slate-400">{r.id}</span> <span className="text-[10px] px-1 bg-slate-100 rounded">{brTypeLabel(r.type)}</span> {r.rule}</>}
                                      </li>
                                    ))}
                                  </ul>
                                )
                              }}
                            />
                            {/* v2.48: 移除验收标准 FieldEditor（已合并到业务规则） */}
                            {/* 4. 数据约束 */}
                            {(() => {
                              const dc = (fp.srr_json as any).data_constraints || []
                              if (!Array.isArray(dc) || !dc.length) return null
                              return (
                                <div className="md:col-span-2">
                                  <FieldEditor
                                    label="数据约束"
                                    fieldPath="data_constraints"
                                    value={dc}
                                    valueType="object[]"
                                    onSave={saveFpField(fp.id)}
                                    disabled={false}
                                    render={(v) => {
                                      const items = Array.isArray(v) ? v : []
                                      if (!items.length) return <p className="text-xs text-slate-400 italic">暂无</p>
                                      return (
                                        <div className="mt-1 overflow-x-auto">
                                          <table className="w-full text-xs">
                                            <thead>
                                              <tr className="text-slate-400 border-b border-slate-200">
                                                <th className="text-left py-1 pr-2">字段</th>
                                                <th className="text-left py-1 px-2">类型</th>
                                                <th className="text-left py-1 px-2">必填</th>
                                                <th className="text-left py-1 px-2">约束</th>
                                                {items.some((d: any) => d.example) && <th className="text-left py-1 pl-2">示例</th>}
                                              </tr>
                                            </thead>
                                            <tbody>
                                              {items.map((d: any, i: number) => (
                                                <tr key={i} className="border-b border-slate-100">
                                                  <td className="py-1 pr-2 text-slate-700 font-medium">{d.field}</td>
                                                  <td className="py-1 px-2 text-slate-500">{dataTypeLabel(d.type)}</td>
                                                  <td className="py-1 px-2">
                                                    {d.required ? <span className="text-rose-500">必填</span> : <span className="text-slate-400">可选</span>}
                                                  </td>
                                                  <td className="py-1 px-2 text-slate-600">{d.constraints}</td>
                                                  {items.some((d2: any) => d2.example) && <td className="py-1 pl-2 text-slate-400 italic">{d.example || '-'}</td>}
                                                </tr>
                                              ))}
                                            </tbody>
                                          </table>
                                        </div>
                                      )
                                    }}
                                  />
                                </div>
                              )
                            })()}
                            {/* 6. 非功能需求 */}
                            {(() => {
                              const nf = (fp.srr_json as any).non_functional
                              if (!nf || typeof nf !== 'object' || Object.keys(nf).length === 0) return null
                              const items = [
                                { key: 'performance', label: '性能', value: nf.performance },
                                { key: 'security', label: '安全', value: nf.security },
                                { key: 'compatibility', label: '兼容性', value: nf.compatibility },
                                { key: 'accessibility', label: '可访问性', value: nf.accessibility },
                              ].filter(it => it.value)
                              if (!items.length) return null
                              return (
                                <div className="md:col-span-2">
                                  <span className="text-[10px] text-slate-400 uppercase tracking-wider">非功能需求</span>
                                  <div className="mt-1 grid grid-cols-2 gap-2">
                                    {items.map(it => (
                                      <div key={it.key} className="text-xs px-2 py-1 bg-slate-50 rounded border border-slate-100">
                                        <span className="text-slate-400 mr-1">{it.label}：</span>
                                        <span className="text-slate-600">{it.value}</span>
                                      </div>
                                    ))}
                                  </div>
                                </div>
                              )
                            })()}
                          </div>
                        )}

                        {/* v2.48: 移除测试提示展示（不再生成 test_hints） */}

                        {/* 依赖关系 */}
                        {depsForFp.length > 0 && (
                          <div>
                            <div className="flex items-center gap-2 mb-1">
                              <span className="text-[10px] text-slate-400 uppercase tracking-wider">依赖关系</span>
                              <button
                                onClick={() => navigate(`/dependency-graph?productId=${productId || ''}&fp=${fp.id}`)}
                                className="text-[10px] text-primary-500 hover:underline flex items-center gap-0.5"
                              >
                                <ExternalLink className="w-3 h-3" /> 查看图谱
                              </button>
                            </div>
                            <div className="space-y-1">
                              {depsForFp.map((d, i) => {
                                const other = d.source_fp_id === fp.id ? d.target_fp_id : d.source_fp_id
                                const label = RELATION_LABELS[d.relation_type] || d.relation_type
                                const dir = d.source_fp_id === fp.id ? '→' : '←'
                                return (
                                  <div key={i} className="text-xs text-slate-500 flex items-center gap-1">
                                    <span className="px-1 py-0.5 bg-slate-100 rounded text-[10px]">{label}</span>
                                    {d.source_fp_id === fp.id && (
                                      <span>{fpNameMap[other] || other}</span>
                                    )}
                                    <span className="text-slate-300">{dir}</span>
                                    {d.target_fp_id === fp.id && (
                                      <span>{fpNameMap[other] || other}</span>
                                    )}
                                  </div>
                                )
                              })}
                            </div>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )
        })
      )}

      {/* 合并弹窗 */}
      {mergeModal && (
        <div className="fixed inset-0 z-50 bg-black/30 backdrop-blur-sm flex items-center justify-center p-4" onClick={() => setMergeModal(false)}>
          <div className="bg-white rounded-2xl shadow-xl max-w-md w-full p-6 animate-slide-up" onClick={e => e.stopPropagation()}>
            <h2 className="text-lg font-semibold text-slate-800 flex items-center gap-2">
              <GitBranch className="w-5 h-5 text-primary-500" /> 合并功能点
            </h2>
            <p className="text-sm text-slate-500 mt-2">
              将 {selected.size} 个选中的功能点合并为一个。LLM 将重新整理子 SRR，保证功能不遗漏。
            </p>
            <label className="block mt-3 text-xs text-slate-500">合并后名称</label>
            <input value={mergeName} onChange={e => setMergeName(e.target.value)}
              className="input-field mt-1 text-sm" placeholder="留空则 LLM 自动生成" autoFocus />
            <div className="flex items-center justify-end gap-3 mt-4">
              <button onClick={() => setMergeModal(false)} className="btn-secondary">取消</button>
              <button onClick={handleMerge} disabled={merging}
                className="btn-primary flex items-center gap-2 disabled:opacity-50">
                {merging ? <Loader2 className="w-4 h-4 animate-spin" /> : <GitBranch className="w-4 h-4" />}
                确认合并
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 编辑 FP 子 SRR 弹窗 */}
      {editSrrFpId && (
        <div className="fixed inset-0 z-50 bg-black/30 backdrop-blur-sm flex items-center justify-center p-4" onClick={() => setEditSrrFpId(null)}>
          <div className="bg-white rounded-2xl shadow-xl max-w-2xl w-full max-h-[80vh] p-6 animate-slide-up" onClick={e => e.stopPropagation()}>
            <h2 className="text-lg font-semibold text-slate-800 flex items-center gap-2">
              <FileCode className="w-5 h-5 text-indigo-500" /> 编辑子 SRR
            </h2>
            <p className="text-xs text-slate-400 mt-1 mb-3">修改 JSON 后点击保存，将替换当前功能点的子 SRR。</p>
            <textarea value={editSrrJson} onChange={e => setEditSrrJson(e.target.value)}
              className="input-field w-full text-xs font-mono resize-none" rows={20} autoFocus />
            <div className="flex items-center justify-end gap-3 mt-4">
              <button onClick={() => setEditSrrFpId(null)} className="btn-secondary">取消</button>
              <button onClick={handleSaveSrr} className="btn-primary flex items-center gap-2">
                <Save className="w-4 h-4" /> 保存子 SRR
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 影响分析弹窗 */}
      {impactModal && (
        <div className="fixed inset-0 z-50 bg-black/30 backdrop-blur-sm flex items-center justify-center p-4" onClick={() => setImpactModal(null)}>
          <div className="bg-white rounded-2xl shadow-xl max-w-lg w-full p-6 animate-slide-up max-h-[80vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
            <h2 className="text-lg font-semibold text-slate-800 flex items-center gap-2">
              <Share2 className="w-5 h-5 text-indigo-500" /> 影响分析
            </h2>
            <p className="text-sm text-slate-500 mt-2 mb-4 whitespace-pre-line">{impactModal.impact_summary}</p>

            {impactModal.affected_modules?.length > 0 && (
              <div className="space-y-3">
                <span className="text-xs text-slate-400 uppercase tracking-wider">受影响模块</span>
                {impactModal.affected_modules.map((mod: any) => (
                  <div key={mod.module_id} className="p-3 bg-slate-50 rounded-xl">
                    <p className="text-xs font-medium text-slate-600 mb-1">
                      {mod.module_id === '_unassigned' ? '未分配模块' : mod.module_id}
                    </p>
                    <div className="flex flex-wrap gap-1">
                      {mod.fp_names.map((n: string, i: number) => (
                        <span key={i} className="text-xs px-2 py-0.5 bg-white border border-slate-200 rounded text-slate-600">
                          {n}
                        </span>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            )}

            {impactModal.total === 0 && (
              <p className="text-sm text-slate-400 py-4 text-center">未检测到受影响的功能点。</p>
            )}

            <div className="flex items-center justify-between mt-4 pt-3 border-t border-slate-100">
              <p className="text-xs text-slate-400">
                影响范围可确认后作为测试用例生成的参考来源
              </p>
              <button onClick={() => setImpactModal(null)} className="btn-secondary">关闭</button>
            </div>
          </div>
        </div>
      )}

      {/* v2.42: 模块选择器 Portal —— 渲染到 body，避免被父容器 overflow 遮挡 */}
      {showFpModulePicker && fpPickerPos && createPortal(
        <div
          ref={fpPickerRef}
          className="fixed z-[100]"
          style={{
            top: fpPickerPos.placement === 'bottom' ? fpPickerPos.top : undefined,
            bottom: fpPickerPos.placement === 'top' ? (window.innerHeight - fpPickerPos.top) : undefined,
            left: fpPickerPos.left,
            width: fpPickerPos.width,
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
    </div>
  )
}
