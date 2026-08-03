import { useState, useEffect, useCallback, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  ReactFlow, useNodesState, useEdgesState, Background, Controls, MiniMap,
  MarkerType, ReactFlowProvider, Panel, Handle, Position, ConnectionMode,
  BaseEdge, getSmoothStepPath, EdgeLabelRenderer, useReactFlow,
  type Node, type Edge, type EdgeProps, type Connection, SelectionMode,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import {
  Search, Trash2, Edit3, GitBranch, Puzzle, Link2, GripVertical,
  Maximize, Minimize, Loader2, Unlink, X, ArrowLeftRight, Boxes,
  History, CheckCircle, XCircle, Clock, Filter,
} from 'lucide-react'
import { dependencyGraph, products as prodApi, requirements as reqApi } from '../api/client'
import { useProduct } from '../store/ProductContext'
import type {
  Module as ModuleType, FPDependencyHistoryItem,
  DependencyVersion, DependencyVersionDetail, VersionDiff,
} from '../types'
import { getEdgeParams, layoutHierarchical, type LayoutDirection } from '../utils/graphLayout'

// ============================================================================
// v2.3 审核状态样式（FP 依赖图谱）
// ============================================================================
const REVIEW_STATUS_STYLES: Record<string, { label: string; stroke: string; dashed: boolean; bg: string; text: string }> = {
  pending: { label: '待审核', stroke: '#f59e0b', dashed: true, bg: 'bg-amber-50', text: 'text-amber-700' },
  approved: { label: '已审核', stroke: '#10b981', dashed: false, bg: 'bg-emerald-50', text: 'text-emerald-700' },
  rejected: { label: '已驳回', stroke: '#ef4444', dashed: true, bg: 'bg-red-50', text: 'text-red-700' },
}

// v2.45 FP 自身状态样式（pending_review/approved/archived）
const FP_STATUS_STYLES: Record<string, { label: string; bg: string; text: string }> = {
  pending_review: { label: '待审核', bg: 'bg-amber-50', text: 'text-amber-700' },
  approved: { label: '已审核', bg: 'bg-emerald-50', text: 'text-emerald-700' },
  archived: { label: '已归档', bg: 'bg-slate-100', text: 'text-slate-500' },
}

// ============================================================================
// Module Node
// ============================================================================
function ModuleNode({ data, selected }: any) {
  return (
    <div className={[
      'relative px-4 py-3 border-2 min-w-[140px] transition-all duration-200 group',
      selected
        ? 'shadow-lg ring-2'
        : 'hover:shadow-lg',
    ].join(' ')} style={{
      background: 'var(--background)',
      borderRadius: 'calc(var(--radius) * 1.1)',
      boxShadow: selected ? 'var(--shadow-apple-md)' : 'var(--shadow-apple-sm)',
      borderColor: selected ? 'var(--brand-400)' : 'var(--border)',
      ...(selected ? { '--tw-ring-color': 'rgba(0, 122, 255, 0.1)' } as React.CSSProperties : {}),
    }}>
      <Handle type="source" position={Position.Left} id="left"
        className="!w-3 !h-3 !bg-[var(--input)] !border-2 !border-[var(--background)] hover:!bg-[var(--brand-400)] !transition-colors !opacity-0 group-hover:!opacity-100" />
      <Handle type="source" position={Position.Right} id="right"
        className="!w-3 !h-3 !bg-[var(--input)] !border-2 !border-[var(--background)] hover:!bg-[var(--brand-400)] !transition-colors !opacity-0 group-hover:!opacity-100" />
      <Handle type="source" position={Position.Top} id="top"
        className="!w-3 !h-3 !bg-[var(--input)] !border-2 !border-[var(--background)] hover:!bg-[var(--brand-400)] !transition-colors !opacity-0 group-hover:!opacity-100" />
      <Handle type="source" position={Position.Bottom} id="bottom"
        className="!w-3 !h-3 !bg-[var(--input)] !border-2 !border-[var(--background)] hover:!bg-[var(--brand-400)] !transition-colors !opacity-0 group-hover:!opacity-100" />
      <div className="flex items-center gap-2 mb-1">
        <div className="w-8 h-8 flex items-center justify-center flex-shrink-0" style={{ borderRadius: 'calc(var(--radius) * 0.5)', background: 'rgba(0, 122, 255, 0.08)' }}>
          <Puzzle className="w-4 h-4" style={{ color: 'var(--brand-500)' }} />
        </div>
        <span className="text-sm font-semibold truncate max-w-[140px]" style={{ color: 'var(--foreground)' }}>
          {data.label}
        </span>
      </div>
      {data.description && (
        <p className="text-[11px] leading-relaxed line-clamp-2 pl-10" style={{ color: 'var(--foreground-tertiary)' }}>
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

// v2.44: 需求状态标签样式（功能点依赖图谱节点状态显示）
// active 为默认活跃状态不显示标签；archived 已有"已归档"角标，不再重复显示
const REQ_STATUS_STYLES: Record<string, { label: string; bg: string; text: string }> = {
  analyzing: { label: '分析中', bg: 'bg-blue-50', text: 'text-blue-600' },
  draft: { label: '草稿', bg: 'bg-slate-100', text: 'text-slate-500' },
  pending_review: { label: '待审核', bg: 'bg-amber-50', text: 'text-amber-700' },
  confirmed: { label: '已确认', bg: 'bg-emerald-50', text: 'text-emerald-700' },
  superseded: { label: '已替代', bg: 'bg-slate-100', text: 'text-slate-500' },
  deprecated: { label: '已弃用', bg: 'bg-red-50', text: 'text-red-600' },
}

function FPNode({ data, selected }: any) {
  const fpStyle = FP_TYPE_STYLES[data.fpType] || FP_TYPE_STYLES.workflow
  const isArchived = data.reqStatus === 'archived' || data.fpStatus === 'archived'
  // v2.3: 含未审核依赖时高亮显示（橙色边框 + 脉冲动画）
  const hasPendingReview = data.hasPendingReview === true
  // v2.44: 需求状态标签（active/archived 不显示）
  const statusStyle = REQ_STATUS_STYLES[data.reqStatus]
  // v2.44: 所属模块名列表
  const moduleNames: string[] = Array.isArray(data.moduleNames) ? data.moduleNames : []
  return (
    <div className={[
      'relative px-4 py-3 border-2 min-w-[140px] transition-all duration-200 group',
      selected
        ? 'shadow-lg ring-2'
        : hasPendingReview
          ? 'shadow-lg animate-pulse'
          : 'hover:shadow-lg',
    ].join(' ')} style={{
      borderRadius: 'calc(var(--radius) * 1.1)',
      boxShadow: selected || hasPendingReview ? 'var(--shadow-apple-md)' : 'var(--shadow-apple-sm)',
      background: isArchived ? 'var(--background-muted)' : hasPendingReview ? 'var(--state-warning-surface)' : 'var(--background)',
      borderColor: selected ? 'var(--brand-400)' : hasPendingReview ? 'var(--state-warning)' : isArchived ? 'var(--input)' : 'var(--border)',
      opacity: isArchived ? 0.8 : 1,
      ...(selected ? { '--tw-ring-color': 'rgba(0, 122, 255, 0.1)' } as React.CSSProperties : {}),
    }}>
      {isArchived && (
        <span className="absolute -top-2 -right-2 text-[9px] px-1.5 py-0.5 rounded-full font-medium" style={{ background: 'var(--input)', color: 'var(--foreground-tertiary)' }}>
          已归档
        </span>
      )}
      {hasPendingReview && (
        <span className="absolute -top-2 -right-2 text-[9px] px-1.5 py-0.5 rounded-full text-white font-medium flex items-center gap-0.5" style={{ background: 'var(--state-warning)' }}>
          <Clock className="w-2.5 h-2.5" /> 待审核
        </span>
      )}
      <Handle type="source" position={Position.Left} id="left"
        className="!w-3 !h-3 !bg-[var(--input)] !border-2 !border-[var(--background)] hover:!bg-[var(--brand-400)] !transition-colors !opacity-0 group-hover:!opacity-100" />
      <Handle type="source" position={Position.Right} id="right"
        className="!w-3 !h-3 !bg-[var(--input)] !border-2 !border-[var(--background)] hover:!bg-[var(--brand-400)] !transition-colors !opacity-0 group-hover:!opacity-100" />
      <Handle type="source" position={Position.Top} id="top"
        className="!w-3 !h-3 !bg-[var(--input)] !border-2 !border-[var(--background)] hover:!bg-[var(--brand-400)] !transition-colors !opacity-0 group-hover:!opacity-100" />
      <Handle type="source" position={Position.Bottom} id="bottom"
        className="!w-3 !h-3 !bg-[var(--input)] !border-2 !border-[var(--background)] hover:!bg-[var(--brand-400)] !transition-colors !opacity-0 group-hover:!opacity-100" />
      <div className="flex items-center gap-2 mb-1">
        <div className="w-8 h-8 flex items-center justify-center flex-shrink-0" style={{ borderRadius: 'calc(var(--radius) * 0.5)', background: isArchived ? 'var(--background-muted)' : 'rgba(0, 122, 255, 0.08)' }}>
          <Boxes className="w-4 h-4" style={{ color: isArchived ? 'var(--foreground-tertiary)' : 'var(--brand-500)' }} />
        </div>
        <span className="text-sm font-semibold truncate max-w-[120px]" style={{ color: isArchived ? 'var(--foreground-tertiary)' : 'var(--foreground)' }}>
          {data.label}
        </span>
      </div>
      <div className="flex items-center gap-1.5 pl-10 flex-wrap">
        <span className={`text-[10px] px-1.5 py-0.5 rounded ${isArchived ? '' : fpStyle.bg + ' ' + fpStyle.text} font-medium`} style={isArchived ? { background: 'var(--background-muted)', color: 'var(--foreground-tertiary)' } : undefined}>
          {fpStyle.label}
        </span>
        {data.priority && (
          <span className="text-[10px] px-1.5 py-0.5 rounded font-medium" style={{ background: 'var(--background-muted)', color: 'var(--foreground-tertiary)' }}>
            {data.priority}
          </span>
        )}
        {statusStyle && !isArchived && (
          <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${statusStyle.bg} ${statusStyle.text}`}>
            {statusStyle.label}
          </span>
        )}
      </div>
      {/* v2.44: 所属模块名显示 */}
      {moduleNames.length > 0 && (
        <div className="flex items-center gap-1 pl-10 mt-0.5">
          <Puzzle className="w-2.5 h-2.5 flex-shrink-0" style={{ color: 'var(--foreground-quaternary)' }} />
          <span className="text-[10px] truncate" style={{ color: isArchived ? 'var(--foreground-quaternary)' : 'var(--foreground-tertiary)' }}>
            {moduleNames.join('、')}
          </span>
        </div>
      )}
      {/* v2.45 修复：功能点节点不显示需求名，避免节点信息冗余；需求归属信息仅在详情抽屉中展示 */}
    </div>
  )
}

const nodeTypes = { moduleNode: ModuleNode, fpNode: FPNode }

// ============================================================================
// FloatingEdge — 浮动边组件（v2.43）
// 根据两节点的相对位置自动计算最佳起止点和方向，不依赖固定 Handle 角色。
// 同时支持点击选中、选中高亮、自定义标签渲染、扩大命中区域。
// ============================================================================
function FloatingEdge({
  id, source, target, sourceX, sourceY, targetX, targetY,
  sourcePosition, targetPosition, markerEnd, markerStart, style,
  selected, label, labelStyle, labelBgStyle,
}: EdgeProps): React.ReactElement {
  // 通过 useReactFlow hook 获取节点（在 ReactFlowProvider 内可用）
  const { getNode } = useReactFlow()
  const sourceNode = getNode(source)
  const targetNode = getNode(target)

  // 优先使用浮动参数；若节点未就绪（首次渲染），降级用 EdgeProps 传入的固定坐标
  let edgeParams: ReturnType<typeof getEdgeParams> | null = null
  if (sourceNode && targetNode) {
    edgeParams = getEdgeParams(sourceNode as Node, targetNode as Node)
  }

  const [edgePath, labelX, labelY] = getSmoothStepPath({
    sourceX: edgeParams?.sx ?? sourceX,
    sourceY: edgeParams?.sy ?? sourceY,
    sourcePosition: edgeParams?.sourcePosition ?? sourcePosition,
    targetX: edgeParams?.tx ?? targetX,
    targetY: edgeParams?.ty ?? targetY,
    targetPosition: edgeParams?.targetPosition ?? targetPosition,
    borderRadius: 12,
  })

  // 选中时高亮：提升 strokeWidth + drop-shadow
  const mergedStyle: React.CSSProperties = {
    ...(style || {}),
    strokeWidth: selected ? 4 : (style?.strokeWidth ?? 2.5),
    filter: selected ? 'drop-shadow(0 1px 3px rgba(0,0,0,0.18))' : undefined,
    transition: 'stroke-width 0.15s ease',
  }

  return (
    <>
      {/* 透明加粗路径用于扩大点击命中区域 */}
      <path
        d={edgePath}
        fill="none"
        stroke="transparent"
        strokeWidth={20}
        style={{ cursor: 'pointer' }}
      />
      <BaseEdge
        id={id}
        path={edgePath}
        markerEnd={markerEnd}
        markerStart={markerStart}
        style={mergedStyle}
      />
      {label && (
        <EdgeLabelRenderer>
          <div
            style={{
              position: 'absolute',
              transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
              pointerEvents: 'none',
              ...(labelStyle || {}),
              ...(labelBgStyle ? {
                background: (labelBgStyle as any).fill,
                padding: '2px 8px',
                borderRadius: 6,
              } : {}),
              ...(selected ? { fontWeight: 700, boxShadow: '0 2px 8px rgba(0,0,0,0.12)' } : {}),
            } as React.CSSProperties}
            className="nodrag nopan"
          >
            {String(label)}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  )
}

const edgeTypes = { floating: FloatingEdge }

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

// v2.43: 边类型改为 floating（浮动边），自动计算最佳起止点
// 选中态通过 FloatingEdge 内部处理（提升 strokeWidth + drop-shadow），避免被节点遮挡
const EDGE_COMMON = { type: 'floating' as const, animated: true, style: { strokeWidth: 2.5 } }

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
    <div className="fixed inset-0 z-[60] flex items-center justify-center p-4"
      style={{ background: 'rgba(0, 0, 0, 0.32)', backdropFilter: 'blur(4px)' }}
      onClick={onClose}>
      <div className="max-w-md w-full max-h-[70vh] flex flex-col"
        style={{ background: 'var(--background)', borderRadius: 'calc(var(--radius) * 1.1)', boxShadow: 'var(--shadow-apple-xl)' }}
        onClick={e => e.stopPropagation()}>
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4" style={{ borderBottom: '1px solid var(--border)' }}>
          <div className="flex items-center gap-2">
            <Puzzle className="w-5 h-5" style={{ color: 'var(--brand-500)' }} />
            <h2 className="text-base font-semibold" style={{ color: 'var(--foreground)' }}>选择模块</h2>
          </div>
          <button onClick={onClose} className="p-1" style={{ borderRadius: 'calc(var(--radius) * 0.5)' }}>
            <X className="w-4 h-4" style={{ color: 'var(--foreground-tertiary)' }} />
          </button>
        </div>

        {/* Search */}
        <div className="px-5 py-3">
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4" style={{ color: 'var(--foreground-tertiary)' }} />
            <input
              value={search}
              onChange={e => setSearch(e.target.value)}
              className="w-full pl-9 pr-3 py-2 text-sm border focus:outline-none"
              style={{ borderColor: 'var(--border)', borderRadius: 'calc(var(--radius) * 0.6)' }}
              placeholder="搜索模块名称..."
              autoFocus
            />
          </div>
        </div>

        {/* Module List */}
        <div className="flex-1 overflow-y-auto px-3 pb-3">
          {filtered.length === 0 ? (
            <p className="text-sm text-center py-8" style={{ color: 'var(--foreground-tertiary)' }}>
              {search ? '无匹配模块' : '所有模块已添加到图谱'}
            </p>
          ) : (
            <div className="space-y-1">
              {filtered.map(mod => (
                <button
                  key={mod.id}
                  onClick={() => onPick(mod)}
                  className="w-full text-left px-4 py-3 transition-all group"
                  style={{ borderRadius: 'calc(var(--radius) * 0.6)' }}
                >
                  <div className="flex items-center gap-3">
                    <div className="w-9 h-9 flex items-center justify-center flex-shrink-0 transition-colors" style={{ borderRadius: 'calc(var(--radius) * 0.5)', background: 'var(--background-muted)' }}>
                      <Puzzle className="w-4 h-4" style={{ color: 'var(--foreground-tertiary)' }} />
                    </div>
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-medium" style={{ color: 'var(--foreground-secondary)' }}>
                        {mod.name}
                      </p>
                      {mod.description && (
                        <p className="text-xs truncate mt-0.5" style={{ color: 'var(--foreground-tertiary)' }}>{mod.description}</p>
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
    <div className="fixed inset-0 z-[60] flex items-center justify-center p-4"
      style={{ background: 'rgba(0, 0, 0, 0.32)', backdropFilter: 'blur(4px)' }}
      onClick={onClose}>
      <div className="max-w-2xl w-full max-h-[85vh] flex flex-col"
        style={{ background: 'var(--background)', borderRadius: 'calc(var(--radius) * 1.1)', boxShadow: 'var(--shadow-apple-xl)' }}
        onClick={e => e.stopPropagation()}>
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4" style={{ borderBottom: '1px solid var(--border)' }}>
          <div className="flex items-center gap-2">
            <GitBranch className="w-5 h-5" style={{ color: 'var(--brand-500)' }} />
            <h3 className="text-base font-semibold" style={{ color: 'var(--foreground)' }}>影响功能说明</h3>
            <span className="text-xs px-2 py-0.5" style={{ borderRadius: 'calc(var(--radius) * 0.5)', background: 'rgba(0, 122, 255, 0.08)', color: 'var(--brand-500)' }}>{fpName}</span>
          </div>
          <button onClick={onClose} style={{ color: 'var(--foreground-tertiary)' }}>
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
          {error && (
            <div className="px-3 py-2 text-sm" style={{ borderRadius: 'calc(var(--radius) * 0.5)', background: 'var(--state-error-surface)', color: 'var(--state-error)' }}>{error}</div>
          )}

          {!data && !loading && (
            <div className="text-center py-8">
              <p className="text-sm mb-4" style={{ color: 'var(--foreground-tertiary)' }}>
                点击下方按钮，基于功能点依赖图谱和模块依赖图谱自动生成影响说明
              </p>
              <button onClick={generate}
                className="px-4 py-2 text-white text-sm font-medium" style={{ borderRadius: 'calc(var(--radius) * 0.5)', background: 'var(--brand-500)' }}>
                生成影响说明
              </button>
            </div>
          )}

          {loading && (
            <div className="flex items-center justify-center py-8">
              <Loader2 className="w-6 h-6 animate-spin" style={{ color: 'var(--brand-500)' }} />
              <span className="ml-2 text-sm" style={{ color: 'var(--foreground-tertiary)' }}>正在分析依赖图谱...</span>
            </div>
          )}

          {data && (
            <>
              {/* 受影响 FP */}
              {data.affected_fps.length > 0 && (
                <div>
                  <h4 className="text-sm font-semibold mb-2" style={{ color: 'var(--foreground-secondary)' }}>
                    受影响的功能点（{data.affected_fps.length}）
                  </h4>
                  <div className="space-y-1.5">
                    {data.affected_fps.map((fp, i) => (
                      <div key={i} className="flex items-center gap-2 text-xs px-3 py-1.5" style={{ background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.25)' }}>
                        <span className="font-medium" style={{ color: 'var(--foreground-secondary)' }}>{fp.fp_name}</span>
                        <span className="px-1.5 py-0.5" style={{ borderRadius: 'calc(var(--radius) * 0.25)', background: 'rgba(0, 122, 255, 0.12)', color: 'var(--brand-500)' }}>
                          {fp.relation_cn || fp.relation_type}
                        </span>
                        {fp.req_title && (
                          <span className="truncate" style={{ color: 'var(--foreground-tertiary)' }}>← {fp.req_title}</span>
                        )}
                        <span className="ml-auto" style={{ color: 'var(--foreground-tertiary)' }}>{fp.depth}跳</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* 受影响模块 */}
              {data.affected_modules.length > 0 && (
                <div>
                  <h4 className="text-sm font-semibold mb-2" style={{ color: 'var(--foreground-secondary)' }}>
                    受影响的模块（{data.affected_modules.length}）
                  </h4>
                  <div className="flex flex-wrap gap-1.5">
                    {data.affected_modules.map((m, i) => (
                      <span key={i} className="text-xs px-2 py-1" style={{ borderRadius: 'calc(var(--radius) * 0.25)', background: 'rgba(0, 122, 255, 0.08)', color: 'var(--brand-500)' }}>
                        {m.module_name}
                        <span className="ml-1" style={{ color: 'var(--brand-400)' }}>({m.reason})</span>
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {/* v2.35: 移除受影响需求展示（不再分析受影响的需求，改为基于 FP 依赖图谱分析受影响功能点） */}

              {/* 可编辑的影响说明 */}
              <div>
                <label className="block text-sm font-semibold mb-1.5" style={{ color: 'var(--foreground-secondary)' }}>
                  影响说明 {data.is_auto_generated && <span className="text-xs font-normal" style={{ color: 'var(--foreground-tertiary)' }}>（自动生成，可编辑）</span>}
                </label>
                <textarea
                  value={description}
                  onChange={e => setDescription(e.target.value)}
                  rows={5}
                  className="w-full px-3 py-2 text-sm border resize-none focus:outline-none"
                  style={{ borderColor: 'var(--border)', borderRadius: 'calc(var(--radius) * 0.5)' }}
                  placeholder="描述本次功能点变更的影响范围..."
                />
              </div>

              <div>
                <label className="block text-sm font-semibold mb-1.5" style={{ color: 'var(--foreground-secondary)' }}>
                  测试关注点
                </label>
                <textarea
                  value={testFocus}
                  onChange={e => setTestFocus(e.target.value)}
                  rows={3}
                  className="w-full px-3 py-2 text-sm border resize-none focus:outline-none"
                  style={{ borderColor: 'var(--border)', borderRadius: 'calc(var(--radius) * 0.5)' }}
                  placeholder="列出测试用例生成时应重点关注的回归场景..."
                />
              </div>
            </>
          )}
        </div>

        {/* Footer */}
        {data && (
          <div className="flex items-center justify-between px-5 py-3" style={{ borderTop: '1px solid var(--border)' }}>
            <button onClick={generate} disabled={loading}
              className="text-xs disabled:opacity-50" style={{ color: 'var(--foreground-tertiary)' }}>
              {loading ? '重新生成中...' : '重新生成'}
            </button>
            <div className="flex gap-2">
              <button onClick={onClose}
                className="px-3 py-1.5 text-sm" style={{ color: 'var(--foreground-secondary)', borderRadius: 'calc(var(--radius) * 0.5)' }}>
                取消
              </button>
              <button onClick={save} disabled={saving || !description.trim()}
                className="px-4 py-1.5 text-sm text-white disabled:opacity-50" style={{ borderRadius: 'calc(var(--radius) * 0.5)', background: 'var(--brand-500)' }}>
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
// v2.45 FP 节点详情 Drawer — 双击 FP 节点打开，展示基础信息 + 概括描述
// ============================================================================
function FPDetailDrawer({
  node, onClose, onShowImpact,
}: {
  node: Node
  onClose: () => void
  onShowImpact: (fpId: string, fpName: string, reqId: string) => void
}) {
  const data = node.data as any
  const fpStyle = FP_TYPE_STYLES[data.fpType] || FP_TYPE_STYLES.workflow
  const fpStatusStyle = FP_STATUS_STYLES[data.fpStatus] || FP_STATUS_STYLES.pending_review
  const moduleNames: string[] = Array.isArray(data.moduleNames) ? data.moduleNames : []

  return (
    <div className="fixed inset-0 z-[65]" onClick={onClose}>
      <div className="absolute inset-0" style={{ background: 'rgba(0, 0, 0, 0.32)', backdropFilter: 'blur(4px)' }} />
      <div className="absolute top-0 right-0 bottom-0 w-[420px] flex flex-col"
        style={{ background: 'var(--background)', boxShadow: 'var(--shadow-apple-xl)' }}
        onClick={e => e.stopPropagation()}>
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 shrink-0" style={{ borderBottom: '1px solid var(--border)' }}>
          <div className="flex items-center gap-2 min-w-0">
            <Boxes className="w-5 h-5 shrink-0" style={{ color: 'var(--brand-500)' }} />
            <h3 className="text-base font-semibold truncate" style={{ color: 'var(--foreground)' }}>{data.label}</h3>
            <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${fpStatusStyle.bg} ${fpStatusStyle.text} shrink-0`}>
              {fpStatusStyle.label}
            </span>
          </div>
          <button onClick={onClose} className="p-1 shrink-0" style={{ borderRadius: 'calc(var(--radius) * 0.5)' }}>
            <X className="w-4 h-4" style={{ color: 'var(--foreground-tertiary)' }} />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
          {/* 基础信息 */}
          <div>
            <h4 className="text-xs font-semibold uppercase tracking-wider mb-2" style={{ color: 'var(--foreground-tertiary)' }}>基础信息</h4>
            <div className="space-y-1.5 text-sm p-3" style={{ background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.5)' }}>
              <div className="flex items-center gap-2">
                <span className="text-xs w-20 shrink-0" style={{ color: 'var(--foreground-tertiary)' }}>类型</span>
                <span className={`text-[11px] px-1.5 py-0.5 rounded font-medium ${fpStyle.bg} ${fpStyle.text}`}>{fpStyle.label}</span>
              </div>
              {data.priority && (
                <div className="flex items-center gap-2">
                  <span className="text-xs w-20 shrink-0" style={{ color: 'var(--foreground-tertiary)' }}>优先级</span>
                  <span className="text-xs px-1.5 py-0.5 rounded font-medium" style={{ background: 'var(--background)', color: 'var(--foreground-secondary)' }}>{data.priority}</span>
                </div>
              )}
              {moduleNames.length > 0 && (
                <div className="flex items-center gap-2">
                  <span className="text-xs w-20 shrink-0" style={{ color: 'var(--foreground-tertiary)' }}>所属模块</span>
                  <span className="text-xs" style={{ color: 'var(--foreground-secondary)' }}>{moduleNames.join('、')}</span>
                </div>
              )}
              {data.reqTitle && (
                <div className="flex items-center gap-2">
                  <span className="text-xs w-20 shrink-0" style={{ color: 'var(--foreground-tertiary)' }}>所属需求</span>
                  <span className="text-xs truncate" style={{ color: 'var(--foreground-secondary)' }}>{data.reqTitle}</span>
                </div>
              )}
              <div className="flex items-center gap-2">
                <span className="text-xs w-20 shrink-0" style={{ color: 'var(--foreground-tertiary)' }}>状态</span>
                <span className={`text-[11px] px-1.5 py-0.5 rounded font-medium ${fpStatusStyle.bg} ${fpStatusStyle.text}`}>{fpStatusStyle.label}</span>
              </div>
            </div>
          </div>

          {/* 实现内容概括 */}
          <div>
            <h4 className="text-xs font-semibold uppercase tracking-wider mb-2" style={{ color: 'var(--foreground-tertiary)' }}>实现内容概括</h4>
            <div className="text-sm leading-relaxed p-3" style={{ background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.5)', color: 'var(--foreground-secondary)', minHeight: '40px' }}>
              {data.summary || <span style={{ color: 'var(--foreground-quaternary)' }}>暂无</span>}
            </div>
          </div>

          {/* 与其他功能点关系 */}
          <div>
            <h4 className="text-xs font-semibold uppercase tracking-wider mb-2" style={{ color: 'var(--foreground-tertiary)' }}>与其他功能点关系</h4>
            <div className="text-sm leading-relaxed p-3" style={{ background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.5)', color: 'var(--foreground-secondary)', minHeight: '40px' }}>
              {data.relationsSummary || <span style={{ color: 'var(--foreground-quaternary)' }}>暂无</span>}
            </div>
          </div>
        </div>

        {/* Footer */}
        {data.reqId && (
          <div className="flex justify-end gap-2 px-5 py-3 shrink-0" style={{ borderTop: '1px solid var(--border)' }}>
            <button onClick={() => onShowImpact(node.id, data.label, data.reqId)}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white"
              style={{ borderRadius: 'calc(var(--radius) * 0.5)', background: 'var(--brand-500)' }}>
              <GitBranch className="w-3.5 h-3.5" /> 生成影响说明
            </button>
          </div>
        )}
      </div>
    </div>
  )
}

// ============================================================================
// v2.45 版本管理 Drawer — 展示依赖版本快照、详情、对比
// ============================================================================
function VersionManagementDrawer({
  productId, onClose,
}: {
  productId: string
  onClose: () => void
}) {
  const [versions, setVersions] = useState<DependencyVersion[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [selectedIds, setSelectedIds] = useState<number[]>([])
  const [detail, setDetail] = useState<DependencyVersionDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [diff, setDiff] = useState<VersionDiff | null>(null)
  const [diffLoading, setDiffLoading] = useState(false)
  const [view, setView] = useState<'list' | 'detail' | 'diff'>('list')

  const loadVersions = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const data = await prodApi.listDependencyVersionsByProduct(productId)
      const sorted = [...(data || [])].sort((a, b) =>
        new Date(b.snapshot_at).getTime() - new Date(a.snapshot_at).getTime()
      )
      setVersions(sorted)
    } catch (e: any) {
      setError(e?.message || '加载失败')
    } finally {
      setLoading(false)
    }
  }, [productId])

  useEffect(() => {
    loadVersions()
  }, [loadVersions])

  const loadDetail = async (versionPk: number) => {
    setDetailLoading(true)
    setDiff(null)
    setError('')
    try {
      const data = await prodApi.getDependencyVersionDetail(versionPk)
      setDetail(data)
      setView('detail')
    } catch (e: any) {
      setError(e?.message || '加载详情失败')
    } finally {
      setDetailLoading(false)
    }
  }

  const toggleSelect = (id: number) => {
    setSelectedIds(prev => {
      if (prev.includes(id)) return prev.filter(x => x !== id)
      if (prev.length >= 2) return [prev[1], id]
      return [...prev, id]
    })
  }

  const runDiff = async () => {
    if (selectedIds.length !== 2) return
    setDiffLoading(true)
    setDiff(null)
    setError('')
    try {
      const result = await prodApi.diffDependencyVersions(selectedIds[0], selectedIds[1])
      setDiff(result)
      setView('diff')
    } catch (e: any) {
      setError(e?.message || '对比失败')
    } finally {
      setDiffLoading(false)
    }
  }

  const fmtDate = (s: string) => {
    if (!s) return '—'
    try { return new Date(s).toLocaleString('zh-CN') } catch { return s }
  }

  return (
    <div className="fixed inset-0 z-[65]" onClick={onClose}>
      <div className="absolute inset-0" style={{ background: 'rgba(0, 0, 0, 0.32)', backdropFilter: 'blur(4px)' }} />
      <div className="absolute top-0 right-0 bottom-0 w-[560px] max-w-[90vw] flex flex-col"
        style={{ background: 'var(--background)', boxShadow: 'var(--shadow-apple-xl)' }}
        onClick={e => e.stopPropagation()}>
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 shrink-0" style={{ borderBottom: '1px solid var(--border)' }}>
          <div className="flex items-center gap-2">
            <History className="w-5 h-5" style={{ color: 'var(--brand-500)' }} />
            <h3 className="text-base font-semibold" style={{ color: 'var(--foreground)' }}>版本管理</h3>
            <span className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>({versions.length})</span>
          </div>
          <button onClick={onClose} className="p-1" style={{ borderRadius: 'calc(var(--radius) * 0.5)' }}>
            <X className="w-4 h-4" style={{ color: 'var(--foreground-tertiary)' }} />
          </button>
        </div>

        {/* Sub-header: back button */}
        {view !== 'list' && (
          <div className="px-5 py-2 flex items-center gap-2 shrink-0" style={{ borderBottom: '1px solid var(--border)' }}>
            <button onClick={() => { setView('list'); setDetail(null); setDiff(null) }}
              className="text-xs flex items-center gap-1" style={{ color: 'var(--brand-500)' }}>
              ← 返回列表
            </button>
            <span className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>
              {view === 'detail' ? '版本详情' : '版本对比'}
            </span>
          </div>
        )}

        {/* Compare bar */}
        {view === 'list' && versions.length > 0 && (
          <div className="px-5 py-2 flex items-center gap-2 shrink-0" style={{ borderBottom: '1px solid var(--border)', background: 'var(--background-muted)' }}>
            <span className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>
              已选择 {selectedIds.length}/2 个版本进行对比
            </span>
            <button onClick={runDiff} disabled={selectedIds.length !== 2 || diffLoading}
              className="ml-auto flex items-center gap-1 px-3 py-1 text-xs font-medium text-white disabled:opacity-50"
              style={{ borderRadius: 'calc(var(--radius) * 0.5)', background: 'var(--brand-500)' }}>
              <ArrowLeftRight className="w-3 h-3" /> 对比版本
            </button>
          </div>
        )}

        {/* Body */}
        <div className="flex-1 overflow-y-auto">
          {error && (
            <div className="m-4 px-3 py-2 text-sm" style={{ borderRadius: 'calc(var(--radius) * 0.5)', background: 'var(--state-error-surface)', color: 'var(--state-error)' }}>{error}</div>
          )}

          {loading ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="w-6 h-6 animate-spin" style={{ color: 'var(--brand-500)' }} />
            </div>
          ) : view === 'list' ? (
            versions.length === 0 ? (
              <p className="text-sm text-center py-12" style={{ color: 'var(--foreground-tertiary)' }}>暂无版本记录</p>
            ) : (
              <div className="p-3 space-y-2">
                {versions.map(v => {
                  const isSelected = selectedIds.includes(v.id)
                  return (
                    <div key={v.id}
                      className="p-3 transition-all"
                      style={{
                        background: isSelected ? 'rgba(0, 122, 255, 0.04)' : 'var(--background-muted)',
                        borderRadius: 'calc(var(--radius) * 0.5)',
                        border: `1px solid ${isSelected ? 'var(--brand-400)' : 'var(--border)'}`,
                      }}>
                      <div className="flex items-center gap-2 mb-1.5">
                        <input type="checkbox" checked={isSelected}
                          onChange={() => toggleSelect(v.id)}
                          className="cursor-pointer shrink-0" />
                        <button onClick={() => loadDetail(v.id)}
                          className="text-sm font-medium hover:underline truncate" style={{ color: 'var(--foreground)' }}>
                          {v.version_id}
                        </button>
                        {v.is_active ? (
                          <span className="text-[10px] px-1.5 py-0.5 rounded font-medium bg-emerald-50 text-emerald-700 shrink-0">启用中</span>
                        ) : (
                          <span className="text-[10px] px-1.5 py-0.5 rounded font-medium bg-slate-100 text-slate-500 shrink-0">未启用</span>
                        )}
                        <span className="ml-auto text-[11px] shrink-0" style={{ color: 'var(--foreground-tertiary)' }}>{fmtDate(v.snapshot_at)}</span>
                      </div>
                      <div className="flex items-center gap-3 text-[11px] ml-6" style={{ color: 'var(--foreground-tertiary)' }}>
                        <span>{v.project_name}</span>
                        {v.go_live_date && <span>上线: {fmtDate(v.go_live_date)}</span>}
                      </div>
                      <div className="flex items-center gap-3 text-[11px] ml-6 mt-0.5" style={{ color: 'var(--foreground-tertiary)' }}>
                        <span>需求 {v.requirement_count}</span>
                        <span>FP {v.fp_count}</span>
                        <span>依赖 {v.dependency_count}</span>
                      </div>
                      {v.change_summary && (
                        <p className="text-[11px] mt-1 ml-6 truncate" style={{ color: 'var(--foreground-tertiary)' }}>{v.change_summary}</p>
                      )}
                    </div>
                  )
                })}
              </div>
            )
          ) : view === 'detail' ? (
            detailLoading ? (
              <div className="flex items-center justify-center py-12">
                <Loader2 className="w-6 h-6 animate-spin" style={{ color: 'var(--brand-500)' }} />
              </div>
            ) : detail ? (
              <div className="p-4 space-y-3">
                <div className="p-3 text-sm" style={{ background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.5)' }}>
                  <div className="flex items-center gap-2 mb-1">
                    <span className="font-medium" style={{ color: 'var(--foreground)' }}>{detail.version_id}</span>
                    {detail.is_active ? (
                      <span className="text-[10px] px-1.5 py-0.5 rounded font-medium bg-emerald-50 text-emerald-700">启用中</span>
                    ) : (
                      <span className="text-[10px] px-1.5 py-0.5 rounded font-medium bg-slate-100 text-slate-500">未启用</span>
                    )}
                  </div>
                  <div className="text-xs space-y-0.5" style={{ color: 'var(--foreground-tertiary)' }}>
                    <div>项目: {detail.project_name} ({detail.project_version})</div>
                    <div>快照时间: {fmtDate(detail.snapshot_at)}</div>
                    {detail.go_live_date && <div>上线时间: {fmtDate(detail.go_live_date)}</div>}
                    <div>需求/FP/依赖: {detail.requirement_count}/{detail.fp_count}/{detail.dependency_count}</div>
                    {detail.change_summary && <div>变更摘要: {detail.change_summary}</div>}
                  </div>
                </div>
                <div>
                  <h4 className="text-xs font-semibold uppercase tracking-wider mb-2" style={{ color: 'var(--foreground-tertiary)' }}>
                    依赖关系明细（{detail.items?.length || 0}）
                  </h4>
                  {(!detail.items || detail.items.length === 0) ? (
                    <p className="text-sm text-center py-6" style={{ color: 'var(--foreground-tertiary)' }}>无依赖关系记录</p>
                  ) : (
                    <div className="space-y-1.5">
                      {detail.items.map(item => {
                        const relLabel = FP_EDGE_STYLES[item.relation_type]?.label || item.relation_type
                        const rs = REVIEW_STATUS_STYLES[item.review_status] || REVIEW_STATUS_STYLES.pending
                        return (
                          <div key={item.id} className="p-2.5 text-xs" style={{ background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.5)', border: '1px solid var(--border)' }}>
                            <div className="flex items-center gap-1.5 mb-1 flex-wrap">
                              <span className="font-medium" style={{ color: 'var(--foreground-secondary)' }}>{item.source_fp_name}</span>
                              <span style={{ color: 'var(--foreground-tertiary)' }}>→</span>
                              <span className="font-medium" style={{ color: 'var(--foreground-secondary)' }}>{item.target_fp_name}</span>
                              <span className="px-1.5 py-0.5 rounded" style={{ background: 'rgba(0, 122, 255, 0.08)', color: 'var(--brand-500)' }}>{relLabel}</span>
                              <span className={`ml-auto px-1.5 py-0.5 rounded font-medium ${rs.bg} ${rs.text}`}>{rs.label}</span>
                            </div>
                            {item.description && (
                              <p className="mt-0.5" style={{ color: 'var(--foreground-tertiary)' }}>{item.description}</p>
                            )}
                            {item.source_req_title && (
                              <p className="mt-0.5 text-[11px]" style={{ color: 'var(--foreground-quaternary)' }}>所属需求: {item.source_req_title}</p>
                            )}
                          </div>
                        )
                      })}
                    </div>
                  )}
                </div>
              </div>
            ) : null
          ) : view === 'diff' ? (
            diffLoading ? (
              <div className="flex items-center justify-center py-12">
                <Loader2 className="w-6 h-6 animate-spin" style={{ color: 'var(--brand-500)' }} />
              </div>
            ) : diff ? (
              <div className="p-4 space-y-3">
                <div className="p-3" style={{ background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.5)' }}>
                  <div className="text-xs mb-2" style={{ color: 'var(--foreground-tertiary)' }}>对比版本</div>
                  <div className="flex items-center gap-2 text-sm flex-wrap">
                    <span className="font-medium" style={{ color: 'var(--foreground)' }}>{diff.version_1.version_id}</span>
                    <span className="text-[11px]" style={{ color: 'var(--foreground-tertiary)' }}>{fmtDate(diff.version_1.snapshot_at)}</span>
                    <ArrowLeftRight className="w-3.5 h-3.5" style={{ color: 'var(--foreground-tertiary)' }} />
                    <span className="font-medium" style={{ color: 'var(--foreground)' }}>{diff.version_2.version_id}</span>
                    <span className="text-[11px]" style={{ color: 'var(--foreground-tertiary)' }}>{fmtDate(diff.version_2.snapshot_at)}</span>
                  </div>
                  <div className="flex items-center gap-3 mt-2 text-xs">
                    <span style={{ color: 'var(--state-success)' }}>+{diff.summary.added_count} 新增</span>
                    <span style={{ color: 'var(--state-error)' }}>-{diff.summary.removed_count} 删除</span>
                    <span style={{ color: 'var(--state-warning)' }}>~{diff.summary.modified_count} 修改</span>
                    <span style={{ color: 'var(--foreground-tertiary)' }}>{diff.summary.unchanged_count} 不变</span>
                  </div>
                </div>

                {/* 新增 */}
                {diff.added.length > 0 && (
                  <div>
                    <h4 className="text-xs font-semibold mb-2" style={{ color: 'var(--state-success)' }}>
                      新增依赖（{diff.added.length}）
                    </h4>
                    <div className="space-y-1.5">
                      {diff.added.map((d, i) => {
                        const relLabel = FP_EDGE_STYLES[d.relation_type]?.label || d.relation_type
                        return (
                          <div key={i} className="p-2.5 text-xs" style={{ background: 'var(--state-success-surface)', borderRadius: 'calc(var(--radius) * 0.5)', borderLeft: '3px solid var(--state-success)' }}>
                            <div className="flex items-center gap-1.5 flex-wrap">
                              <span className="font-medium" style={{ color: 'var(--foreground-secondary)' }}>{d.source_fp_name}</span>
                              <span style={{ color: 'var(--foreground-tertiary)' }}>→</span>
                              <span className="font-medium" style={{ color: 'var(--foreground-secondary)' }}>{d.target_fp_name}</span>
                              <span className="px-1.5 py-0.5 rounded" style={{ background: 'rgba(0, 122, 255, 0.08)', color: 'var(--brand-500)' }}>{relLabel}</span>
                            </div>
                            {d.description && <p className="mt-0.5" style={{ color: 'var(--foreground-tertiary)' }}>{d.description}</p>}
                          </div>
                        )
                      })}
                    </div>
                  </div>
                )}

                {/* 删除 */}
                {diff.removed.length > 0 && (
                  <div>
                    <h4 className="text-xs font-semibold mb-2" style={{ color: 'var(--state-error)' }}>
                      删除依赖（{diff.removed.length}）
                    </h4>
                    <div className="space-y-1.5">
                      {diff.removed.map((d, i) => {
                        const relLabel = FP_EDGE_STYLES[d.relation_type]?.label || d.relation_type
                        return (
                          <div key={i} className="p-2.5 text-xs" style={{ background: 'var(--state-error-surface)', borderRadius: 'calc(var(--radius) * 0.5)', borderLeft: '3px solid var(--state-error)' }}>
                            <div className="flex items-center gap-1.5 flex-wrap">
                              <span className="font-medium" style={{ color: 'var(--foreground-secondary)' }}>{d.source_fp_name}</span>
                              <span style={{ color: 'var(--foreground-tertiary)' }}>→</span>
                              <span className="font-medium" style={{ color: 'var(--foreground-secondary)' }}>{d.target_fp_name}</span>
                              <span className="px-1.5 py-0.5 rounded" style={{ background: 'rgba(0, 122, 255, 0.08)', color: 'var(--brand-500)' }}>{relLabel}</span>
                            </div>
                            {d.description && <p className="mt-0.5" style={{ color: 'var(--foreground-tertiary)' }}>{d.description}</p>}
                          </div>
                        )
                      })}
                    </div>
                  </div>
                )}

                {/* 修改 */}
                {diff.modified.length > 0 && (
                  <div>
                    <h4 className="text-xs font-semibold mb-2" style={{ color: 'var(--state-warning)' }}>
                      修改依赖（{diff.modified.length}）
                    </h4>
                    <div className="space-y-1.5">
                      {diff.modified.map((d, i) => {
                        const relLabel = FP_EDGE_STYLES[d.relation_type]?.label || d.relation_type
                        return (
                          <div key={i} className="p-2.5 text-xs" style={{ background: 'var(--state-warning-surface)', borderRadius: 'calc(var(--radius) * 0.5)', borderLeft: '3px solid var(--state-warning)' }}>
                            <div className="flex items-center gap-1.5 mb-1 flex-wrap">
                              <span className="font-medium" style={{ color: 'var(--foreground-secondary)' }}>{d.source_fp_name}</span>
                              <span style={{ color: 'var(--foreground-tertiary)' }}>→</span>
                              <span className="font-medium" style={{ color: 'var(--foreground-secondary)' }}>{d.target_fp_name}</span>
                              <span className="px-1.5 py-0.5 rounded" style={{ background: 'rgba(0, 122, 255, 0.08)', color: 'var(--brand-500)' }}>{relLabel}</span>
                            </div>
                            {d.old_description !== d.new_description && (
                              <div className="mt-1 text-[11px] space-y-0.5" style={{ color: 'var(--foreground-tertiary)' }}>
                                <div>旧: {d.old_description || '—'}</div>
                                <div>新: {d.new_description || '—'}</div>
                              </div>
                            )}
                            {d.old_review_status !== d.new_review_status && (
                              <div className="mt-1 text-[11px]" style={{ color: 'var(--foreground-tertiary)' }}>
                                审核状态: {d.old_review_status || '—'} → {d.new_review_status || '—'}
                              </div>
                            )}
                          </div>
                        )
                      })}
                    </div>
                  </div>
                )}

                {diff.added.length === 0 && diff.removed.length === 0 && diff.modified.length === 0 && (
                  <p className="text-sm text-center py-8" style={{ color: 'var(--foreground-tertiary)' }}>两个版本无差异</p>
                )}
              </div>
            ) : null
          ) : null}
        </div>
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
    <div className="absolute top-0 right-0 bottom-0 w-96 flex flex-col z-30" style={{ background: 'var(--background)', borderLeft: '1px solid var(--border)', boxShadow: 'var(--shadow-apple-xl)' }}>
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3" style={{ borderBottom: '1px solid var(--border)' }}>
        <div className="flex items-center gap-2">
          <History className="w-4 h-4" style={{ color: 'var(--brand-500)' }} />
          <h3 className="text-sm font-semibold" style={{ color: 'var(--foreground)' }}>依赖变动历史</h3>
          <span className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>({items.length})</span>
        </div>
        <button onClick={onClose} className="p-1" style={{ borderRadius: 'calc(var(--radius) * 0.5)' }}>
          <X className="w-4 h-4" style={{ color: 'var(--foreground-tertiary)' }} />
        </button>
      </div>

      {/* Filter */}
      <div className="px-4 py-2 flex items-center gap-1.5 flex-wrap" style={{ borderBottom: '1px solid var(--border)' }}>
        <span className="text-[11px]" style={{ color: 'var(--foreground-tertiary)' }}>操作类型:</span>
        <button onClick={() => setActionFilter('')}
          className="text-[11px] px-2 py-0.5"
          style={!actionFilter ? { background: 'var(--foreground)', color: 'var(--background)', borderRadius: 'calc(var(--radius) * 0.25)' } : { background: 'var(--background-muted)', color: 'var(--foreground-secondary)', borderRadius: 'calc(var(--radius) * 0.25)' }}>
          全部
        </button>
        {Object.entries(ACTION_LABELS).map(([key, val]) => (
          <button key={key} onClick={() => setActionFilter(actionFilter === key ? '' : key)}
            className={`text-[11px] px-2 py-0.5 ${actionFilter === key ? '' : `${val.bg} ${val.text} hover:opacity-80`}`}
            style={actionFilter === key ? { background: 'var(--foreground)', color: 'var(--background)', borderRadius: 'calc(var(--radius) * 0.25)' } : { borderRadius: 'calc(var(--radius) * 0.25)' }}>
            {val.label}
          </button>
        ))}
      </div>

      {/* List */}
      <div className="flex-1 overflow-y-auto p-3 space-y-2">
        {loading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="w-5 h-5 animate-spin" style={{ color: 'var(--brand-500)' }} />
          </div>
        ) : filtered.length === 0 ? (
          <p className="text-sm text-center py-8" style={{ color: 'var(--foreground-tertiary)' }}>暂无历史记录</p>
        ) : (
          filtered.map(h => {
            const action = ACTION_LABELS[h.action] || { label: h.action, bg: 'bg-slate-100', text: 'text-slate-700' }
            return (
              <div key={h.id} className="p-2.5 text-xs" style={{ background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.5)', border: '1px solid var(--border)' }}>
                <div className="flex items-center gap-1.5 mb-1.5">
                  <span className={`px-1.5 py-0.5 rounded font-medium ${action.bg} ${action.text}`} style={{ borderRadius: 'calc(var(--radius) * 0.25)' }}>
                    {action.label}
                  </span>
                  <span style={{ color: 'var(--foreground-tertiary)' }}>{new Date(h.created_at).toLocaleString('zh-CN')}</span>
                  {h.changed_by && <span className="ml-auto" style={{ color: 'var(--foreground-tertiary)' }}>by {h.changed_by}</span>}
                </div>
                <div style={{ color: 'var(--foreground-secondary)' }}>
                  <span className="font-medium">{h.source_name}</span>
                  <span className="mx-1" style={{ color: 'var(--foreground-tertiary)' }}>→</span>
                  <span className="font-medium">{h.target_name}</span>
                  <span className="ml-1.5" style={{ color: 'var(--foreground-tertiary)' }}>[{h.relation_type}]</span>
                </div>
                {(h.old_review_status || h.new_review_status) && (
                  <div className="mt-1 text-[11px]" style={{ color: 'var(--foreground-tertiary)' }}>
                    状态变更：
                    <span style={{ color: 'var(--foreground-secondary)' }}>{h.old_review_status || '—'}</span>
                    <span className="mx-1">→</span>
                    <span style={{ color: 'var(--foreground-secondary)' }}>{h.new_review_status || '—'}</span>
                  </div>
                )}
                {h.change_reason && (
                  <p className="mt-1 text-[11px] italic" style={{ color: 'var(--foreground-tertiary)' }}>原因：{h.change_reason}</p>
                )}
                {h.related_req_id && (
                  <p className="mt-1 text-[11px]" style={{ color: 'var(--foreground-tertiary)' }}>关联需求：{h.related_req_id}</p>
                )}
                {h.dependency_id && h.action !== 'deleted' && (
                  <button onClick={() => onReviewJump(h.dependency_id)}
                    className="mt-1.5 text-[11px]" style={{ color: 'var(--brand-500)' }}>
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
    <div className="fixed inset-0 z-[70] flex items-center justify-center p-4"
      style={{ background: 'rgba(0, 0, 0, 0.32)', backdropFilter: 'blur(4px)' }}
      onClick={onClose}>
      <div className="max-w-md w-full"
        style={{ background: 'var(--background)', borderRadius: 'calc(var(--radius) * 1.1)', boxShadow: 'var(--shadow-apple-xl)' }}
        onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between px-5 py-4" style={{ borderBottom: '1px solid var(--border)' }}>
          <div className="flex items-center gap-2">
            <CheckCircle className="w-5 h-5" style={{ color: 'var(--brand-500)' }} />
            <h3 className="text-base font-semibold" style={{ color: 'var(--foreground)' }}>审核依赖关系</h3>
          </div>
          <button onClick={onClose} className="p-1" style={{ borderRadius: 'calc(var(--radius) * 0.5)' }}>
            <X className="w-4 h-4" style={{ color: 'var(--foreground-tertiary)' }} />
          </button>
        </div>

        <div className="px-5 py-4 space-y-3">
          {error && <div className="px-3 py-2 text-sm" style={{ borderRadius: 'calc(var(--radius) * 0.5)', background: 'var(--state-error-surface)', color: 'var(--state-error)' }}>{error}</div>}

          <div className="p-3 text-sm" style={{ background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.5)' }}>
            <div className="text-xs mb-1" style={{ color: 'var(--foreground-tertiary)' }}>依赖关系</div>
            <div style={{ color: 'var(--foreground-secondary)' }}>
              <span className="font-medium">{sourceName}</span>
              <span className="mx-1.5" style={{ color: 'var(--foreground-tertiary)' }}>→</span>
              <span className="font-medium">{targetName}</span>
            </div>
            <div className="mt-1 text-[11px]" style={{ color: 'var(--foreground-tertiary)' }}>
              当前状态：<span className="font-medium">{REVIEW_STATUS_STYLES[currentStatus]?.label || currentStatus}</span>
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium mb-1.5" style={{ color: 'var(--foreground-secondary)' }}>审核操作</label>
            <div className="flex gap-2">
              <button onClick={() => setAction('approve')}
                className="flex-1 px-3 py-2 text-sm font-medium border-2 transition-colors"
                style={action === 'approve'
                  ? { borderColor: 'var(--state-success)', background: 'var(--state-success-surface)', color: 'var(--state-success)', borderRadius: 'calc(var(--radius) * 0.5)' }
                  : { borderColor: 'var(--border)', color: 'var(--foreground-secondary)', borderRadius: 'calc(var(--radius) * 0.5)' }
                }>
                <CheckCircle className="w-4 h-4 inline mr-1" /> 审核通过
              </button>
              <button onClick={() => setAction('reject')}
                className="flex-1 px-3 py-2 text-sm font-medium border-2 transition-colors"
                style={action === 'reject'
                  ? { borderColor: 'var(--state-error)', background: 'var(--state-error-surface)', color: 'var(--state-error)', borderRadius: 'calc(var(--radius) * 0.5)' }
                  : { borderColor: 'var(--border)', color: 'var(--foreground-secondary)', borderRadius: 'calc(var(--radius) * 0.5)' }
                }>
                <XCircle className="w-4 h-4 inline mr-1" /> 驳回
              </button>
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium mb-1.5" style={{ color: 'var(--foreground-secondary)' }}>
              审核原因 {action === 'reject' && <span style={{ color: 'var(--state-error)' }}>*</span>}
            </label>
            <textarea
              value={reason}
              onChange={e => setReason(e.target.value)}
              rows={3}
              className="w-full px-3 py-2 text-sm border resize-none focus:outline-none"
              style={{ borderColor: 'var(--border)', borderRadius: 'calc(var(--radius) * 0.5)' }}
              placeholder={action === 'reject' ? '请填写驳回原因（必填）' : '可填写审核说明（选填）'}
            />
          </div>
        </div>

        <div className="flex justify-end gap-2 px-5 py-3" style={{ borderTop: '1px solid var(--border)' }}>
          <button onClick={onClose}
            className="px-3 py-1.5 text-sm" style={{ color: 'var(--foreground-secondary)', borderRadius: 'calc(var(--radius) * 0.5)' }}>
            取消
          </button>
          <button onClick={submit} disabled={submitting}
            className="px-4 py-1.5 text-sm text-white disabled:opacity-50" style={{ borderRadius: 'calc(var(--radius) * 0.5)', background: 'var(--brand-500)' }}>
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

  // v2.45: FP 节点详情 Drawer + 版本管理 Drawer
  const [fpDetailNode, setFpDetailNode] = useState<Node | null>(null)
  const [versionDrawerOpen, setVersionDrawerOpen] = useState(false)

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
    // v2.45 修复：任何产品切换（包括 A→B）都先关闭所有 Drawer/Modal，
    // 避免显示旧产品数据或在错误产品上执行 FP 影响说明/审核操作
    setFpDetailNode(null)
    setImpactModal(null)
    setVersionDrawerOpen(false)
    setHistoryOpen(false)
    setReviewModal(null)
    setCtxMenu(null)

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

  // v2.43: 布局方向状态（TB=自上而下，LR=从左到右），dagre 布局使用
  const [layoutDirection, setLayoutDirection] = useState<LayoutDirection>('TB')

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
      // v2.43: 应用 dagre 层次布局（若用户已自定义节点位置则保留，否则按依赖关系分层）
      const hasCustomPos = (data?.nodes || []).some((n: any) => n.position)
      const finalNodes = hasCustomPos
        ? graphNodes
        : layoutHierarchical(graphNodes, graphEdges, {
            direction: layoutDirection,
            nodeSep: 60,
            rankSep: 120,
            startX: 80,
            startY: 80,
          })
      setNodes(finalNodes)
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

      // v2.44: 构建 moduleId → moduleName 映射，用于在节点上显示所属模块名
      const moduleNameMap: Record<string, string> = {}
      ;(data?.modules || []).forEach((m: { id: string; name: string }) => {
        moduleNameMap[m.id] = m.name
      })

      // v2.3: 统计每个节点的 pending 依赖数，用于在节点上显示"待审核"标签
      const pendingDepNodeIds = new Set<string>()
      ;(data?.edges || []).forEach((e: any) => {
        if (e.review_status === 'pending') {
          pendingDepNodeIds.add(e.source)
          pendingDepNodeIds.add(e.target)
        }
      })

      const fpNodes: Node[] = (data?.nodes || []).map((n: any, i: number) => {
        // v2.44: 解析所属模块名列表（优先使用 modules 多模块字段，回退到 module_id）
        const moduleIds: string[] = Array.isArray(n.modules) && n.modules.length > 0
          ? n.modules
          : (n.module_id ? [n.module_id] : [])
        const moduleNames = moduleIds.map(id => moduleNameMap[id]).filter(Boolean)
        return {
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
            moduleNames,               // v2.44: 所属模块名列表（用于节点显示）
            hasPendingReview: pendingDepNodeIds.has(n.id),  // v2.3: 是否含未审核依赖
            summary: n.summary || '',                    // v2.45: FP 实现内容概括
            relationsSummary: n.relations_summary || '', // v2.45: 与其他 FP 关系概括
            fpStatus: n.fp_status || '',                 // v2.45: FP 自身状态
          },
        }
      })
      const nameMap: Record<string, string> = {}
      fpNodes.forEach(n => { nameMap[n.id] = String(n.data.label) })
      const fpEdges: Edge[] = (data?.edges || []).map((e: any) =>
        createFPEdge(
          e.id, e.source, e.target, e.relation_type,
          nameMap[e.source], nameMap[e.target], e.description,
          e.review_status || 'pending',
        )
      )
      // v2.43: FP 图谱应用 dagre 层次布局（FP 节点位置不持久化，每次重新布局）
      const finalFpNodes = layoutHierarchical(fpNodes, fpEdges, {
        direction: layoutDirection,
        nodeSep: 50,
        rankSep: 110,
        startX: 80,
        startY: 80,
      })
      setNodes(finalFpNodes)
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

  // Double-click: 模块模式打开 picker 交换节点；FP 模式打开节点详情 Drawer
  const onNodeDoubleClick = useCallback((_: any, node: Node) => {
    if (graphType === 'fp') {
      // v2.45: FP 模式双击打开详情 Drawer（影响说明可通过 Drawer 内按钮打开）
      setFpDetailNode(node)
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
      if (e.key === 'Escape') {
        // v2.45 增强：Esc 按优先级关闭 Drawer/Modal，最后才关全屏/产品 picker
        if (ctxMenu) { setCtxMenu(null); return }
        if (reviewModal) { setReviewModal(null); return }
        if (impactModal) { setImpactModal(null); return }
        if (fpDetailNode) { setFpDetailNode(null); return }
        if (versionDrawerOpen) { setVersionDrawerOpen(false); return }
        if (historyOpen) { setHistoryOpen(false); return }
        setFullscreen(false); setPickerOpen(false)
      }
      if ((e.key === 'Delete' || e.key === 'Backspace') &&
        document.activeElement?.tagName !== 'INPUT' &&
        document.activeElement?.tagName !== 'TEXTAREA') {
        removeSelected()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [nodes, edges, ctxMenu, reviewModal, impactModal, fpDetailNode, versionDrawerOpen, historyOpen])

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
    <div className={`flex flex-col ${fullscreen ? 'fixed inset-0 z-50' : 'h-[calc(100vh-5rem)]'}`} style={fullscreen ? { background: 'var(--background)' } : undefined}>
      {/* Top Toolbar — flex-wrap 自动多行，避免工具拥挤 */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-5 py-2.5 shrink-0" style={{ borderBottom: '1px solid var(--border)', background: 'var(--background)' }}>
        <div className="flex items-center gap-2 shrink-0">
          <GitBranch className="w-5 h-5" style={{ color: 'var(--brand-500)' }} />
          <div className="flex items-center gap-1 p-0.5" style={{ background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.5)' }}>
            <button onClick={() => switchGraphType('modules')}
              className="px-3 py-1 text-xs font-medium transition-colors"
              style={graphType === 'modules'
                ? { background: 'var(--background)', color: 'var(--foreground)', borderRadius: 'calc(var(--radius) * 0.25)', boxShadow: 'var(--shadow-apple-sm)' }
                : { color: 'var(--foreground-tertiary)', borderRadius: 'calc(var(--radius) * 0.25)' }}>
              模块依赖
            </button>
            <button onClick={() => switchGraphType('fp')}
              className="px-3 py-1 text-xs font-medium transition-colors"
              style={graphType === 'fp'
                ? { background: 'var(--background)', color: 'var(--foreground)', borderRadius: 'calc(var(--radius) * 0.25)', boxShadow: 'var(--shadow-apple-sm)' }
                : { color: 'var(--foreground-tertiary)', borderRadius: 'calc(var(--radius) * 0.25)' }}>
              功能点依赖
            </button>
          </div>
          {productName && <span className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>· {productName}</span>}
        </div>

        {/* v2.3: FP 图谱筛选器 — 模块 + 审核状态 */}
        {selectedProductId && graphType === 'fp' && (
          <div className="flex items-center gap-1.5 shrink-0">
            <div className="flex items-center gap-1.5">
              <Filter className="w-3.5 h-3.5" style={{ color: 'var(--foreground-tertiary)' }} />
              <select value={moduleFilter} onChange={e => setModuleFilter(e.target.value)}
                className="text-xs px-2 py-1 border focus:outline-none max-w-[140px]"
                style={{ borderColor: 'var(--border)', borderRadius: 'calc(var(--radius) * 0.5)', background: 'var(--background)', color: 'var(--foreground-secondary)' }}>
                <option value="">全部模块（全局总览）</option>
                {graphModules.map(m => (
                  <option key={m.id} value={m.id}>{m.name}</option>
                ))}
              </select>
              <select value={reviewStatusFilter} onChange={e => setReviewStatusFilter(e.target.value)}
                className="text-xs px-2 py-1 border focus:outline-none"
                style={{ borderColor: 'var(--border)', borderRadius: 'calc(var(--radius) * 0.5)', background: 'var(--background)', color: 'var(--foreground-secondary)' }}>
                <option value="">全部状态</option>
                <option value="pending">仅待审核</option>
                <option value="approved">仅已审核</option>
                <option value="rejected">仅已驳回</option>
              </select>
            </div>
            <div className="w-px h-5" style={{ background: 'var(--border)' }} />
            <button onClick={toggleHistory}
              className="btn-ghost text-xs flex items-center gap-1"
              style={historyOpen ? { background: 'rgba(0, 122, 255, 0.08)', color: 'var(--brand-500)' } : undefined}>
              <History className="w-3.5 h-3.5" /> 变动历史
            </button>
          </div>
        )}

        {/* Draggable toolbar items — 仅模块模式显示 */}
        {selectedProductId && graphType === 'modules' && (
          <div className="flex items-center gap-1.5 px-2 shrink-0">
            {/* Module drag item */}
            <div
              draggable
              onDragStart={(e) => { e.dataTransfer.setData('application/reactflow', 'module'); e.dataTransfer.effectAllowed = 'move' }}
              className="flex items-center gap-1.5 px-3 py-1.5 border text-xs font-medium cursor-grab active:cursor-grabbing select-none transition-colors"
              style={{ borderRadius: 'calc(var(--radius) * 0.5)', background: 'rgba(0, 122, 255, 0.08)', borderColor: 'rgba(0, 122, 255, 0.2)', color: 'var(--brand-500)' }}
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
              className="flex items-center gap-1.5 px-3 py-1.5 border text-xs font-medium cursor-grab active:cursor-grabbing select-none transition-colors"
              style={{ borderRadius: 'calc(var(--radius) * 0.5)', background: 'rgba(0, 199, 190, 0.08)', borderColor: 'rgba(0, 199, 190, 0.2)', color: '#00c7be' }}
              title="拖拽到画布后，从节点拖出连线"
            >
              <GripVertical className="w-3 h-3 opacity-40" />
              <Link2 className="w-3.5 h-3.5" />
              <span>连线</span>
            </div>
          </div>
        )}

        {/* Edge type selector — 按模式显示不同选项 */}
        {selectedProductId && (
          <div className="flex items-center gap-1.5 shrink-0">
            <span className="text-[11px]" style={{ color: 'var(--foreground-tertiary)' }}>连线类型:</span>
            {edgeTypeOptions.map(([key, val]) => (
              <button key={key} onClick={() => setEdgeType(key)}
                className="px-2.5 py-1 text-xs font-medium border transition-colors"
                style={edgeType === key
                  ? { background: `${val.stroke}1a`, borderColor: val.stroke, color: val.stroke, borderRadius: 'calc(var(--radius) * 0.5)' }
                  : { borderColor: 'var(--border)', color: 'var(--foreground-tertiary)', borderRadius: 'calc(var(--radius) * 0.5)' }
                }>
                {graphType === 'fp' && (key === 'invariants' || key === 'lifecycle_scenarios') ? `↔ ${val.label}` : `→ ${val.label}`}
              </button>
            ))}
          </div>
        )}

        {/* v2.3: 审核状态图例 — FP 模式下显示 */}
        {selectedProductId && graphType === 'fp' && (
          <div className="flex items-center gap-2 shrink-0">
            {Object.entries(REVIEW_STATUS_STYLES).map(([key, val]) => (
              <div key={key} className="flex items-center gap-1 text-[10px]" style={{ color: 'var(--foreground-tertiary)' }}>
                <div className={`w-4 h-0.5 ${val.dashed ? 'border-t-2 border-dashed' : ''}`}
                  style={{ borderColor: val.stroke, background: val.dashed ? 'transparent' : val.stroke }} />
                <span>{val.label}</span>
              </div>
            ))}
          </div>
        )}

        {/* 右侧操作组：版本管理 + 刷新 + 全屏 — ml-auto 推到行尾，宽度不够时自动换到下一行 */}
        <div className="flex items-center gap-2 ml-auto shrink-0">
          {selectedProductId && (
            <button onClick={() => setVersionDrawerOpen(true)} className="btn-ghost text-xs flex items-center gap-1"
              title="查看依赖版本快照与对比">
              <History className="w-3.5 h-3.5" /> 版本管理
            </button>
          )}
          <button onClick={() => graphType === 'fp' ? loadFPGraph() : loadGraph()} disabled={loading} className="btn-ghost text-xs">
            <Loader2 className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} /> 刷新
          </button>
          <button onClick={toggleFullscreen} className="btn-ghost text-xs" title={fullscreen ? '退出全屏 (Esc)' : '全屏'}>
            {fullscreen ? <Minimize className="w-3.5 h-3.5" /> : <Maximize className="w-3.5 h-3.5" />}
          </button>
        </div>
      </div>

      {/* Main Content */}
      <div className="flex flex-1 min-h-0">
        {/* Left Sidebar — Legend & Stats only */}
        <div className="w-48 flex flex-col shrink-0 p-3" style={{ borderRight: '1px solid var(--border)', background: 'var(--background-muted)' }}>
          {!selectedProductId ? (
            <p className="text-xs text-center py-8" style={{ color: 'var(--foreground-tertiary)' }}>请先在顶部栏选择产品</p>
          ) : (
            <>
              <p className="text-[10px] uppercase tracking-wider font-medium mb-3" style={{ color: 'var(--foreground-tertiary)' }}>图谱信息</p>
              <div className="space-y-2 text-xs" style={{ color: 'var(--foreground-secondary)' }}>
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

              <div className="mt-4 pt-3" style={{ borderTop: '1px solid var(--border)' }}>
                <p className="text-[10px] uppercase tracking-wider font-medium mb-2" style={{ color: 'var(--foreground-tertiary)' }}>图例</p>
                <div className="space-y-1.5">
                  {Object.entries(currentEdgeStyles).map(([key, val]) => (
                    <div key={key} className="flex items-center gap-2 text-[11px]">
                      <div className="w-5 h-0.5 rounded-full" style={{ background: val.stroke }} />
                      <span style={{ color: 'var(--foreground-secondary)' }}>{val.label}</span>
                    </div>
                  ))}
                </div>
              </div>

              <div className="mt-auto pt-3" style={{ borderTop: '1px solid var(--border)' }}>
                <p className="text-[10px] leading-relaxed" style={{ color: 'var(--foreground-tertiary)' }}>
                  {graphType === 'fp'
                    ? '双击节点查看详情 · 拖拽节点手柄连线 · 右键删除/交换方向 · 位置不持久化'
                    : '从工具栏拖拽模块到画布 · 拖拽节点手柄连线 · 双击节点切换模块'}
                </p>
              </div>
            </>
          )}
        </div>

        {/* Graph Canvas */}
        <div className="flex-1 relative" style={{ background: 'var(--background-muted)' }}
          onDragOver={(e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'move' }}
          onDrop={(e) => {
            if (graphType === 'fp') return  // FP 模式不支持拖放添加
            e.preventDefault()
            const itemType = e.dataTransfer.getData('application/reactflow')
            if (itemType === 'module') openAddPicker()
          }}
        >
          {loading ? (
            <div className="absolute inset-0 flex items-center justify-center z-10" style={{ background: 'rgba(255, 255, 255, 0.6)' }}>
              <Loader2 className="w-8 h-8 animate-spin" style={{ color: 'var(--brand-400)' }} />
            </div>
          ) : !selectedProductId ? (
            <div className="absolute inset-0 flex flex-col items-center justify-center" style={{ color: 'var(--foreground-tertiary)' }}>
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
                edgeTypes={edgeTypes}
                // v2.43: Loose 模式允许任意 Handle 互相连接（浮动边核心配置）
                connectionMode={ConnectionMode.Loose}
                // v2.43: 选中边时提升 z-index，避免被节点遮挡
                elevateEdgesOnSelect
                fitView fitViewOptions={{ padding: 0.3 }}
                selectionMode={SelectionMode.Partial} selectionOnDrag
                panOnDrag={[1, 2]} deleteKeyCode={null} multiSelectionKeyCode="Shift"
                connectionLineStyle={{ stroke: currentEdgeStyles[edgeType]?.stroke || '#818cf8', strokeWidth: 2 }}
                defaultEdgeOptions={{ type: 'floating', animated: true }}
              >
                <Background color="#e2e8f0" gap={20} size={1} />
                <Controls showInteractive={false} className="!rounded-xl !shadow-md !border-[var(--border)]" />
                <MiniMap nodeStrokeColor="#818cf8" nodeColor="#eef2ff" nodeBorderRadius={12}
                  maskColor="rgba(0,0,0,0.05)" style={{ bottom: 12, right: 12 }}
                  className="!rounded-xl !shadow-md !border-[var(--border)]" />

                {nodes.length === 0 && (
                  <Panel position="top-center">
                    <div className="px-4 py-3 text-center" style={{ background: 'rgba(255, 255, 255, 0.9)', backdropFilter: 'blur(4px)', borderRadius: 'calc(var(--radius) * 1.1)', boxShadow: 'var(--shadow-apple-md)', border: '1px solid var(--border)' }}>
                      <p className="text-sm font-medium mb-1" style={{ color: 'var(--foreground-secondary)' }}>画布为空</p>
                      <p className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>
                        {graphType === 'fp'
                          ? '当前产品下暂无功能点，请先在需求详情中拆分功能点'
                          : '从工具栏拖拽「模块」到画布添加，拖拽节点间的手柄创建连线'}
                      </p>
                    </div>
                  </Panel>
                )}

                {selectedEdge && (
                  <Panel position="bottom-center">
                    <div className="px-4 py-2.5 flex items-center gap-3" style={{ background: 'rgba(255, 255, 255, 0.95)', backdropFilter: 'blur(4px)', borderRadius: 'calc(var(--radius) * 1.1)', boxShadow: 'var(--shadow-apple-md)', border: '1px solid var(--border)' }}>
                      <div className="flex items-center gap-1.5 text-xs">
                        <span className="w-2 h-2 rounded-full"
                          style={{ background: currentEdgeStyles[(selectedEdge.data as any)?.eType]?.stroke || '#818cf8' }} />
                        <span className="font-medium" style={{ color: 'var(--foreground-secondary)' }}>{selectedEdge.label}</span>
                      </div>
                      {/* v2.3: 审核状态徽章 */}
                      {graphType === 'fp' && (selectedEdge.data as any)?.reviewStatus && (
                        <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${REVIEW_STATUS_STYLES[(selectedEdge.data as any).reviewStatus]?.bg} ${REVIEW_STATUS_STYLES[(selectedEdge.data as any).reviewStatus]?.text}`}>
                          {REVIEW_STATUS_STYLES[(selectedEdge.data as any).reviewStatus]?.label}
                        </span>
                      )}
                      <div className="w-px h-4" style={{ background: 'var(--border)' }} />
                      {/* v2.3: 审核按钮 — 仅 pending 状态显示 */}
                      {graphType === 'fp' && (selectedEdge.data as any)?.reviewStatus === 'pending' && (
                        <button onClick={() => openReviewModal(selectedEdge)}
                          className="flex items-center gap-1 text-xs transition-colors" style={{ color: 'var(--brand-500)' }}>
                          <CheckCircle className="w-3 h-3" /> 审核
                        </button>
                      )}
                      <button onClick={() => deleteEdge(selectedEdge.id)}
                        className="flex items-center gap-1 text-xs transition-colors" style={{ color: 'var(--state-error)' }}>
                        <Unlink className="w-3 h-3" /> 删除连线
                      </button>
                    </div>
                  </Panel>
                )}

                <Panel position="bottom-right">
                  <p className="text-[10px]" style={{ color: 'var(--foreground-quaternary)' }}>
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
        <div className="fixed z-[100] py-1.5 min-w-[150px]"
          style={{ left: ctxMenu.x, top: ctxMenu.y, background: 'var(--background)', borderRadius: 'calc(var(--radius) * 0.6)', boxShadow: 'var(--shadow-apple-xl)', border: '1px solid var(--border)' }}>
          {ctxMenu.nodeId && graphType === 'modules' && (
            <>
              <button onClick={contextEdit}
                className="w-full flex items-center gap-2 px-3 py-2 text-sm" style={{ color: 'var(--foreground-secondary)' }}>
                <Edit3 className="w-3.5 h-3.5" /> 切换模块
              </button>
              <div className="my-1" style={{ borderTop: '1px solid var(--border)' }} />
              <button onClick={contextRemove}
                className="w-full flex items-center gap-2 px-3 py-2 text-sm" style={{ color: 'var(--state-error)' }}>
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
                      className="w-full flex items-center gap-2 px-3 py-2 text-sm" style={{ color: 'var(--brand-500)' }}>
                      <CheckCircle className="w-3.5 h-3.5" /> 审核依赖
                    </button>
                    <div className="my-1" style={{ borderTop: '1px solid var(--border)' }} />
                  </>
                )
              }
              return null
            })()}
            <button onClick={swapEdge}
              className="w-full flex items-center gap-2 px-3 py-2 text-sm" style={{ color: 'var(--brand-500)' }}>
              <ArrowLeftRight className="w-3.5 h-3.5" /> 交换方向
            </button>
            <div className="my-1" style={{ borderTop: '1px solid var(--border)' }} />
            <button onClick={contextRemove}
              className="w-full flex items-center gap-2 px-3 py-2 text-sm" style={{ color: 'var(--state-error)' }}>
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

      {/* v2.45: FP 节点详情 Drawer — 双击 FP 节点打开 */}
      {fpDetailNode && (
        <FPDetailDrawer
          node={fpDetailNode}
          onClose={() => setFpDetailNode(null)}
          onShowImpact={(fpId, fpName, reqId) => {
            setFpDetailNode(null)
            setImpactModal({ fpId, fpName, reqId })
          }}
        />
      )}

      {/* v2.45: 版本管理 Drawer */}
      {versionDrawerOpen && selectedProductId && (
        <VersionManagementDrawer
          productId={selectedProductId}
          onClose={() => setVersionDrawerOpen(false)}
        />
      )}
    </div>
  )
}
