import { useState, useMemo } from 'react'
import {
  ChevronDown, ChevronRight, Edit3, Trash2, Check, X,
  Plus, Folder, FileText, ListChecks, AlertTriangle, Shield, Zap,
  Layers, CircleDot,
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

export default function XMindEditor({
  cases, reqs, selectedProjectName,
  onEdit, onDelete, onApprove, onReject, onAddNew,
}: Props) {
  // 按需求 → 功能点 → 用例 三层分组
  const tree = useMemo(() => {
    const reqMap = new Map<string, Map<string, TestCase[]>>()
    for (const c of cases) {
      const reqId = c.req_id || '未分类'
      const fp = c.functional_point || '默认功能点'
      if (!reqMap.has(reqId)) reqMap.set(reqId, new Map())
      const fpMap = reqMap.get(reqId)!
      if (!fpMap.has(fp)) fpMap.set(fp, [])
      fpMap.get(fp)!.push(c)
    }
    return Array.from(reqMap.entries()).map(([reqId, fpMap]) => ({
      reqId,
      reqTitle: reqs.find(r => r.id === reqId)?.title || reqId,
      fps: Array.from(fpMap.entries()).map(([fp, cs]) => ({
        fp,
        cases: cs.sort((a, b) => a.id.localeCompare(b.id)),
      })),
    }))
  }, [cases, reqs])

  // 展开状态
  const [expandedReqs, setExpandedReqs] = useState<Set<string>>(new Set(tree.map(t => t.reqId)))
  const [expandedFps, setExpandedFps] = useState<Set<string>>(new Set())
  const [expandedCases, setExpandedCases] = useState<Set<string>>(new Set())
  const [busyId, setBusyId] = useState<string>('')

  const toggle = (set: Set<string>, setFn: (s: Set<string>) => void, key: string) => {
    setFn(new Set(set).has(key) ? (new Set(set).delete(key), set) : new Set(set).add(key))
  }

  const handleApprove = async (id: string) => {
    setBusyId(id)
    try { await onApprove(id) } finally { setBusyId('') }
  }
  const handleReject = async (id: string) => {
    setBusyId(id)
    try { await onReject(id) } finally { setBusyId('') }
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
          <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-slate-300"></span><span className="text-slate-500">草稿 {draftCount}</span></span>
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
            </div>

            {/* 一级：需求 */}
            <div className="pl-4 space-y-1.5">
              {tree.map(({ reqId, reqTitle, fps }) => {
                const reqExpanded = expandedReqs.has(reqId)
                const reqCaseCount = fps.reduce((s, f) => s + f.cases.length, 0)
                return (
                  <div key={reqId} className="border-l-2 border-blue-200 pl-3">
                    <button
                      onClick={() => toggle(expandedReqs, setExpandedReqs, reqId)}
                      className="w-full flex items-center gap-2 py-1.5 hover:bg-blue-50/40 rounded-md px-2 -ml-2 transition-colors text-left group"
                    >
                      {reqExpanded ? <ChevronDown className="w-3.5 h-3.5 text-slate-400" /> : <ChevronRight className="w-3.5 h-3.5 text-slate-400" />}
                      <div className="w-5 h-5 rounded bg-blue-100 flex items-center justify-center">
                        <FileText className="w-3 h-3 text-blue-600" />
                      </div>
                      <span className="text-sm font-medium text-slate-700 truncate flex-1">{reqTitle}</span>
                      <span className="text-xs font-mono text-slate-400">{reqId}</span>
                      <span className="badge bg-blue-50 text-blue-600 text-xs">{reqCaseCount}</span>
                    </button>

                    {reqExpanded && (
                      <div className="pl-5 space-y-1">
                        {/* 二级：功能点 */}
                        {fps.map(({ fp, cases: fpCases }) => {
                          const fpKey = `${reqId}::${fp}`
                          const fpExpanded = expandedFps.has(fpKey)
                          const fpApproved = fpCases.filter(c => c.status === 'approved').length
                          return (
                            <div key={fpKey} className="border-l border-purple-200 pl-3">
                              <button
                                onClick={() => toggle(expandedFps, setExpandedFps, fpKey)}
                                className="w-full flex items-center gap-2 py-1.5 hover:bg-purple-50/40 rounded-md px-2 -ml-2 transition-colors text-left"
                              >
                                {fpExpanded ? <ChevronDown className="w-3 h-3 text-slate-400" /> : <ChevronRight className="w-3 h-3 text-slate-400" />}
                                <div className="w-5 h-5 rounded bg-purple-100 flex items-center justify-center">
                                  <Layers className="w-3 h-3 text-purple-600" />
                                </div>
                                <span className="text-sm text-slate-700 truncate flex-1">{fp}</span>
                                <span className="badge bg-purple-50 text-purple-600 text-xs">{fpApproved}/{fpCases.length}</span>
                              </button>

                              {fpExpanded && (
                                <div className="pl-5 space-y-1.5 mt-1">
                                  {/* 三级：用例 */}
                                  {fpCases.map(c => {
                                    const caseExpanded = expandedCases.has(c.id)
                                    const TypeIcon = TYPE_ICONS[c.type] || FileText
                                    const typeColor = TYPE_COLORS[c.type] || 'text-slate-500'
                                    return (
                                      <div key={c.id} className="border border-slate-200 rounded-lg bg-white hover:shadow-sm transition-shadow">
                                        {/* 用例头 */}
                                        <div className="flex items-center gap-2 px-3 py-2">
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
          </div>
        )}
      </div>
    </div>
  )
}
