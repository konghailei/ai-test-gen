import { useState, useMemo, useCallback } from 'react'
import {
  ChevronDown, ChevronRight, Edit3, Trash2, Check, X,
  Plus, Folder, FileText, ListChecks, AlertTriangle, Shield, Zap,
  Layers, CircleDot, CheckCircle2, FolderPlus, GripVertical,
} from 'lucide-react'
import type { TestCase, Requirement } from '../types'
import { priorityBadge, statusBadge, confidenceColor, CASE_STATUS_LABELS } from '../utils'
import { caseTypeLabel } from '../utils/labels'

interface Props {
  cases: TestCase[]
  reqs: Requirement[]
  selectedProjectName: string
  onEdit: (c: TestCase) => void
  onDelete: (id: string) => void
  onApprove: (id: string) => Promise<void>
  onReject: (id: string) => Promise<void>
  onAddNew: () => void
  /** 在指定需求/功能点下新增用例（预填充 req_id 和 functional_point） */
  onAddCaseAtLevel: (reqId: string, fp: string) => void
  /** 拖拽移动用例到新的需求/功能点 */
  onMoveCase: (caseId: string, newReqId: string, newFp: string) => Promise<void>
  /** 批量审核（一键审核目录下所有用例） */
  onBatchApprove: (caseIds: string[], accepted: boolean) => Promise<void>
  /** 删除空目录（仅对虚拟目录生效） */
  onDeleteDir?: (reqId: string, fp: string) => void
}

const TYPE_ICONS: Record<string, typeof FileText> = {
  functional: FileText,
  boundary: Layers,
  exception: AlertTriangle,
  security: Shield,
  performance: Zap,
  regression: CircleDot,
  integration: Layers,
  ui: FileText,
  api: CircleDot,
  data: Layers,
}

const TYPE_COLORS: Record<string, string> = {
  functional: 'text-blue-500',
  boundary: 'text-purple-500',
  exception: 'text-amber-500',
  security: 'text-red-500',
  performance: 'text-emerald-500',
  regression: 'text-cyan-500',
  integration: 'text-indigo-500',
  ui: 'text-pink-500',
  api: 'text-teal-500',
  data: 'text-slate-500',
}

/** 拖拽数据格式 */
const DRAG_FORMAT = 'application/xmind-drag'

interface DragData {
  type: 'case' | 'fp'
  caseId?: string
  reqId?: string
  fp?: string
}

export default function XMindEditor({
  cases, reqs, selectedProjectName,
  onEdit, onDelete, onApprove, onReject, onAddNew,
  onAddCaseAtLevel, onMoveCase, onBatchApprove, onDeleteDir,
}: Props) {
  // 虚拟目录（空功能点，尚未有用例）
  const [virtualDirs, setVirtualDirs] = useState<Set<string>>(new Set())
  const [dragOverKey, setDragOverKey] = useState('')
  const [batchBusyKey, setBatchBusyKey] = useState('')

  // 业务场景常量：无 functional_point 的用例归入此节点（对齐需求分析 XMind 导出结构）
  const BIZ_SCENE = '业务场景'

  // 按需求 → [业务场景 + 功能点N（平级）] → 应用场景分类 → 用例
  // 对齐需求分析 XMind 导出："业务场景"和"功能点"为同一层级
  const tree = useMemo(() => {
    const reqMap = new Map<string, Map<string, Map<string, TestCase[]>>>()
    for (const c of cases) {
      const reqId = c.req_id || '未分类'
      // 无 functional_point 的用例归入"业务场景"，与功能点平级
      const fp = c.functional_point?.trim() ? c.functional_point : BIZ_SCENE
      const type = c.type || 'other'
      if (!reqMap.has(reqId)) reqMap.set(reqId, new Map())
      const fpMap = reqMap.get(reqId)!
      if (!fpMap.has(fp)) fpMap.set(fp, new Map())
      const typeMap = fpMap.get(fp)!
      if (!typeMap.has(type)) typeMap.set(type, [])
      typeMap.get(type)!.push(c)
    }
    // 合并虚拟目录
    for (const vKey of virtualDirs) {
      const [reqId, fp] = vKey.split('::', 2)
      if (!reqId || !fp) continue
      if (!reqMap.has(reqId)) reqMap.set(reqId, new Map())
      const fpMap = reqMap.get(reqId)!
      if (!fpMap.has(fp)) fpMap.set(fp, new Map())
    }
    return Array.from(reqMap.entries()).map(([reqId, fpMap]) => {
      const fps = Array.from(fpMap.entries()).map(([fp, typeMap]) => ({
        fp,
        isBizScene: fp === BIZ_SCENE,
        types: Array.from(typeMap.entries()).map(([type, cs]) => ({
          type,
          cases: cs.sort((a, b) => a.id.localeCompare(b.id)),
        })),
      }))
      // 业务场景排第一位（与需求分析导出一致）
      fps.sort((a, b) => {
        if (a.isBizScene) return -1
        if (b.isBizScene) return 1
        return a.fp.localeCompare(b.fp)
      })
      return {
        reqId,
        // 优先用 reqs 中的标题，其次用用例自带的 req_title，最后回退到 reqId
        reqTitle: reqs.find(r => r.id === reqId)?.title
          || cases.find(c => c.req_id === reqId)?.req_title
          || reqId,
        fps,
      }
    })
  }, [cases, reqs, virtualDirs])

  // 展开状态
  const [expandedReqs, setExpandedReqs] = useState<Set<string>>(new Set(tree.map(t => t.reqId)))
  const [expandedFps, setExpandedFps] = useState<Set<string>>(new Set())
  const [expandedTypes, setExpandedTypes] = useState<Set<string>>(new Set())
  const [expandedCases, setExpandedCases] = useState<Set<string>>(new Set())
  const [busyId, setBusyId] = useState<string>('')

  const toggle = useCallback((set: Set<string>, setFn: (s: Set<string>) => void, key: string) => {
    const next = new Set(set)
    if (next.has(key)) next.delete(key)
    else next.add(key)
    setFn(next)
  }, [])

  const handleApprove = async (id: string) => {
    setBusyId(id)
    try { await onApprove(id) } finally { setBusyId('') }
  }
  const handleReject = async (id: string) => {
    setBusyId(id)
    try { await onReject(id) } finally { setBusyId('') }
  }

  // 新增目录（功能点）
  const handleAddDir = (reqId: string, parentFp?: string) => {
    if (!reqId || reqId === '未分类') {
      alert('请先选择一个需求，再添加目录')
      return
    }
    const name = prompt(parentFp ? `在「${parentFp}」下新建子目录名称：` : '请输入新目录（功能点）名称：')
    if (!name?.trim()) return
    const fpName = parentFp ? `${parentFp} / ${name.trim()}` : name.trim()
    const vKey = `${reqId}::${fpName}`
    setVirtualDirs(prev => new Set(prev).add(vKey))
    // 自动展开该需求
    setExpandedReqs(prev => new Set(prev).add(reqId))
  }

  // 批量审核
  const handleBatchApprove = async (key: string, caseIds: string[], accepted: boolean) => {
    if (caseIds.length === 0) { alert('该目录下没有用例'); return }
    const action = accepted ? '通过' : '驳回'
    if (!confirm(`确认一键${action}「${key}」下的 ${caseIds.length} 个用例？`)) return
    setBatchBusyKey(key)
    try { await onBatchApprove(caseIds, accepted) }
    catch (e: any) { alert('批量审核失败: ' + e.message) }
    finally { setBatchBusyKey('') }
  }

  // 删除空目录（仅虚拟目录可删）
  const handleDeleteDir = (reqId: string, fp: string) => {
    if (fp === BIZ_SCENE) { alert('「业务场景」为系统默认分类，不可删除'); return }
    const vKey = `${reqId}::${fp}`
    // 检查是否有实际用例
    const hasCases = cases.some(c => c.req_id === reqId && (c.functional_point?.trim() ? c.functional_point : BIZ_SCENE) === fp)
    if (hasCases) { alert('该目录下仍有用例，请先移走或删除用例后再删除目录'); return }
    if (!virtualDirs.has(vKey)) { alert('该目录非虚拟目录，无法在此删除'); return }
    if (!confirm(`确认删除空目录「${fp}」？`)) return
    setVirtualDirs(prev => { const next = new Set(prev); next.delete(vKey); return next })
    if (onDeleteDir) onDeleteDir(reqId, fp)
  }

  // 拖拽处理
  const handleDragStart = (e: React.DragEvent, data: DragData) => {
    e.dataTransfer.setData(DRAG_FORMAT, JSON.stringify(data))
    e.dataTransfer.effectAllowed = 'move'
  }
  const handleDragOver = (e: React.DragEvent, key: string) => {
    e.preventDefault()
    e.dataTransfer.dropEffect = 'move'
    setDragOverKey(key)
  }
  const handleDrop = async (e: React.DragEvent, targetReqId: string, targetFp: string) => {
    e.preventDefault()
    setDragOverKey('')
    const raw = e.dataTransfer.getData(DRAG_FORMAT)
    if (!raw) return
    const data: DragData = JSON.parse(raw)
    if (data.type === 'case' && data.caseId) {
      // 移动单个用例
      if (data.reqId === targetReqId && data.fp === targetFp) return // 同位置不处理
      try { await onMoveCase(data.caseId, targetReqId, targetFp) }
      catch (e: any) { alert('移动用例失败: ' + e.message) }
    }
  }

  const totalCases = cases.length
  const approvedCount = cases.filter(c => c.status === 'approved').length
  const rejectedCount = cases.filter(c => c.status === 'rejected').length
  const reviewedCount = cases.filter(c => c.status === 'reviewed').length
  const draftCount = cases.filter(c => c.status === 'draft').length

  return (
    <div className="card p-0 overflow-hidden">
      {/* 工具栏 */}
      <div className="flex items-center justify-between px-5 py-3 border-b border-slate-100 bg-gradient-to-r from-primary-50/50 to-transparent">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-primary-100 flex items-center justify-center">
            <span className="text-primary-600 font-bold text-sm">{selectedProjectName.charAt(0) || '?'}</span>
          </div>
          <div>
            <div className="text-sm font-semibold text-slate-800">{selectedProjectName || '未选择项目'}</div>
            <div className="text-xs text-slate-400">
              {totalCases} 个用例 · {tree.length} 个需求 · {tree.reduce((s, t) => s + t.fps.length, 0)} 个功能点
            </div>
          </div>
        </div>
        <div className="flex items-center gap-3 text-xs">
          <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-emerald-500"></span><span className="text-slate-500">通过 {approvedCount}</span></span>
          <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-amber-500"></span><span className="text-slate-500">已审核 {reviewedCount}</span></span>
          <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-red-500"></span><span className="text-slate-500">驳回 {rejectedCount}</span></span>
          <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-slate-300"></span><span className="text-slate-500">未审核 {draftCount}</span></span>
          <button onClick={onAddNew} className="btn-secondary text-xs flex items-center gap-1 ml-2 py-1">
            <Plus className="w-3 h-3" />新增用例
          </button>
        </div>
      </div>

      {/* 树形结构 */}
      <div className="p-5 max-h-[calc(100vh-260px)] overflow-auto">
        {tree.length === 0 ? (
          <div className="text-center py-12">
            <Folder className="w-12 h-12 mx-auto mb-3 text-slate-200" />
            <p className="text-slate-400 text-sm">暂无用例，请先生成或新增</p>
          </div>
        ) : (
          <div className="space-y-2">
            {/* 根节点：项目 */}
            <div className="flex items-center gap-2 pb-2 border-b-2 border-primary-200">
              <div className="w-7 h-7 rounded-md bg-primary-500 flex items-center justify-center text-white">
                <Folder className="w-4 h-4" />
              </div>
              <span className="text-sm font-bold text-slate-800">{selectedProjectName || '所有用例'}</span>
              <span className="badge bg-primary-100 text-primary-700 text-xs">{totalCases}</span>
              {/* 根级操作 */}
              <div className="flex items-center gap-1 ml-auto">
                {totalCases > 0 && (
                  <button
                    onClick={() => handleBatchApprove('根目录', cases.map(c => c.id), true)}
                    disabled={batchBusyKey === '根目录'}
                    className="btn-ghost text-xs flex items-center gap-1 px-2 py-1 text-slate-500 hover:text-emerald-600 disabled:opacity-30"
                    title="一键审核全部用例"
                  >
                    {batchBusyKey === '根目录' ? <span className="block w-3 h-3 border-2 border-slate-300 border-t-emerald-500 rounded-full animate-spin" /> : <CheckCircle2 className="w-3.5 h-3.5" />}
                    一键审核
                  </button>
                )}
              </div>
            </div>

            {/* 一级：需求 */}
            <div className="pl-4 space-y-1.5">
              {tree.map(({ reqId, reqTitle, fps }) => {
                const reqExpanded = expandedReqs.has(reqId)
                const allReqCases = fps.flatMap(f => f.types.flatMap(t => t.cases))
                const reqCaseCount = allReqCases.length
                const reqKey = `req::${reqId}`
                return (
                  <div key={reqId} className="border-l-2 border-blue-200 pl-3">
                    <div
                      className={`w-full flex items-center gap-2 py-1.5 px-2 -ml-2 rounded-md transition-colors group ${dragOverKey === reqKey ? 'bg-blue-100 ring-1 ring-blue-300' : 'hover:bg-blue-50/40'}`}
                      onDragOver={(e) => handleDragOver(e, reqKey)}
                      onDragLeave={() => setDragOverKey('')}
                      onDrop={(e) => { handleDrop(e, reqId, ''); setExpandedReqs(prev => new Set(prev).add(reqId)) }}
                    >
                      <button
                        onClick={() => toggle(expandedReqs, setExpandedReqs, reqId)}
                        className="flex items-center gap-2 flex-1 min-w-0 text-left"
                      >
                        {reqExpanded ? <ChevronDown className="w-3.5 h-3.5 text-slate-400 shrink-0" /> : <ChevronRight className="w-3.5 h-3.5 text-slate-400 shrink-0" />}
                        <div className="w-5 h-5 rounded bg-blue-100 flex items-center justify-center shrink-0">
                          <FileText className="w-3 h-3 text-blue-600" />
                        </div>
                        <span className="text-sm font-medium text-slate-700 truncate">{reqTitle}</span>
                        {reqTitle !== reqId && (
                          <span className="text-xs font-mono text-slate-400 shrink-0">{reqId}</span>
                        )}
                      </button>
                      <span className="badge bg-blue-50 text-blue-600 text-xs shrink-0">{reqCaseCount}</span>
                      {/* 需求级操作按钮 */}
                      <div className="flex items-center gap-0.5 shrink-0 opacity-0 group-hover:opacity-100 transition-opacity">
                        <button
                          onClick={() => onAddCaseAtLevel(reqId, '')}
                          className="btn-ghost p-1 text-slate-400 hover:text-primary-600"
                          title="在此需求下新增用例"
                        ><Plus className="w-3.5 h-3.5" /></button>
                        <button
                          onClick={() => handleAddDir(reqId)}
                          className="btn-ghost p-1 text-slate-400 hover:text-purple-600"
                          title="在此需求下新增目录"
                        ><FolderPlus className="w-3.5 h-3.5" /></button>
                        {reqCaseCount > 0 && (
                          <button
                            onClick={() => handleBatchApprove(reqKey, allReqCases.map(c => c.id), true)}
                            disabled={batchBusyKey === reqKey}
                            className="btn-ghost p-1 text-slate-400 hover:text-emerald-600 disabled:opacity-30"
                            title="一键审核此需求下所有用例"
                          >
                            {batchBusyKey === reqKey ? <span className="block w-3.5 h-3.5 border-2 border-slate-300 border-t-emerald-500 rounded-full animate-spin" /> : <CheckCircle2 className="w-3.5 h-3.5" />}
                          </button>
                        )}
                      </div>
                    </div>

                    {reqExpanded && (
                      <div className="pl-5 space-y-1">
                        {/* 二级：业务场景 + 功能点（平级，对齐需求分析 XMind 导出） */}
                        {fps.map(({ fp, types, isBizScene }) => {
                          const fpKey = `${reqId}::${fp}`
                          const dragKey = `fp::${fpKey}`
                          const fpExpanded = expandedFps.has(fpKey)
                          const fpCases = types.flatMap(t => t.cases)
                          const fpApproved = fpCases.filter(c => c.status === 'approved').length
                          const fpEmpty = fpCases.length === 0
                          // 业务场景用蓝色图标，功能点用紫色图标
                          const fpIconBg = isBizScene ? 'bg-blue-100' : 'bg-purple-100'
                          const fpIconColor = isBizScene ? 'text-blue-600' : 'text-purple-600'
                          const FpIcon = isBizScene ? FileText : Layers
                          return (
                            <div key={fpKey} className={`border-l pl-3 ${isBizScene ? 'border-blue-200' : 'border-purple-200'}`}>
                              <div
                                className={`w-full flex items-center gap-2 py-1.5 px-2 -ml-2 rounded-md transition-colors group ${dragOverKey === dragKey ? 'bg-purple-100 ring-1 ring-purple-300' : 'hover:bg-purple-50/40'}`}
                                onDragOver={(e) => handleDragOver(e, dragKey)}
                                onDragLeave={() => setDragOverKey('')}
                                onDrop={(e) => { handleDrop(e, reqId, isBizScene ? '' : fp); setExpandedFps(prev => new Set(prev).add(fpKey)) }}
                              >
                                <button
                                  onClick={() => toggle(expandedFps, setExpandedFps, fpKey)}
                                  className="flex items-center gap-2 flex-1 min-w-0 text-left"
                                >
                                  {fpExpanded ? <ChevronDown className="w-3 h-3 text-slate-400 shrink-0" /> : <ChevronRight className="w-3 h-3 text-slate-400 shrink-0" />}
                                  <div className={`w-5 h-5 rounded ${fpIconBg} flex items-center justify-center shrink-0`}>
                                    <FpIcon className={`w-3 h-3 ${fpIconColor}`} />
                                  </div>
                                  <span className="text-sm text-slate-700 truncate">{fp}</span>
                                </button>
                                <span className={`badge text-xs shrink-0 ${fpEmpty ? 'bg-slate-100 text-slate-400' : isBizScene ? 'bg-blue-50 text-blue-600' : 'bg-purple-50 text-purple-600'}`}>{fpApproved}/{fpCases.length}</span>
                                {/* 功能点级操作按钮 */}
                                <div className="flex items-center gap-0.5 shrink-0 opacity-0 group-hover:opacity-100 transition-opacity">
                                  <button
                                    onClick={() => onAddCaseAtLevel(reqId, isBizScene ? '' : fp)}
                                    className="btn-ghost p-1 text-slate-400 hover:text-primary-600"
                                    title="在此目录下新增用例"
                                  ><Plus className="w-3.5 h-3.5" /></button>
                                  {!isBizScene && (
                                    <button
                                      onClick={() => handleAddDir(reqId, fp)}
                                      className="btn-ghost p-1 text-slate-400 hover:text-purple-600"
                                      title="在此目录下新增子目录"
                                    ><FolderPlus className="w-3.5 h-3.5" /></button>
                                  )}
                                  {fpCases.length > 0 && (
                                    <button
                                      onClick={() => handleBatchApprove(fpKey, fpCases.map(c => c.id), true)}
                                      disabled={batchBusyKey === fpKey}
                                      className="btn-ghost p-1 text-slate-400 hover:text-emerald-600 disabled:opacity-30"
                                      title="一键审核此目录下所有用例"
                                    >
                                      {batchBusyKey === fpKey ? <span className="block w-3.5 h-3.5 border-2 border-slate-300 border-t-emerald-500 rounded-full animate-spin" /> : <CheckCircle2 className="w-3.5 h-3.5" />}
                                    </button>
                                  )}
                                  {/* 删除空目录（仅虚拟目录可删） */}
                                  {!isBizScene && fpEmpty && (
                                    <button
                                      onClick={() => handleDeleteDir(reqId, fp)}
                                      className="btn-ghost p-1 text-slate-400 hover:text-red-500"
                                      title="删除此空目录"
                                    ><Trash2 className="w-3.5 h-3.5" /></button>
                                  )}
                                </div>
                              </div>

                              {fpExpanded && (
                                <div className="pl-5 space-y-1.5 mt-1">
                                  {/* 三级：应用场景分类（按 type 分组） */}
                                  {types.map(({ type, cases: typeCases }) => {
                                    const typeKey = `${fpKey}::${type}`
                                    const typeExpanded = expandedTypes.has(typeKey)
                                    const TypeIcon = TYPE_ICONS[type] || FileText
                                    const typeColor = TYPE_COLORS[type] || 'text-slate-500'
                                    const typeApproved = typeCases.filter(c => c.status === 'approved').length
                                    return (
                                      <div key={typeKey} className="border-l border-slate-200 pl-3">
                                        <div
                                          className="w-full flex items-center gap-2 py-1 px-2 -ml-2 rounded-md transition-colors group"
                                          onDragOver={(e) => handleDragOver(e, `type::${typeKey}`)}
                                          onDragLeave={() => setDragOverKey('')}
                                          onDrop={(e) => { handleDrop(e, reqId, fp); setExpandedTypes(prev => new Set(prev).add(typeKey)) }}
                                        >
                                          <button
                                            onClick={() => toggle(expandedTypes, setExpandedTypes, typeKey)}
                                            className="flex items-center gap-2 flex-1 min-w-0 text-left"
                                          >
                                            {typeExpanded ? <ChevronDown className="w-3 h-3 text-slate-400 shrink-0" /> : <ChevronRight className="w-3 h-3 text-slate-400 shrink-0" />}
                                            <div className="w-5 h-5 rounded bg-slate-50 flex items-center justify-center shrink-0">
                                              <TypeIcon className={`w-3 h-3 ${typeColor}`} />
                                            </div>
                                            <span className="text-sm text-slate-600 truncate">{caseTypeLabel(type)}</span>
                                          </button>
                                          <span className={`badge text-xs shrink-0 ${typeCases.length === 0 ? 'bg-slate-100 text-slate-400' : 'bg-slate-100 text-slate-600'}`}>{typeApproved}/{typeCases.length}</span>
                                        </div>

                                        {typeExpanded && (
                                          <div className="pl-5 space-y-1.5 mt-1">
                                            {/* 四级：用例 */}
                                            {typeCases.map(c => {
                                              const caseExpanded = expandedCases.has(c.id)
                                              return (
                                                <div
                                                  key={c.id}
                                                  draggable
                                                  onDragStart={(e) => handleDragStart(e, { type: 'case', caseId: c.id, reqId, fp })}
                                                  className="border border-slate-200 rounded-lg bg-white hover:shadow-sm transition-shadow cursor-grab active:cursor-grabbing"
                                                >
                                                  {/* 用例头 */}
                                                  <div className="flex items-center gap-2 px-3 py-2">
                                                    <GripVertical className="w-3 h-3 text-slate-300 shrink-0" />
                                                    <button
                                                      onClick={() => toggle(expandedCases, setExpandedCases, c.id)}
                                                      className="flex items-center gap-2 flex-1 min-w-0 text-left"
                                                    >
                                                      {caseExpanded ? <ChevronDown className="w-3 h-3 text-slate-400 shrink-0" /> : <ChevronRight className="w-3 h-3 text-slate-400 shrink-0" />}
                                                      <div className={`w-5 h-5 rounded flex items-center justify-center shrink-0 bg-slate-50`}>
                                                        <TypeIcon className={`w-3 h-3 ${typeColor}`} />
                                                      </div>
                                                      <span className="text-xs font-mono text-slate-400 shrink-0">{c.id.replace(/^TC-[^-]+-\d+-/, '')}</span>
                                                      <span className="text-sm text-slate-700 truncate">{c.title}</span>
                                                    </button>
                                                    <div className="flex items-center gap-1.5 shrink-0">
                                                      <span className="badge bg-slate-100 text-slate-600 text-xs">{caseTypeLabel(c.type)}</span>
                                                      <span className={priorityBadge(c.priority)}>{c.priority}</span>
                                                      <span className={statusBadge(c.status)}>{CASE_STATUS_LABELS[c.status] || c.status}</span>
                                                      <span className={`text-xs font-mono font-medium ${confidenceColor(c.confidence)}`}>{(c.confidence * 100).toFixed(0)}%</span>
                                                      {/* 操作按钮 */}
                                                      <div className="flex items-center gap-0.5 ml-1 pl-1.5 border-l border-slate-100">
                                                        {c.status !== 'approved' && (
                                                          <button
                                                            onClick={() => handleApprove(c.id)}
                                                            disabled={busyId === c.id}
                                                            className="btn-ghost p-1 text-slate-400 hover:text-emerald-600 disabled:opacity-30"
                                                            title="审核通过"
                                                          >
                                                            {busyId === c.id ? <span className="block w-3.5 h-3.5 border-2 border-slate-300 border-t-emerald-500 rounded-full animate-spin" /> : <Check className="w-3.5 h-3.5" />}
                                                          </button>
                                                        )}
                                                        {c.status !== 'rejected' && (
                                                          <button
                                                            onClick={() => handleReject(c.id)}
                                                            disabled={busyId === c.id}
                                                            className="btn-ghost p-1 text-slate-400 hover:text-red-500 disabled:opacity-30"
                                                            title="驳回"
                                                          >
                                                            <X className="w-3.5 h-3.5" />
                                                          </button>
                                                        )}
                                                        <button
                                                          onClick={() => onEdit(c)}
                                                          className="btn-ghost p-1 text-slate-400 hover:text-primary-600"
                                                          title="编辑"
                                                        >
                                                          <Edit3 className="w-3.5 h-3.5" />
                                                        </button>
                                                        <button
                                                          onClick={() => onDelete(c.id)}
                                                          className="btn-ghost p-1 text-slate-400 hover:text-red-500"
                                                          title="删除"
                                                        >
                                                          <Trash2 className="w-3.5 h-3.5" />
                                                        </button>
                                                      </div>
                                                    </div>
                                                  </div>

                                                  {/* 用例详情展开 */}
                                                  {caseExpanded && (
                                                    <div className="px-4 pb-3 pt-1 space-y-2 bg-slate-50/30 border-t border-slate-100">
                                                      {c.description && (
                                                        <div>
                                                          <p className="text-xs font-semibold text-slate-500 mb-0.5">描述</p>
                                                          <p className="text-xs text-slate-600">{c.description}</p>
                                                        </div>
                                                      )}
                                                      {c.preconditions && c.preconditions.length > 0 && (
                                                        <div>
                                                          <p className="text-xs font-semibold text-slate-500 mb-0.5 flex items-center gap-1"><AlertTriangle className="w-3 h-3" />前置条件</p>
                                                          <ul className="text-xs text-slate-600 list-disc list-inside space-y-0.5">
                                                            {c.preconditions.map((p, i) => <li key={i}>{p}</li>)}
                                                          </ul>
                                                        </div>
                                                      )}
                                                      {c.steps && c.steps.length > 0 && (
                                                        <div>
                                                          <p className="text-xs font-semibold text-slate-500 mb-0.5 flex items-center gap-1"><ListChecks className="w-3 h-3" />测试步骤</p>
                                                          <div className="space-y-1">
                                                            {c.steps.map((s, i) => (
                                                              <div key={i} className="grid grid-cols-12 gap-2 text-xs">
                                                                <span className="col-span-1 text-slate-400 font-mono">{s.step || i + 1}.</span>
                                                                <span className="col-span-6 text-slate-700">{s.action}</span>
                                                                <span className="col-span-5 text-slate-500">→ {s.expected}</span>
                                                              </div>
                                                            ))}
                                                          </div>
                                                        </div>
                                                      )}
                                                      {Array.isArray(c.case_json?.assertions) && (c.case_json!.assertions as any[]).length > 0 && (
                                                        <div>
                                                          <p className="text-xs font-semibold text-slate-500 mb-0.5 flex items-center gap-1"><Shield className="w-3 h-3" />断言</p>
                                                          <ul className="text-xs text-slate-600 list-disc list-inside space-y-0.5">
                                                            {(c.case_json!.assertions as any[]).map((a, i) => (
                                                              <li key={i}>{a.target} {a.comparison} {a.value}</li>
                                                            ))}
                                                          </ul>
                                                        </div>
                                                      )}
                                                    </div>
                                                  )}
                                                </div>
                                              )
                                            })}
                                            {typeCases.length === 0 && (
                                              <div className="text-center py-3 text-xs text-slate-400 italic">
                                                空分类
                                              </div>
                                            )}
                                          </div>
                                        )}
                                      </div>
                                    )
                                  })}
                                  {/* 空目录提示 */}
                                  {fpCases.length === 0 && (
                                    <div className="text-center py-3 text-xs text-slate-400 italic">
                                      空目录 — 点击 <Plus className="w-3 h-3 inline" /> 添加用例
                                    </div>
                                  )}
                                </div>
                              )}
                            </div>
                          )
                        })}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>

            {/* 拖拽提示 */}
            <div className="text-center text-xs text-slate-300 pt-2 border-t border-slate-100">
              <GripVertical className="w-3 h-3 inline" /> 拖拽用例到其他目录可移动 · 悬停节点显示操作按钮
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
