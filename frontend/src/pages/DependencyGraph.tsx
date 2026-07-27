import { useState, useEffect, useCallback, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  ReactFlow, useNodesState, useEdgesState, Background, Controls, MiniMap,
  MarkerType, ReactFlowProvider, Panel, Handle, Position,
  type Node, type Edge, type Connection, SelectionMode,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import {
  Search, Trash2, Edit3, GitBranch, Puzzle, Link2, GripVertical,
  Maximize, Minimize, Loader2, Unlink, X, ArrowLeftRight, Boxes,
  History, CheckCircle, XCircle, Clock, Filter,
} from 'lucide-react'
import { dependencyGraph, products as prodApi, requirements as reqApi } from '../api/client'
import { useProduct } from '../store/ProductContext'
import type { Module as ModuleType, FPDependencyHistoryItem } from '../types'

// ============================================================================
// v2.3 审核状态样式（FP 依赖图谱）
// ============================================================================
const REVIEW_STATUS_STYLES: Record<string, { label: string; stroke: string; dashed: boolean; bg: string; text: string }> = {
  pending: { label: '待审核', stroke: '#f59e0b', dashed: true, bg: 'bg-amber-50', text: 'text-amber-700' },
  approved: { label: '已审核', stroke: '#10b981', dashed: false, bg: 'bg-emerald-50', text: 'text-emerald-700' },
  rejected: { label: '已驳回', stroke: '#ef4444', dashed: true, bg: 'bg-red-50', text: 'text-red-700' },
}

// ============================================================================
// Module Node
// ============================================================================
function ModuleNode({ data, selected }: any) {
  return (
    <div className={[
      'relative px-4 py-3 bg-white rounded-2xl border-2 shadow-md min-w-[140px] transition-all duration-200 group',
      selected
        ? 'border-primary-400 shadow-primary-100 shadow-lg ring-2 ring-primary-100'
        : 'border-slate-200 hover:border-slate-300 hover:shadow-lg',
    ].join(' ')}>
      <Handle type="target" position={Position.Left}
        className="!w-3 !h-3 !bg-slate-300 !border-2 !border-white hover:!bg-primary-400 !transition-colors" />
      <Handle type="source" position={Position.Right}
        className="!w-3 !h-3 !bg-slate-300 !border-2 !border-white hover:!bg-primary-400 !transition-colors" />
      <Handle type="target" position={Position.Top} id="top"
        className="!w-3 !h-3 !bg-slate-300 !border-2 !border-white hover:!bg-primary-400 !transition-colors" />
      <Handle type="source" position={Position.Bottom} id="bottom"
        className="!w-3 !h-3 !bg-slate-300 !border-2 !border-white hover:!bg-primary-400 !transition-colors" />
      <div className="flex items-center gap-2 mb-1">
        <div className="w-8 h-8 rounded-lg bg-primary-50 flex items-center justify-center flex-shrink-0">
          <Puzzle className="w-4 h-4 text-primary-500" />
        </div>
        <span className="text-sm font-semibold text-slate-800 truncate max-w-[140px]">
          {data.label}
        </span>
      </div>
      {data.description && (
        <p className="text-[11px] text-slate-400 leading-relaxed line-clamp-2 pl-10">
          {data.description}
        </p>
      )}
    </div>
  )
}

// ============================================================================
// FP Node (功能点节点)
// ============================================================================
const FP_TYPE_STYLES: Record<string, { label: string; bg: string; text: string }> = {
  workflow: { label: '流程', bg: 'bg-blue-50', text: 'text-blue-600' },
  calculation: { label: '计算', bg: 'bg-green-50', text: 'text-green-600' },
  constraint: { label: '约束', bg: 'bg-orange-50', text: 'text-orange-600' },
  api_behavior: { label: 'API', bg: 'bg-purple-50', text: 'text-purple-600' },
  data_record: { label: '数据', bg: 'bg-cyan-50', text: 'text-cyan-600' },
  config: { label: '配置', bg: 'bg-slate-100', text: 'text-slate-600' },
}

function FPNode({ data, selected }: any) {
  const fpStyle = FP_TYPE_STYLES[data.fpType] || FP_TYPE_STYLES.workflow
  const isArchived = data.reqStatus === 'archived'
  // v2.3: 含未审核依赖时高亮显示（橙色边框 + 脉冲动画）
  const hasPendingReview = data.hasPendingReview === true
  return (
    <div className={[
      'relative px-4 py-3 rounded-2xl border-2 shadow-md min-w-[140px] transition-all duration-200 group',
      isArchived
        ? 'bg-slate-50 border-slate-300 opacity-80'
        : hasPendingReview
          ? 'bg-amber-50'
          : 'bg-white',
      selected
        ? 'border-indigo-400 shadow-indigo-100 shadow-lg ring-2 ring-indigo-100'
        : hasPendingReview
          ? 'border-amber-400 shadow-amber-100 shadow-lg animate-pulse'
          : isArchived
            ? 'border-slate-300'
            : 'border-slate-200 hover:border-slate-300 hover:shadow-lg',
    ].join(' ')}>
      {isArchived && (
        <span className="absolute -top-2 -right-2 text-[9px] px-1.5 py-0.5 rounded-full bg-slate-300 text-slate-500 font-medium">
          已归档
        </span>
      )}
      {hasPendingReview && (
        <span className="absolute -top-2 -right-2 text-[9px] px-1.5 py-0.5 rounded-full bg-amber-500 text-white font-medium flex items-center gap-0.5">
          <Clock className="w-2.5 h-2.5" /> 待审核
        </span>
      )}
      <Handle type="target" position={Position.Left}
        className="!w-3 !h-3 !bg-slate-300 !border-2 !border-white hover:!bg-indigo-400 !transition-colors" />
      <Handle type="source" position={Position.Right}
        className="!w-3 !h-3 !bg-slate-300 !border-2 !border-white hover:!bg-indigo-400 !transition-colors" />
      <Handle type="target" position={Position.Top} id="top"
        className="!w-3 !h-3 !bg-slate-300 !border-2 !border-white hover:!bg-indigo-400 !transition-colors" />
      <Handle type="source" position={Position.Bottom} id="bottom"
        className="!w-3 !h-3 !bg-slate-300 !border-2 !border-white hover:!bg-indigo-400 !transition-colors" />
      <div className="flex items-center gap-2 mb-1">
        <div className={[
          'w-8 h-8 rounded-lg flex items-center justify-center flex-shrink-0',
          isArchived ? 'bg-slate-200' : 'bg-indigo-50',
        ].join(' ')}>
          <Boxes className={['w-4 h-4', isArchived ? 'text-slate-400' : 'text-indigo-500'].join(' ')} />
        </div>
        <span className={[
          'text-sm font-semibold truncate max-w-[120px]',
          isArchived ? 'text-slate-400' : 'text-slate-800',
        ].join(' ')}>
          {data.label}
        </span>
      </div>
      <div className="flex items-center gap-1.5 pl-10">
        <span className={`text-[10px] px-1.5 py-0.5 rounded ${isArchived ? 'bg-slate-100 text-slate-400' : fpStyle.bg + ' ' + fpStyle.text} font-medium`}>
          {fpStyle.label}
        </span>
        {data.priority && (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-500 font-medium">
            {data.priority}
          </span>
        )}
      </div>
      {data.reqTitle && (
        <p className={[
          'text-[11px] leading-relaxed line-clamp-1 pl-10 mt-0.5',
          isArchived ? 'text-slate-300' : 'text-slate-400',
        ].join(' ')}>
          {data.reqTitle}
        </p>
      )}
    </div>
  )
}

const nodeTypes = { moduleNode: ModuleNode, fpNode: FPNode }

// ============================================================================
// Edge Styles (模块 / 功能点 分离)
// ============================================================================
const MODULE_EDGE_STYLES: Record<string, { label: string; stroke: string }> = {
  depends_on: { label: '依赖', stroke: '#818cf8' },
  interdependent: { label: '相互依赖', stroke: '#22d3ee' },
}

const FP_EDGE_STYLES: Record<string, { label: string; stroke: string }> = {
  depends_on: { label: '依赖', stroke: '#818cf8' },
  data_flows: { label: '数据流向', stroke: '#34d399' },
  invariants: { label: '共享约束', stroke: '#fbbf24' },
  lifecycle_scenarios: { label: '生命周期', stroke: '#f472b6' },
  conflicts_with: { label: '冲突', stroke: '#f87171' },
}

const EDGE_COMMON = { type: 'smoothstep' as const, animated: true, style: { strokeWidth: 2.5 } }

function createEdge(source: string, target: string, eType: string, srcName = source, tgtName = target): any {
  const s = MODULE_EDGE_STYLES[eType] || MODULE_EDGE_STYLES.depends_on
  const label = eType === 'interdependent'
    ? `${srcName} ↔ ${tgtName}`
    : `${srcName} 依赖 ${tgtName}`
  const edge: any = {
    id: `${source}-${target}-${eType}`,
    source, target, ...EDGE_COMMON,
    style: { ...EDGE_COMMON.style, stroke: s.stroke },
    markerEnd: { type: MarkerType.ArrowClosed, color: s.stroke, width: 18, height: 18 },
    label,
    data: { label, eType },
  }
  if (eType === 'interdependent') {
    edge.markerStart = { type: MarkerType.ArrowClosed, color: s.stroke, width: 18, height: 18 }
  }
  return edge
}

function createFPEdge(
  depId: number, source: string, target: string, relationType: string,
  srcName = source, tgtName = target, description = '', reviewStatus = 'pending',
): any {
  const s = FP_EDGE_STYLES[relationType] || FP_EDGE_STYLES.depends_on
  const rs = REVIEW_STATUS_STYLES[reviewStatus] || REVIEW_STATUS_STYLES.pending
  // v2.3: 审核状态优先决定颜色；approved 时使用 relation_type 原色（保留语义信息）
  const stroke = reviewStatus === 'approved' ? s.stroke : rs.stroke
  const isDashed = rs.dashed
  const label = `${srcName} ${s.label} ${tgtName}`
  const edge: any = {
    id: `e-${depId}`,
    source, target, ...EDGE_COMMON,
    style: {
      ...EDGE_COMMON.style,
      stroke,
      strokeDasharray: isDashed ? '6 4' : undefined,
      opacity: reviewStatus === 'rejected' ? 0.55 : 1,
    },
    markerEnd: { type: MarkerType.ArrowClosed, color: stroke, width: 18, height: 18 },
    label: reviewStatus === 'approved' ? label : `${label} [${rs.label}]`,
    labelStyle: { fill: stroke, fontSize: 11, fontWeight: reviewStatus === 'pending' ? 700 : 400 },
    labelBgStyle: { fill: '#fff', fillOpacity: 0.9 },
    data: { label, eType: relationType, description, depId, reviewStatus },
  }
  if (relationType === 'invariants' || relationType === 'lifecycle_scenarios') {
    edge.markerStart = { type: MarkerType.ArrowClosed, color: stroke, width: 18, height: 18 }
  }
  return edge
}

// ============================================================================
// Module Picker Modal (shared for add + swap)
// ============================================================================
function ModulePicker({
  modules, excludeIds, onPick, onClose
}: {
  modules: ModuleType[]
  excludeIds: string[]
  onPick: (mod: ModuleType) => void
  onClose: () => void
}) {
  const [search, setSearch] = useState('')
  const filtered = modules.filter(m =>
    !excludeIds.includes(m.id) &&
    m.name.toLowerCase().includes(search.toLowerCase())
  )

  return (
    <div className="fixed inset-0 z-[60] bg-black/30 backdrop-blur-sm flex items-center justify-center p-4"
      onClick={onClose}>
      <div className="bg-white rounded-2xl shadow-xl max-w-md w-full max-h-[70vh] flex flex-col"
        onClick={e => e.stopPropagation()}>
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-slate-100">
          <div className="flex items-center gap-2">
            <Puzzle className="w-5 h-5 text-primary-500" />
            <h2 className="text-base font-semibold text-slate-800">选择模块</h2>
          </div>
          <button onClick={onClose} className="p-1 rounded-lg hover:bg-slate-100">
            <X className="w-4 h-4 text-slate-400" />
          </button>
        </div>

        {/* Search */}
        <div className="px-5 py-3">
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
            <input
              value={search}
              onChange={e => setSearch(e.target.value)}
              className="w-full pl-9 pr-3 py-2 text-sm border border-slate-200 rounded-xl focus:outline-none focus:border-primary-400 focus:ring-1 focus:ring-primary-100"
              placeholder="搜索模块名称..."
              autoFocus
            />
          </div>
        </div>

        {/* Module List */}
        <div className="flex-1 overflow-y-auto px-3 pb-3">
          {filtered.length === 0 ? (
            <p className="text-sm text-slate-400 text-center py-8">
              {search ? '无匹配模块' : '所有模块已添加到图谱'}
            </p>
          ) : (
            <div className="space-y-1">
              {filtered.map(mod => (
                <button
                  key={mod.id}
                  onClick={() => onPick(mod)}
                  className="w-full text-left px-4 py-3 rounded-xl hover:bg-primary-50 border border-transparent hover:border-primary-200 transition-all group"
                >
                  <div className="flex items-center gap-3">
                    <div className="w-9 h-9 rounded-lg bg-slate-100 group-hover:bg-primary-100 flex items-center justify-center flex-shrink-0 transition-colors">
                      <Puzzle className="w-4 h-4 text-slate-400 group-hover:text-primary-500" />
                    </div>
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-medium text-slate-700 group-hover:text-primary-700">
                        {mod.name}
                      </p>
                      {mod.description && (
                        <p className="text-xs text-slate-400 truncate mt-0.5">{mod.description}</p>
                      )}
                    </div>
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

// ============================================================================
// v3.0 Impact Description Modal — FP 节点双击打开，自动生成 + 可编辑
// ============================================================================
function ImpactDescriptionModal({
  fpId, fpName, reqId, onClose,
}: {
  fpId: string
  fpName: string
  reqId: string
  onClose: () => void
}) {
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [data, setData] = useState<import('../types').FPImpactDescription | null>(null)
  const [description, setDescription] = useState('')
  const [testFocus, setTestFocus] = useState('')
  const [error, setError] = useState('')

  const generate = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const res = await reqApi.generateImpactDescription(reqId, fpId)
      setData(res)
      setDescription(res.description || '')
      setTestFocus(res.test_focus || '')
    } catch (e: any) {
      setError(e?.message || '生成失败')
    } finally {
      setLoading(false)
    }
  }, [reqId, fpId])

  const save = useCallback(async () => {
    setSaving(true)
    setError('')
    try {
      await reqApi.saveImpactDescription(reqId, fpId, { description, test_focus: testFocus })
      onClose()
    } catch (e: any) {
      setError(e?.message || '保存失败')
    } finally {
      setSaving(false)
    }
  }, [reqId, fpId, description, testFocus, onClose])

  return (
    <div className="fixed inset-0 z-[60] bg-black/30 backdrop-blur-sm flex items-center justify-center p-4"
      onClick={onClose}>
      <div className="bg-white rounded-2xl shadow-xl max-w-2xl w-full max-h-[85vh] flex flex-col"
        onClick={e => e.stopPropagation()}>
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-slate-100">
          <div className="flex items-center gap-2">
            <GitBranch className="w-5 h-5 text-indigo-500" />
            <h3 className="text-base font-semibold text-slate-800">影响功能说明</h3>
            <span className="text-xs px-2 py-0.5 rounded bg-indigo-50 text-indigo-600">{fpName}</span>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-600">
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
          {error && (
            <div className="px-3 py-2 rounded-lg bg-red-50 text-red-600 text-sm">{error}</div>
          )}

          {!data && !loading && (
            <div className="text-center py-8">
              <p className="text-sm text-slate-500 mb-4">
                点击下方按钮，基于功能点依赖图谱和模块依赖图谱自动生成影响说明
              </p>
              <button onClick={generate}
                className="px-4 py-2 bg-indigo-500 text-white rounded-lg hover:bg-indigo-600 text-sm font-medium">
                生成影响说明
              </button>
            </div>
          )}

          {loading && (
            <div className="flex items-center justify-center py-8">
              <Loader2 className="w-6 h-6 text-indigo-500 animate-spin" />
              <span className="ml-2 text-sm text-slate-500">正在分析依赖图谱...</span>
            </div>
          )}

          {data && (
            <>
              {/* 受影响 FP */}
              {data.affected_fps.length > 0 && (
                <div>
                  <h4 className="text-sm font-semibold text-slate-700 mb-2">
                    受影响的功能点（{data.affected_fps.length}）
                  </h4>
                  <div className="space-y-1.5">
                    {data.affected_fps.map((fp, i) => (
                      <div key={i} className="flex items-center gap-2 text-xs px-3 py-1.5 bg-slate-50 rounded">
                        <span className="font-medium text-slate-700">{fp.fp_name}</span>
                        <span className="px-1.5 py-0.5 rounded bg-indigo-100 text-indigo-600">
                          {fp.relation_cn || fp.relation_type}
                        </span>
                        {fp.req_title && (
                          <span className="text-slate-400 truncate">← {fp.req_title}</span>
                        )}
                        <span className="ml-auto text-slate-400">{fp.depth}跳</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* 受影响模块 */}
              {data.affected_modules.length > 0 && (
                <div>
                  <h4 className="text-sm font-semibold text-slate-700 mb-2">
                    受影响的模块（{data.affected_modules.length}）
                  </h4>
                  <div className="flex flex-wrap gap-1.5">
                    {data.affected_modules.map((m, i) => (
                      <span key={i} className="text-xs px-2 py-1 bg-cyan-50 text-cyan-700 rounded">
                        {m.module_name}
                        <span className="ml-1 text-cyan-400">({m.reason})</span>
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {/* v2.35: 移除受影响需求展示（不再分析受影响的需求，改为基于 FP 依赖图谱分析受影响功能点） */}

              {/* 可编辑的影响说明 */}
              <div>
                <label className="block text-sm font-semibold text-slate-700 mb-1.5">
                  影响说明 {data.is_auto_generated && <span className="text-xs text-slate-400 font-normal">（自动生成，可编辑）</span>}
                </label>
                <textarea
                  value={description}
                  onChange={e => setDescription(e.target.value)}
                  rows={5}
                  className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:border-indigo-400 focus:ring-1 focus:ring-indigo-100 resize-none"
                  placeholder="描述本次功能点变更的影响范围..."
                />
              </div>

              <div>
                <label className="block text-sm font-semibold text-slate-700 mb-1.5">
                  测试关注点
                </label>
                <textarea
                  value={testFocus}
                  onChange={e => setTestFocus(e.target.value)}
                  rows={3}
                  className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:border-indigo-400 focus:ring-1 focus:ring-indigo-100 resize-none"
                  placeholder="列出测试用例生成时应重点关注的回归场景..."
                />
              </div>
            </>
          )}
        </div>

        {/* Footer */}
        {data && (
          <div className="flex items-center justify-between px-5 py-3 border-t border-slate-100">
            <button onClick={generate} disabled={loading}
              className="text-xs text-slate-500 hover:text-indigo-600 disabled:opacity-50">
              {loading ? '重新生成中...' : '重新生成'}
            </button>
            <div className="flex gap-2">
              <button onClick={onClose}
                className="px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-100 rounded-lg">
                取消
              </button>
              <button onClick={save} disabled={saving || !description.trim()}
                className="px-4 py-1.5 text-sm bg-indigo-500 text-white rounded-lg hover:bg-indigo-600 disabled:opacity-50">
                {saving ? '保存中...' : '保存'}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

// ============================================================================
// v2.3 依赖关系变动历史面板
// ============================================================================
const ACTION_LABELS: Record<string, { label: string; bg: string; text: string }> = {
  created: { label: '新增', bg: 'bg-blue-50', text: 'text-blue-700' },
  approved: { label: '审核通过', bg: 'bg-emerald-50', text: 'text-emerald-700' },
  rejected: { label: '驳回', bg: 'bg-red-50', text: 'text-red-700' },
  modified: { label: '修改', bg: 'bg-amber-50', text: 'text-amber-700' },
  deleted: { label: '删除', bg: 'bg-slate-100', text: 'text-slate-700' },
}

function DependencyHistoryPanel({
  items, loading, onClose, onReviewJump,
}: {
  items: FPDependencyHistoryItem[]
  loading: boolean
  onClose: () => void
  onReviewJump: (depId: number | null) => void
}) {
  const [actionFilter, setActionFilter] = useState<string>('')
  const filtered = actionFilter ? items.filter(i => i.action === actionFilter) : items

  return (
    <div className="absolute top-0 right-0 bottom-0 w-96 bg-white border-l border-slate-200 shadow-xl flex flex-col z-30">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-slate-100">
        <div className="flex items-center gap-2">
          <History className="w-4 h-4 text-indigo-500" />
          <h3 className="text-sm font-semibold text-slate-800">依赖变动历史</h3>
          <span className="text-xs text-slate-400">({items.length})</span>
        </div>
        <button onClick={onClose} className="p-1 rounded-lg hover:bg-slate-100">
          <X className="w-4 h-4 text-slate-400" />
        </button>
      </div>

      {/* Filter */}
      <div className="px-4 py-2 border-b border-slate-100 flex items-center gap-1.5 flex-wrap">
        <span className="text-[11px] text-slate-500">操作类型:</span>
        <button onClick={() => setActionFilter('')}
          className={`text-[11px] px-2 py-0.5 rounded ${!actionFilter ? 'bg-slate-700 text-white' : 'bg-slate-100 text-slate-600 hover:bg-slate-200'}`}>
          全部
        </button>
        {Object.entries(ACTION_LABELS).map(([key, val]) => (
          <button key={key} onClick={() => setActionFilter(actionFilter === key ? '' : key)}
            className={`text-[11px] px-2 py-0.5 rounded ${actionFilter === key ? 'bg-slate-700 text-white' : `${val.bg} ${val.text} hover:opacity-80`}`}>
            {val.label}
          </button>
        ))}
      </div>

      {/* List */}
      <div className="flex-1 overflow-y-auto p-3 space-y-2">
        {loading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="w-5 h-5 text-indigo-500 animate-spin" />
          </div>
        ) : filtered.length === 0 ? (
          <p className="text-sm text-slate-400 text-center py-8">暂无历史记录</p>
        ) : (
          filtered.map(h => {
            const action = ACTION_LABELS[h.action] || { label: h.action, bg: 'bg-slate-100', text: 'text-slate-700' }
            return (
              <div key={h.id} className="p-2.5 bg-slate-50 rounded-lg border border-slate-100 text-xs">
                <div className="flex items-center gap-1.5 mb-1.5">
                  <span className={`px-1.5 py-0.5 rounded font-medium ${action.bg} ${action.text}`}>
                    {action.label}
                  </span>
                  <span className="text-slate-400">{new Date(h.created_at).toLocaleString('zh-CN')}</span>
                  {h.changed_by && <span className="text-slate-400 ml-auto">by {h.changed_by}</span>}
                </div>
                <div className="text-slate-700">
                  <span className="font-medium">{h.source_name}</span>
                  <span className="mx-1 text-slate-400">→</span>
                  <span className="font-medium">{h.target_name}</span>
                  <span className="ml-1.5 text-slate-400">[{h.relation_type}]</span>
                </div>
                {(h.old_review_status || h.new_review_status) && (
                  <div className="mt-1 text-[11px] text-slate-500">
                    状态变更：
                    <span className="text-slate-600">{h.old_review_status || '—'}</span>
                    <span className="mx-1">→</span>
                    <span className="text-slate-600">{h.new_review_status || '—'}</span>
                  </div>
                )}
                {h.change_reason && (
                  <p className="mt-1 text-[11px] text-slate-500 italic">原因：{h.change_reason}</p>
                )}
                {h.related_req_id && (
                  <p className="mt-1 text-[11px] text-slate-400">关联需求：{h.related_req_id}</p>
                )}
                {h.dependency_id && h.action !== 'deleted' && (
                  <button onClick={() => onReviewJump(h.dependency_id)}
                    className="mt-1.5 text-[11px] text-indigo-500 hover:text-indigo-700">
                    查看此依赖 →
                  </button>
                )}
              </div>
            )
          })
        )}
      </div>
    </div>
  )
}

// ============================================================================
// v2.3 审核依赖 Modal
// ============================================================================
function ReviewDependencyModal({
  depId, sourceName, targetName, currentStatus, reqId, onClose, onReviewed,
}: {
  depId: number
  sourceName: string
  targetName: string
  currentStatus: string
  reqId: string
  onClose: () => void
  onReviewed: () => void
}) {
  const [action, setAction] = useState<'approve' | 'reject'>('approve')
  const [reason, setReason] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  const submit = async () => {
    if (action === 'reject' && !reason.trim()) {
      setError('驳回时必须填写原因')
      return
    }
    setSubmitting(true)
    setError('')
    try {
      await reqApi.fpReviewDependency(reqId, depId, action, reason.trim())
      onReviewed()
      onClose()
    } catch (e: any) {
      setError(e?.message || '审核失败')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="fixed inset-0 z-[70] bg-black/30 backdrop-blur-sm flex items-center justify-center p-4"
      onClick={onClose}>
      <div className="bg-white rounded-2xl shadow-xl max-w-md w-full"
        onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between px-5 py-4 border-b border-slate-100">
          <div className="flex items-center gap-2">
            <CheckCircle className="w-5 h-5 text-indigo-500" />
            <h3 className="text-base font-semibold text-slate-800">审核依赖关系</h3>
          </div>
          <button onClick={onClose} className="p-1 rounded-lg hover:bg-slate-100">
            <X className="w-4 h-4 text-slate-400" />
          </button>
        </div>

        <div className="px-5 py-4 space-y-3">
          {error && <div className="px-3 py-2 rounded-lg bg-red-50 text-red-600 text-sm">{error}</div>}

          <div className="p-3 bg-slate-50 rounded-lg text-sm">
            <div className="text-slate-500 text-xs mb-1">依赖关系</div>
            <div className="text-slate-700">
              <span className="font-medium">{sourceName}</span>
              <span className="mx-1.5 text-slate-400">→</span>
              <span className="font-medium">{targetName}</span>
            </div>
            <div className="mt-1 text-[11px] text-slate-400">
              当前状态：<span className="font-medium">{REVIEW_STATUS_STYLES[currentStatus]?.label || currentStatus}</span>
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1.5">审核操作</label>
            <div className="flex gap-2">
              <button onClick={() => setAction('approve')}
                className={`flex-1 px-3 py-2 rounded-lg text-sm font-medium border-2 transition-colors ${
                  action === 'approve' ? 'border-emerald-500 bg-emerald-50 text-emerald-700' : 'border-slate-200 text-slate-600 hover:border-slate-300'
                }`}>
                <CheckCircle className="w-4 h-4 inline mr-1" /> 审核通过
              </button>
              <button onClick={() => setAction('reject')}
                className={`flex-1 px-3 py-2 rounded-lg text-sm font-medium border-2 transition-colors ${
                  action === 'reject' ? 'border-red-500 bg-red-50 text-red-700' : 'border-slate-200 text-slate-600 hover:border-slate-300'
                }`}>
                <XCircle className="w-4 h-4 inline mr-1" /> 驳回
              </button>
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1.5">
              审核原因 {action === 'reject' && <span className="text-red-500">*</span>}
            </label>
            <textarea
              value={reason}
              onChange={e => setReason(e.target.value)}
              rows={3}
              className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:border-indigo-400 focus:ring-1 focus:ring-indigo-100 resize-none"
              placeholder={action === 'reject' ? '请填写驳回原因（必填）' : '可填写审核说明（选填）'}
            />
          </div>
        </div>

        <div className="flex justify-end gap-2 px-5 py-3 border-t border-slate-100">
          <button onClick={onClose}
            className="px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-100 rounded-lg">
            取消
          </button>
          <button onClick={submit} disabled={submitting}
            className="px-4 py-1.5 text-sm bg-indigo-500 text-white rounded-lg hover:bg-indigo-600 disabled:opacity-50">
            {submitting ? '提交中...' : '确认'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ============================================================================
// Main Page
// ============================================================================
export default function DependencyGraph() {
  const { selectedProductId, products } = useProduct()
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([])
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([])
  const [loading, setLoading] = useState(false)
  const [fullscreen, setFullscreen] = useState(false)
  const rfInstance = useRef<any>(null)
  const toggleFullscreen = () => setFullscreen(!fullscreen)

  // 从 URL query 读取 module_id（支持从模块管理页面跳转过来后定位）
  const [searchParams, setSearchParams] = useSearchParams()
  const pendingFocusModuleId = useRef<string | null>(null)
  useEffect(() => {
    const mid = searchParams.get('module_id') || ''
    if (mid) {
      pendingFocusModuleId.current = mid
      setSearchParams({}, { replace: true })
    }
  }, [])

  /** 节点加载完成后，根据 pendingFocusModuleId 选中并居中该节点 */
  const focusPendingModule = useCallback(() => {
    const mid = pendingFocusModuleId.current
    if (!mid || !rfInstance.current) return
    const target = nodes.find(n => n.id === mid)
    if (target) {
      // 选中该节点（视觉高亮）
      setNodes(nds => nds.map(n => ({ ...n, selected: n.id === mid })))
      // 居中显示
      const { x, y } = target.position
      rfInstance.current.setCenter(x, y, { zoom: 1.4, duration: 600 })
      pendingFocusModuleId.current = null
    }
  }, [nodes, setNodes])

  const [allModules, setAllModules] = useState<ModuleType[]>([])
  const [edgeType, setEdgeType] = useState('depends_on')
  const [selectedEdge, setSelectedEdge] = useState<Edge | null>(null)
  const [ctxMenu, setCtxMenu] = useState<{ x: number; y: number; nodeId?: string; edgeId?: string } | null>(null)

  // v1.4: 图谱类型切换
  const [graphType, setGraphType] = useState<'modules' | 'fp'>('modules')

  // Module picker state
  const [pickerOpen, setPickerOpen] = useState(false)
  const [pickerExcludes, setPickerExcludes] = useState<string[]>([])
  const [pickerTarget, setPickerTarget] = useState<'new' | { nodeId: string }>('new')

  // v3.0: FP 影响说明 Modal state
  const [impactModal, setImpactModal] = useState<{ fpId: string; fpName: string; reqId: string } | null>(null)

  // v2.3: FP 图谱筛选与审核
  const [reviewStatusFilter, setReviewStatusFilter] = useState<string>('')  // '' / pending / approved / rejected
  const [moduleFilter, setModuleFilter] = useState<string>('')  // '' 或 module_id
  const [graphModules, setGraphModules] = useState<{ id: string; name: string }[]>([])
  const [historyOpen, setHistoryOpen] = useState(false)
  const [historyItems, setHistoryItems] = useState<FPDependencyHistoryItem[]>([])
  const [historyLoading, setHistoryLoading] = useState(false)
  const [reviewModal, setReviewModal] = useState<{
    depId: number; sourceName: string; targetName: string; currentStatus: string; reqId: string
  } | null>(null)

  // 当前模式对应的边样式表
  const currentEdgeStyles = graphType === 'fp' ? FP_EDGE_STYLES : MODULE_EDGE_STYLES

  // ========== Data Loading ==========
  useEffect(() => {
    if (selectedProductId) {
      prodApi.listModules(selectedProductId)
        .then(mods => setAllModules(mods || []))
        .catch(() => setAllModules([]))
      graphType === 'fp' ? loadFPGraph() : loadGraph()
    } else {
      setNodes([]); setEdges([]); setAllModules([]); setGraphModules([])
    }
  }, [selectedProductId, graphType])

  // v2.3: FP 模式下筛选条件变化时重新加载
  useEffect(() => {
    if (selectedProductId && graphType === 'fp') {
      loadFPGraph()
    }
  }, [reviewStatusFilter, moduleFilter])

  // 节点加载后尝试聚焦 pendingFocusModuleId
  useEffect(() => {
    if (pendingFocusModuleId.current && nodes.length > 0) {
      const t = setTimeout(focusPendingModule, 200)
      return () => clearTimeout(t)
    }
  }, [nodes, focusPendingModule])

  function autoLayout(index: number, total: number) {
    const cols = Math.ceil(Math.sqrt(total))
    return { x: 80 + (index % cols) * 240, y: 80 + Math.floor(index / cols) * 160 }
  }

  const loadGraph = async () => {
    if (!selectedProductId) return
    setLoading(true)
    try {
      const data = await dependencyGraph.get(selectedProductId)
      const graphNodes: Node[] = (data?.nodes || []).map((n: any, i: number) => ({
        id: n.id,
        type: 'moduleNode',
        position: n.position || autoLayout(i, (data?.nodes || []).length),
        data: { label: n.name, description: n.description || '' },
      }))
      // 构建 ID→名称 映射，用于生成可读的边标签
      const nameMap: Record<string, string> = {}
      graphNodes.forEach(n => { nameMap[n.id] = String(n.data.label || n.id) })
      const graphEdges: Edge[] = (data?.edges || []).map((e: any) =>
        createEdge(e.source, e.target, e.type || 'depends_on', nameMap[e.source], nameMap[e.target])
      )
      setNodes(graphNodes)
      setEdges(graphEdges)
    } catch { }
    setLoading(false)
  }

  // ========== v1.4 FP Graph（使用跨需求图谱端点，消除 N+1 + slice 截断） ==========
  const loadFPGraph = async () => {
    if (!selectedProductId) return
    setLoading(true)
    try {
      const data = await dependencyGraph.fpGraph(
        selectedProductId,
        moduleFilter || undefined,
        reviewStatusFilter || undefined,
      )
      // v2.3: 保存模块列表供筛选器使用
      if (data?.modules) setGraphModules(data.modules)

      // v2.3: 统计每个节点的 pending 依赖数，用于在节点上显示"待审核"标签
      const pendingDepNodeIds = new Set<string>()
      ;(data?.edges || []).forEach((e: any) => {
        if (e.review_status === 'pending') {
          pendingDepNodeIds.add(e.source)
          pendingDepNodeIds.add(e.target)
        }
      })

      const fpNodes: Node[] = (data?.nodes || []).map((n: any, i: number) => ({
        id: n.id,
        type: 'fpNode',
        position: autoLayout(i, (data?.nodes || []).length),
        data: {
          label: n.name,
          fpType: n.fp_type,
          reqId: n.req_id,           // 供 onConnect/deleteEdge 确定 POST/DELETE 路径参数
          reqTitle: n.req_title,
          reqStatus: n.req_status,   // 归档/active，前端据此切换节点样式
          priority: n.priority,
          moduleId: n.module_id,     // v2.3: 模块 ID
          modules: n.modules,        // v2.3: 多模块关联
          hasPendingReview: pendingDepNodeIds.has(n.id),  // v2.3: 是否含未审核依赖
        },
      }))
      const nameMap: Record<string, string> = {}
      fpNodes.forEach(n => { nameMap[n.id] = String(n.data.label) })
      const fpEdges: Edge[] = (data?.edges || []).map((e: any) =>
        createFPEdge(
          e.id, e.source, e.target, e.relation_type,
          nameMap[e.source], nameMap[e.target], e.description,
          e.review_status || 'pending',
        )
      )
      setNodes(fpNodes)
      setEdges(fpEdges)
    } catch { }
    setLoading(false)
  }

  // v2.3: 加载依赖变动历史（合并多个 reqId 的结果）
  const loadHistory = async () => {
    if (nodes.length === 0) {
      setHistoryItems([])
      return
    }
    // 收集图中所有 unique reqId
    const reqIds = new Set<string>()
    nodes.forEach((n: any) => {
      if (n.data?.reqId) reqIds.add(n.data.reqId as string)
    })
    if (reqIds.size === 0) {
      setHistoryItems([])
      return
    }
    setHistoryLoading(true)
    try {
      // 并行获取每个 reqId 的历史，合并后按时间倒序排序
      const allResults = await Promise.all(
        Array.from(reqIds).map(rid => reqApi.fpDependencyHistory(rid).catch(() => ({ items: [], total: 0 })))
      )
      const merged = allResults.flatMap(r => r.items || [])
      // 按创建时间倒序，并去重（同一 dependency_id 可能被多个 reqId 关联返回）
      const seen = new Set<number>()
      const deduped = merged
        .filter((h: FPDependencyHistoryItem) => {
          if (h.dependency_id && seen.has(h.dependency_id)) return false
          if (h.dependency_id) seen.add(h.dependency_id)
          return true
        })
        .sort((a: FPDependencyHistoryItem, b: FPDependencyHistoryItem) =>
          new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
        )
      setHistoryItems(deduped)
    } catch {
      setHistoryItems([])
    }
    setHistoryLoading(false)
  }

  // v2.3: 打开审核 Modal
  const openReviewModal = (edge: Edge) => {
    const depId = (edge.data as any)?.depId as number
    const reviewStatus = (edge.data as any)?.reviewStatus as string || 'pending'
    if (!depId) { alert('无法审核：缺少依赖 ID'); return }
    // 找到 source 节点对应的 reqId（依赖记录归属的需求）
    const sourceNode = nodes.find(n => n.id === edge.source)
    const reqId = sourceNode?.data?.reqId as string
    if (!reqId) { alert('无法审核：缺少需求 ID'); return }
    const srcName = String(sourceNode?.data?.label || edge.source)
    const tgtName = String(nodes.find(n => n.id === edge.target)?.data?.label || edge.target)
    setReviewModal({ depId, sourceName: srcName, targetName: tgtName, currentStatus: reviewStatus, reqId })
  }

  // v2.3: 审核完成后刷新图谱
  const onReviewed = () => {
    setReviewModal(null)
    loadFPGraph()
    if (historyOpen) loadHistory()
  }

  // ========== Edge Operations ==========
  const onConnect = useCallback(async (conn: Connection) => {
    if (!conn.source || !conn.target || conn.source === conn.target) return
    try {
      if (graphType === 'fp') {
        // FP 模式：需确定 source_fp 所属的 req_id
        const sourceNode = nodes.find(n => n.id === conn.source)
        const reqId = sourceNode?.data?.reqId as string
        if (!reqId) { alert('无法确定功能点所属需求'); return }
        await reqApi.fpAddDependency(reqId, conn.source, conn.target, edgeType)
        await loadFPGraph()
      } else {
        await dependencyGraph.addEdge(conn.source, conn.target, edgeType)
        await loadGraph()
      }
    } catch (e: any) {
      alert('添加依赖失败: ' + (e?.message || '未知错误'))
    }
  }, [edgeType, graphType, nodes])

  const deleteEdge = async (edgeId: string) => {
    const edge = edges.find(e => e.id === edgeId)
    if (!edge || !confirm('确认删除该连线？')) return
    try {
      if (graphType === 'fp') {
        const depId = (edge.data as any)?.depId || Number(String(edge.id).replace(/^e-/, ''))
        const sourceNode = nodes.find(n => n.id === edge.source)
        const reqId = sourceNode?.data?.reqId as string
        if (!reqId || !depId) { alert('无法删除：缺少依赖 ID'); return }
        await reqApi.fpRemoveDependency(reqId, depId)
        await loadFPGraph()
      } else {
        await dependencyGraph.removeEdge(String(edge.source), String(edge.target))
        await loadGraph()
      }
      setSelectedEdge(null)
    } catch (e: any) {
      alert('删除失败: ' + (e?.message || '未知错误'))
    }
  }

  // ========== Node Operations (graph only, never delete from DB) ==========
  /** Add a module node to the graph at next auto-layout position */
  const addNodeToGraph = (mod: ModuleType) => {
    if (nodes.some(n => n.id === mod.id)) return
    const pos = autoLayout(nodes.length, nodes.length + 1)
    setNodes(nds => [...nds, {
      id: mod.id, type: 'moduleNode', position: pos,
      data: { label: mod.name, description: mod.description || '' },
    }])
  }

  /** Remove a node from the graph only — module stays in DB */
  const removeNodeFromGraph = (nodeId: string) => {
    setNodes(nds => nds.filter(n => n.id !== nodeId))
    setEdges(eds => eds.filter(e => e.source !== nodeId && e.target !== nodeId))
  }

  /** Swap node to a different module */
  const swapNodeModule = (oldNodeId: string, newMod: ModuleType) => {
    setNodes(nds => nds.map(n => n.id === oldNodeId
      ? { ...n, id: newMod.id, data: { label: newMod.name, description: newMod.description || '' } }
      : n
    ))
    // Update edges referencing old node id
    setEdges(eds => eds.map(e => {
      if (e.source === oldNodeId && e.target === oldNodeId) return e
      if (e.source === oldNodeId) return { ...e, source: newMod.id, id: `${newMod.id}-${e.target}-${e.label}` }
      if (e.target === oldNodeId) return { ...e, target: newMod.id, id: `${e.source}-${newMod.id}-${e.label}` }
      return e
    }))
  }

  const onNodeDragStop = useCallback(async (_: any, node: Node) => {
    if (graphType === 'fp') return  // FP 不持久化位置
    try { await dependencyGraph.updateNodePosition(node.id, node.position.x, node.position.y) } catch { }
  }, [graphType])

  // Double-click: 模块模式打开 picker 交换节点；FP 模式打开影响说明 Modal
  const onNodeDoubleClick = useCallback((_: any, node: Node) => {
    if (graphType === 'fp') {
      // FP 模式：打开影响说明 Modal
      const reqId = (node.data as any)?.reqId as string
      const fpName = (node.data as any)?.label as string
      if (reqId && node.id) {
        setImpactModal({ fpId: node.id, fpName, reqId })
      }
      return
    }
    setPickerTarget({ nodeId: node.id })
    setPickerExcludes(nodes.filter(n => n.id !== node.id).map(n => n.id))
    setPickerOpen(true)
  }, [nodes, graphType])

  // ========== Context Menu ==========
  const onNodeContextMenu = useCallback((event: React.MouseEvent, node: Node) => {
    event.preventDefault()
    setCtxMenu({ x: event.clientX, y: event.clientY, nodeId: node.id })
  }, [])

  const onEdgeContextMenu = useCallback((event: React.MouseEvent, edge: Edge) => {
    event.preventDefault()
    setCtxMenu({ x: event.clientX, y: event.clientY, edgeId: edge.id })
  }, [])

  const onPaneClick = useCallback(() => {
    setCtxMenu(null)
    setSelectedEdge(null)
  }, [])

  useEffect(() => {
    const handler = () => setCtxMenu(null)
    document.addEventListener('click', handler)
    return () => document.removeEventListener('click', handler)
  }, [])

  const onEdgeClick = useCallback((_: any, edge: Edge) => {
    setSelectedEdge(edge)
    setCtxMenu(null)
  }, [])

  // ========== Keyboard ==========
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { setFullscreen(false); setPickerOpen(false) }
      if ((e.key === 'Delete' || e.key === 'Backspace') &&
        document.activeElement?.tagName !== 'INPUT' &&
        document.activeElement?.tagName !== 'TEXTAREA') {
        removeSelected()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [nodes, edges])

  const removeSelected = () => {
    const selNodeIds = nodes.filter(n => n.selected).map(n => n.id)
    const selEdgeIds = edges.filter(e => e.selected).map(e => e.id)
    if (selNodeIds.length === 0 && selEdgeIds.length === 0) return
    if (graphType === 'modules') {
      if (!confirm(`从图谱中移除 ${selNodeIds.length} 个节点、${selEdgeIds.length} 条连线？（模块不会被删除）`)) return
    } else {
      if (selEdgeIds.length === 0) return
      if (!confirm(`删除 ${selEdgeIds.length} 条依赖连线？`)) return
    }
    if (graphType === 'modules') {
      selNodeIds.forEach((id: string) => removeNodeFromGraph(id))
    }
    selEdgeIds.forEach((id: string) => deleteEdge(id))
  }

  // ========== Picker handler ==========
  const handlePickModule = (mod: ModuleType) => {
    if (pickerTarget === 'new') {
      addNodeToGraph(mod)
    } else {
      swapNodeModule(pickerTarget.nodeId, mod)
    }
    setPickerOpen(false)
  }

  const openAddPicker = () => {
    setPickerTarget('new')
    setPickerExcludes(nodes.map((n: any) => n.id))
    setPickerOpen(true)
  }

  // ========== Context menu actions ==========
  const contextEdit = () => {
    if (!ctxMenu?.nodeId) return
    const node = nodes.find((n: any) => n.id === ctxMenu.nodeId)
    if (!node) return
    setPickerTarget({ nodeId: node.id })
    setPickerExcludes(nodes.filter((n: any) => n.id !== node.id).map((n: any) => n.id))
    setPickerOpen(true)
    setCtxMenu(null)
  }

  const swapEdge = async () => {
    if (!ctxMenu?.edgeId) return
    const edge = edges.find((e: any) => e.id === ctxMenu.edgeId)
    if (!edge) return
    const srcName = String(nodes.find((n: any) => n.id === edge.source)?.data?.label || edge.source)
    const tgtName = String(nodes.find((n: any) => n.id === edge.target)?.data?.label || edge.target)
    if (!confirm(`将依赖方向从 "${srcName} → ${tgtName}" 改为 "${tgtName} → ${srcName}"？`)) return
    try {
      if (graphType === 'fp') {
        const depId = (edge.data as any)?.depId
        const sourceNode = nodes.find((n: any) => n.id === edge.source)
        const reqId = sourceNode?.data?.reqId as string
        const eType = (edge.data as any)?.eType || 'depends_on'
        if (!reqId || !depId) { alert('无法交换：缺少依赖 ID'); return }
        await reqApi.fpRemoveDependency(reqId, depId)
        // 创建反向依赖（target 的 reqId）
        const targetNode = nodes.find((n: any) => n.id === edge.target)
        const targetReqId = targetNode?.data?.reqId as string
        if (!targetReqId) { alert('无法交换：缺少目标功能点所属需求'); return }
        await reqApi.fpAddDependency(targetReqId, edge.target, edge.source, eType)
        await loadFPGraph()
      } else {
        await dependencyGraph.removeEdge(String(edge.source), String(edge.target))
        await dependencyGraph.addEdge(String(edge.target), String(edge.source), (edge.data as any)?.eType || 'depends_on')
        await loadGraph()
      }
    } catch (e: any) { alert('交换方向失败: ' + (e?.message || '未知错误')) }
  }

  const contextRemove = () => {
    if (ctxMenu?.nodeId) {
      if (graphType === 'fp') {
        setCtxMenu(null)
        return
      }
      if (!confirm('从图谱中移除该模块？（模块不会被删除）')) return
      removeNodeFromGraph(ctxMenu.nodeId)
    }
    if (ctxMenu?.edgeId) deleteEdge(ctxMenu.edgeId)
    setCtxMenu(null)
  }

  // ========== Helpers ==========
  const onGraphNodes = allModules.filter(m => !nodes.some((n: any) => n.id === m.id))
  const productName = selectedProductId
    ? products.find(p => p.id === selectedProductId)?.name || selectedProductId
    : null

  // 边类型选项按模式生成
  const edgeTypeOptions = graphType === 'fp'
    ? Object.entries(FP_EDGE_STYLES)
    : Object.entries(MODULE_EDGE_STYLES)

  // 切换图谱类型
  const switchGraphType = (type: 'modules' | 'fp') => {
    setGraphType(type)
    setEdgeType('depends_on')  // 切换后重置为默认类型
    // v2.3: 切换到 FP 时重置筛选条件，避免上次筛选残留
    if (type === 'fp') {
      setReviewStatusFilter('')
      setModuleFilter('')
    }
    setHistoryOpen(false)
  }

  // v2.3: 切换历史面板时按需加载
  const toggleHistory = () => {
    const next = !historyOpen
    setHistoryOpen(next)
    if (next && historyItems.length === 0) loadHistory()
  }

  // ==========================================================================
  // Render
  // ==========================================================================
  return (
    <div className={`flex flex-col ${fullscreen ? 'fixed inset-0 z-50 bg-white' : 'h-[calc(100vh-5rem)]'}`}>
      {/* Top Toolbar */}
      <div className="flex items-center gap-3 px-5 py-2.5 border-b border-slate-200 bg-white shrink-0">
        <GitBranch className="w-5 h-5 text-primary-500" />
        <div className="flex items-center gap-1 bg-slate-100 rounded-lg p-0.5">
          <button onClick={() => switchGraphType('modules')}
            className={`px-3 py-1 text-xs font-medium rounded-md transition-colors ${
              graphType === 'modules' ? 'bg-white text-slate-800 shadow-sm' : 'text-slate-500 hover:text-slate-700'
            }`}>
            模块依赖
          </button>
          <button onClick={() => switchGraphType('fp')}
            className={`px-3 py-1 text-xs font-medium rounded-md transition-colors ${
              graphType === 'fp' ? 'bg-white text-slate-800 shadow-sm' : 'text-slate-500 hover:text-slate-700'
            }`}>
            功能点依赖
          </button>
        </div>
        {productName && <span className="text-xs text-slate-400">· {productName}</span>}

        {/* v2.3: FP 图谱筛选器 — 模块 + 审核状态 */}
        {selectedProductId && graphType === 'fp' && (
          <>
            <div className="flex items-center gap-1.5">
              <Filter className="w-3.5 h-3.5 text-slate-400" />
              <select value={moduleFilter} onChange={e => setModuleFilter(e.target.value)}
                className="text-xs px-2 py-1 border border-slate-200 rounded-lg bg-white text-slate-700 focus:outline-none focus:border-primary-400 max-w-[140px]">
                <option value="">全部模块（全局总览）</option>
                {graphModules.map(m => (
                  <option key={m.id} value={m.id}>{m.name}</option>
                ))}
              </select>
              <select value={reviewStatusFilter} onChange={e => setReviewStatusFilter(e.target.value)}
                className="text-xs px-2 py-1 border border-slate-200 rounded-lg bg-white text-slate-700 focus:outline-none focus:border-primary-400">
                <option value="">全部状态</option>
                <option value="pending">仅待审核</option>
                <option value="approved">仅已审核</option>
                <option value="rejected">仅已驳回</option>
              </select>
            </div>
            <div className="w-px h-5 bg-slate-200" />
            <button onClick={toggleHistory}
              className={`btn-ghost text-xs flex items-center gap-1 ${historyOpen ? 'bg-indigo-50 text-indigo-700' : ''}`}>
              <History className="w-3.5 h-3.5" /> 变动历史
            </button>
            <div className="w-px h-5 bg-slate-200" />
          </>
        )}

        <div className="flex-1" />

        {/* Draggable toolbar items — 仅模块模式显示 */}
        {selectedProductId && graphType === 'modules' && (
          <>
            <div className="flex items-center gap-1.5 px-2">
              {/* Module drag item */}
              <div
                draggable
                onDragStart={(e) => { e.dataTransfer.setData('application/reactflow', 'module'); e.dataTransfer.effectAllowed = 'move' }}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border text-xs font-medium cursor-grab active:cursor-grabbing select-none bg-primary-50 border-primary-200 text-primary-600 hover:bg-primary-100 transition-colors"
                title="拖拽到画布添加模块"
              >
                <GripVertical className="w-3 h-3 opacity-40" />
                <Puzzle className="w-3.5 h-3.5" />
                <span>模块</span>
              </div>
              {/* Connection drag item */}
              <div
                draggable
                onDragStart={(e) => { e.dataTransfer.setData('application/reactflow', 'connection'); e.dataTransfer.effectAllowed = 'move' }}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border text-xs font-medium cursor-grab active:cursor-grabbing select-none bg-cyan-50 border-cyan-200 text-cyan-600 hover:bg-cyan-100 transition-colors"
                title="拖拽到画布后，从节点拖出连线"
              >
                <GripVertical className="w-3 h-3 opacity-40" />
                <Link2 className="w-3.5 h-3.5" />
                <span>连线</span>
              </div>
            </div>
            <div className="w-px h-5 bg-slate-200" />
          </>
        )}

        {/* Edge type selector — 按模式显示不同选项 */}
        {selectedProductId && (
          <>
            <span className="text-[11px] text-slate-400">连线类型:</span>
            {edgeTypeOptions.map(([key, val]) => (
              <button key={key} onClick={() => setEdgeType(key)}
                className={`px-2.5 py-1 rounded-lg text-xs font-medium border transition-colors ${edgeType === key ? 'border-slate-300 text-slate-700' : 'border-slate-200 text-slate-500 hover:border-slate-300'}`}
                style={edgeType === key ? { background: `${val.stroke}1a`, borderColor: val.stroke, color: val.stroke } : {}}>
                {graphType === 'fp' && (key === 'invariants' || key === 'lifecycle_scenarios') ? `↔ ${val.label}` : `→ ${val.label}`}
              </button>
            ))}
          </>
        )}

        {/* v2.3: 审核状态图例 — FP 模式下显示 */}
        {selectedProductId && graphType === 'fp' && (
          <div className="flex items-center gap-2 ml-2">
            {Object.entries(REVIEW_STATUS_STYLES).map(([key, val]) => (
              <div key={key} className="flex items-center gap-1 text-[10px] text-slate-500">
                <div className={`w-4 h-0.5 ${val.dashed ? 'border-t-2 border-dashed' : ''}`}
                  style={{ borderColor: val.stroke, background: val.dashed ? 'transparent' : val.stroke }} />
                <span>{val.label}</span>
              </div>
            ))}
          </div>
        )}

        <div className="w-px h-5 bg-slate-200" />
        <button onClick={() => graphType === 'fp' ? loadFPGraph() : loadGraph()} disabled={loading} className="btn-ghost text-xs">
          <Loader2 className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} /> 刷新
        </button>
        <button onClick={toggleFullscreen} className="btn-ghost text-xs" title={fullscreen ? '退出全屏 (Esc)' : '全屏'}>
          {fullscreen ? <Minimize className="w-3.5 h-3.5" /> : <Maximize className="w-3.5 h-3.5" />}
        </button>
      </div>

      {/* Main Content */}
      <div className="flex flex-1 min-h-0">
        {/* Left Sidebar — Legend & Stats only */}
        <div className="w-48 border-r border-slate-200 bg-slate-50/50 flex flex-col shrink-0 p-3">
          {!selectedProductId ? (
            <p className="text-xs text-slate-400 text-center py-8">请先在顶部栏选择产品</p>
          ) : (
            <>
              <p className="text-[10px] text-slate-400 uppercase tracking-wider font-medium mb-3">图谱信息</p>
              <div className="space-y-2 text-xs text-slate-600">
                <div className="flex justify-between">
                  <span>{graphType === 'fp' ? '功能点节点' : '模块节点'}</span>
                  <span className="font-medium">{nodes.length}</span>
                </div>
                <div className="flex justify-between">
                  <span>依赖连线</span>
                  <span className="font-medium">{edges.length}</span>
                </div>
                {graphType === 'modules' && (
                  <div className="flex justify-between">
                    <span>可用模块</span>
                    <span className="font-medium">{onGraphNodes.length}</span>
                  </div>
                )}
              </div>

              <div className="mt-4 pt-3 border-t border-slate-200">
                <p className="text-[10px] text-slate-400 uppercase tracking-wider font-medium mb-2">图例</p>
                <div className="space-y-1.5">
                  {Object.entries(currentEdgeStyles).map(([key, val]) => (
                    <div key={key} className="flex items-center gap-2 text-[11px]">
                      <div className="w-5 h-0.5 rounded-full" style={{ background: val.stroke }} />
                      <span className="text-slate-600">{val.label}</span>
                    </div>
                  ))}
                </div>
              </div>

              <div className="mt-auto pt-3 border-t border-slate-200">
                <p className="text-[10px] text-slate-400 leading-relaxed">
                  {graphType === 'fp'
                    ? '双击节点生成影响说明 · 拖拽节点手柄连线 · 右键删除/交换方向 · 位置不持久化'
                    : '从工具栏拖拽模块到画布 · 拖拽节点手柄连线 · 双击节点切换模块'}
                </p>
              </div>
            </>
          )}
        </div>

        {/* Graph Canvas */}
        <div className="flex-1 relative bg-slate-50"
          onDragOver={(e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'move' }}
          onDrop={(e) => {
            if (graphType === 'fp') return  // FP 模式不支持拖放添加
            e.preventDefault()
            const itemType = e.dataTransfer.getData('application/reactflow')
            if (itemType === 'module') openAddPicker()
          }}
        >
          {loading ? (
            <div className="absolute inset-0 flex items-center justify-center bg-white/60 z-10">
              <Loader2 className="w-8 h-8 animate-spin text-primary-400" />
            </div>
          ) : !selectedProductId ? (
            <div className="absolute inset-0 flex flex-col items-center justify-center text-slate-400">
              <GitBranch className="w-12 h-12 mb-3 opacity-30" />
              <p className="text-sm font-medium">请先在顶部栏选择产品</p>
            </div>
          ) : (
            <ReactFlowProvider>
              <ReactFlow
                nodes={nodes} edges={edges}
                onNodesChange={onNodesChange} onEdgesChange={onEdgesChange}
                onConnect={onConnect}
                onInit={(inst: any) => { rfInstance.current = inst }}
                onNodeDragStop={onNodeDragStop}
                onNodeDoubleClick={onNodeDoubleClick}
                onNodeContextMenu={onNodeContextMenu}
                onEdgeContextMenu={onEdgeContextMenu}
                onEdgeClick={onEdgeClick}
                onPaneClick={onPaneClick}
                nodeTypes={nodeTypes}
                fitView fitViewOptions={{ padding: 0.3 }}
                selectionMode={SelectionMode.Partial} selectionOnDrag
                panOnDrag={[1, 2]} deleteKeyCode={null} multiSelectionKeyCode="Shift"
                connectionLineStyle={{ stroke: currentEdgeStyles[edgeType]?.stroke || '#818cf8', strokeWidth: 2 }}
                defaultEdgeOptions={{ type: 'smoothstep', animated: true }}
              >
                <Background color="#e2e8f0" gap={20} size={1} />
                <Controls showInteractive={false} className="!rounded-xl !shadow-md !border-slate-200" />
                <MiniMap nodeStrokeColor="#818cf8" nodeColor="#eef2ff" nodeBorderRadius={12}
                  maskColor="rgba(0,0,0,0.05)" style={{ bottom: 12, right: 12 }}
                  className="!rounded-xl !shadow-md !border-slate-200" />

                {nodes.length === 0 && (
                  <Panel position="top-center">
                    <div className="bg-white/90 backdrop-blur px-4 py-3 rounded-2xl shadow-lg border border-slate-200 text-center">
                      <p className="text-sm font-medium text-slate-600 mb-1">画布为空</p>
                      <p className="text-xs text-slate-400">
                        {graphType === 'fp'
                          ? '当前产品下暂无功能点，请先在需求详情中拆分功能点'
                          : '从工具栏拖拽「模块」到画布添加，拖拽节点间的手柄创建连线'}
                      </p>
                    </div>
                  </Panel>
                )}

                {selectedEdge && (
                  <Panel position="bottom-center">
                    <div className="bg-white/95 backdrop-blur px-4 py-2.5 rounded-2xl shadow-lg border border-slate-200 flex items-center gap-3">
                      <div className="flex items-center gap-1.5 text-xs">
                        <span className="w-2 h-2 rounded-full"
                          style={{ background: currentEdgeStyles[(selectedEdge.data as any)?.eType]?.stroke || '#818cf8' }} />
                        <span className="text-slate-600 font-medium">{selectedEdge.label}</span>
                      </div>
                      {/* v2.3: 审核状态徽章 */}
                      {graphType === 'fp' && (selectedEdge.data as any)?.reviewStatus && (
                        <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${REVIEW_STATUS_STYLES[(selectedEdge.data as any).reviewStatus]?.bg} ${REVIEW_STATUS_STYLES[(selectedEdge.data as any).reviewStatus]?.text}`}>
                          {REVIEW_STATUS_STYLES[(selectedEdge.data as any).reviewStatus]?.label}
                        </span>
                      )}
                      <div className="w-px h-4 bg-slate-200" />
                      {/* v2.3: 审核按钮 — 仅 pending 状态显示 */}
                      {graphType === 'fp' && (selectedEdge.data as any)?.reviewStatus === 'pending' && (
                        <button onClick={() => openReviewModal(selectedEdge)}
                          className="flex items-center gap-1 text-xs text-indigo-600 hover:text-indigo-800 transition-colors">
                          <CheckCircle className="w-3 h-3" /> 审核
                        </button>
                      )}
                      <button onClick={() => deleteEdge(selectedEdge.id)}
                        className="flex items-center gap-1 text-xs text-red-500 hover:text-red-700 transition-colors">
                        <Unlink className="w-3 h-3" /> 删除连线
                      </button>
                    </div>
                  </Panel>
                )}

                <Panel position="bottom-right">
                  <p className="text-[10px] text-slate-300">
                    {graphType === 'fp' ? '右键删除/交换方向' : 'Shift+框选 · Del 移除 · 右键菜单'}
                  </p>
                </Panel>
              </ReactFlow>
            </ReactFlowProvider>
          )}
        </div>
      </div>

      {/* Context Menu */}
      {ctxMenu && (
        <div className="fixed z-[100] bg-white rounded-xl shadow-xl border border-slate-200 py-1.5 min-w-[150px]"
          style={{ left: ctxMenu.x, top: ctxMenu.y }}>
          {ctxMenu.nodeId && graphType === 'modules' && (
            <>
              <button onClick={contextEdit}
                className="w-full flex items-center gap-2 px-3 py-2 text-sm text-slate-700 hover:bg-slate-50">
                <Edit3 className="w-3.5 h-3.5" /> 切换模块
              </button>
              <div className="border-t border-slate-100 my-1" />
              <button onClick={contextRemove}
                className="w-full flex items-center gap-2 px-3 py-2 text-sm text-red-600 hover:bg-red-50">
                <Trash2 className="w-3.5 h-3.5" /> 从图谱移除
              </button>
            </>
          )}
          {ctxMenu.edgeId && (<>
            {/* v2.3: FP 模式下 pending 依赖可审核 */}
            {graphType === 'fp' && (() => {
              const edge = edges.find(e => e.id === ctxMenu.edgeId)
              const rs = (edge?.data as any)?.reviewStatus
              if (rs === 'pending') {
                return (
                  <>
                    <button onClick={() => {
                      if (edge) openReviewModal(edge)
                      setCtxMenu(null)
                    }}
                      className="w-full flex items-center gap-2 px-3 py-2 text-sm text-indigo-600 hover:bg-indigo-50">
                      <CheckCircle className="w-3.5 h-3.5" /> 审核依赖
                    </button>
                    <div className="border-t border-slate-100 my-1" />
                  </>
                )
              }
              return null
            })()}
            <button onClick={swapEdge}
              className="w-full flex items-center gap-2 px-3 py-2 text-sm text-primary-600 hover:bg-primary-50">
              <ArrowLeftRight className="w-3.5 h-3.5" /> 交换方向
            </button>
            <div className="border-t border-slate-100 my-1" />
            <button onClick={contextRemove}
              className="w-full flex items-center gap-2 px-3 py-2 text-sm text-red-600 hover:bg-red-50">
              <Unlink className="w-3.5 h-3.5" /> 删除连线
            </button>
          </>)}
        </div>
      )}

      {/* Module Picker Modal — 仅模块模式使用 */}
      {pickerOpen && (
        <ModulePicker
          modules={allModules}
          excludeIds={pickerExcludes}
          onPick={handlePickModule}
          onClose={() => setPickerOpen(false)}
        />
      )}

      {impactModal && (
        <ImpactDescriptionModal
          fpId={impactModal.fpId}
          fpName={impactModal.fpName}
          reqId={impactModal.reqId}
          onClose={() => setImpactModal(null)}
        />
      )}

      {/* v2.3: 依赖变动历史面板 — 仅 FP 模式且开启时显示 */}
      {historyOpen && graphType === 'fp' && (
        <DependencyHistoryPanel
          items={historyItems}
          loading={historyLoading}
          onClose={() => setHistoryOpen(false)}
          onReviewJump={(depId) => {
            // 在图中找到对应的 edge 并选中
            if (depId) {
              const edge = edges.find(e => (e.data as any)?.depId === depId)
              if (edge) {
                setSelectedEdge(edge)
                setNodes(nds => nds.map(n => ({ ...n, selected: n.id === edge.source || n.id === edge.target })))
                setEdges(eds => eds.map(e => ({ ...e, selected: e.id === edge.id })))
              }
            }
          }}
        />
      )}

      {/* v2.3: 审核依赖 Modal */}
      {reviewModal && (
        <ReviewDependencyModal
          depId={reviewModal.depId}
          sourceName={reviewModal.sourceName}
          targetName={reviewModal.targetName}
          currentStatus={reviewModal.currentStatus}
          reqId={reviewModal.reqId}
          onClose={() => setReviewModal(null)}
          onReviewed={onReviewed}
        />
      )}
    </div>
  )
}
