/**
 * 依赖图谱布局与浮动边工具集（v2.43）
 *
 * 设计参考：
 * - ReactFlow 官方 Floating Edges 示例：https://reactflow.dev/learn/advanced-use/floating-edges
 * - Dagre 层次布局算法（专业级，含边交叉最小化、节点对齐）
 *
 * 包含两部分：
 *   1. 浮动边参数计算：根据两节点相对位置自动选择最佳起止 Handle（上下左右），
 *      避免「被依赖项只能从左边连入」的固定限制。
 *   2. Dagre 层次布局：专业的 Sugiyama 算法实现，将依赖图分层排列，
 *      自动最小化边交叉，呈现清晰的有向层级结构。
 */

import dagre from 'dagre'
import type { Edge, Node } from '@xyflow/react'
import { Position } from '@xyflow/react'

// ============================================================================
// 浮动边工具：根据节点位置自动计算最佳起止点和方向
// ============================================================================

type NodeData = Record<string, unknown>
type GetNode = (id: string) => Node<NodeData> | undefined

/**
 * 运行时访问 Node 的 positionAbsolute 字段（ReactFlow v12 运行时存在，但类型定义中为可选内部字段）。
 */
function getAbsolutePos(node: Node<NodeData>): { x: number; y: number } {
  const anyNode = node as unknown as { positionAbsolute?: { x: number; y: number }; position: { x: number; y: number } }
  return anyNode.positionAbsolute ?? anyNode.position
}

/**
 * 计算从源节点中心到目标节点中心的连线与节点边界的交点。
 *
 * 算法：从源节点中心向目标节点中心发射射线，计算射线与源节点矩形边界的交点。
 */
function getNodeIntersection(
  source: Node<NodeData>,
  target: Node<NodeData>,
): { x: number; y: number } {
  // source 的中心点（使用 absolute position，已包含父节点偏移）
  const sourceAbs = getAbsolutePos(source)
  const sourcePosW = sourceAbs.x
  const sourcePosH = sourceAbs.y
  const sourceWidth = source.width ?? source.measured?.width ?? 180
  const sourceHeight = source.height ?? source.measured?.height ?? 80
  const sourceCenterX = sourcePosW + sourceWidth / 2
  const sourceCenterY = sourcePosH + sourceHeight / 2

  // target 的中心点
  const targetAbs = getAbsolutePos(target)
  const targetPosW = targetAbs.x
  const targetPosH = targetAbs.y
  const targetWidth = target.width ?? target.measured?.width ?? 180
  const targetHeight = target.height ?? target.measured?.height ?? 80
  const targetCenterX = targetPosW + targetWidth / 2
  const targetCenterY = targetPosH + targetHeight / 2

  // 从 source 中心到 target 中心的向量
  const w = targetCenterX - sourceCenterX
  const h = targetCenterY - sourceCenterY

  // source 节点半宽半高
  const halfW = sourceWidth / 2
  const halfH = sourceHeight / 2
  if (halfW === 0 || halfH === 0) {
    return { x: sourceCenterX, y: sourceCenterY }
  }

  // 防止除零
  const absW = Math.abs(w) || 0.0001
  const absH = Math.abs(h) || 0.0001

  // 计算 x、y 方向的归一化比例
  const sx = absW / halfW
  const sy = absH / halfH

  // 取较小的比例保证交点在节点边界上
  const ratio = 1 / Math.max(sx, sy)
  const dx = w * ratio
  const dy = h * ratio

  return { x: sourceCenterX + dx, y: sourceCenterY + dy }
}

/**
 * 根据两节点的相对位置判断源节点的最佳 Handle 位置（上下左右）。
 * 使用标准化坐标比例判断主要方向，避免节点宽高不同导致的误判。
 */
function getEdgePosition(
  source: Node<NodeData>,
  target: Node<NodeData>,
): Position {
  const sW = source.width ?? source.measured?.width ?? 180
  const sH = source.height ?? source.measured?.height ?? 80
  const sAbs = getAbsolutePos(source)
  const sCx = sAbs.x + sW / 2
  const sCy = sAbs.y + sH / 2

  const tW = target.width ?? target.measured?.width ?? 180
  const tH = target.height ?? target.measured?.height ?? 80
  const tAbs = getAbsolutePos(target)
  const tCx = tAbs.x + tW / 2
  const tCy = tAbs.y + tH / 2

  const dx = tCx - sCx
  const dy = tCy - sCy

  // 标准化坐标比例（消除节点宽高差异的影响）
  const normDx = Math.abs(dx) / Math.max(sW, 1)
  const normDy = Math.abs(dy) / Math.max(sH, 1)

  if (normDx > normDy) {
    // 主要方向为水平
    return dx > 0 ? Position.Right : Position.Left
  } else {
    // 主要方向为垂直
    return dy > 0 ? Position.Bottom : Position.Top
  }
}

/**
 * 计算浮动边的完整参数：源点、目标点、源 Handle 位置、目标 Handle 位置。
 * FloatingEdge 组件使用此函数动态计算每条边的渲染路径。
 */
export function getEdgeParams(
  source: Node<NodeData>,
  target: Node<NodeData>,
): {
  sx: number
  sy: number
  tx: number
  ty: number
  sourcePosition: Position
  targetPosition: Position
} {
  const sourceIntersection = getNodeIntersection(source, target)
  const targetIntersection = getNodeIntersection(target, source)
  const sourcePosition = getEdgePosition(source, target)
  const targetPosition = getEdgePosition(target, source)

  return {
    sx: sourceIntersection.x,
    sy: sourceIntersection.y,
    tx: targetIntersection.x,
    ty: targetIntersection.y,
    sourcePosition,
    targetPosition,
  }
}

// ============================================================================
// Dagre 层次布局
// ============================================================================

export type LayoutDirection = 'TB' | 'LR'

interface LayoutOptions {
  /** 布局方向：TB=自上而下，LR=从左到右 */
  direction?: LayoutDirection
  /** 同层节点间距（dagre nodesep） */
  nodeSep?: number
  /** 层与层之间的间距（dagre ranksep） */
  rankSep?: number
  /** 起始坐标 X（dagre 会对齐到此点） */
  startX?: number
  /** 起始坐标 Y */
  startY?: number
}

/** 默认节点尺寸（用于 dagre 布局，避免节点未渲染时无尺寸） */
const DEFAULT_NODE_WIDTH = 200
const DEFAULT_NODE_HEIGHT = 90

/**
 * 使用 dagre 对节点列表应用层次布局。
 *
 * Dagre 算法步骤（Sugiyama 风格完整实现）：
 *   1. 拓扑分层（rank assignment）
 *   2. 同层节点排序（ordering，最小化边交叉）
 *   3. 坐标分配（coordinate assignment）
 *   4. 节点对齐
 *
 * @param nodes 原始节点列表（position 字段会被覆盖）
 * @param edges 边列表
 * @param options 布局选项
 * @returns 新的节点列表（带布局后的 position）
 */
export function layoutHierarchical(
  nodes: Node[],
  edges: Edge[],
  options: LayoutOptions = {},
): Node[] {
  const {
    direction = 'TB',
    nodeSep = 60,
    rankSep = 100,
    startX = 0,
    startY = 0,
  } = options

  if (nodes.length === 0) return nodes

  // 创建 dagre 图实例
  const g = new dagre.graphlib.Graph()
  g.setGraph({
    rankdir: direction,           // 'TB' | 'LR' | 'BT' | 'RL'
    nodesep: nodeSep,             // 同层节点间距
    ranksep: rankSep,             // 层间距
    marginx: startX,              // 左边距
    marginy: startY,              // 上边距
    ranker: 'network-simplex',    // 网络单纯形算法（最优布局）
  })
  g.setDefaultEdgeLabel(() => ({}))

  // 添加节点（使用实际或默认尺寸）
  nodes.forEach(node => {
    const width = node.width ?? node.measured?.width ?? DEFAULT_NODE_WIDTH
    const height = node.height ?? node.measured?.height ?? DEFAULT_NODE_HEIGHT
    g.setNode(node.id, { width, height })
  })

  // 添加边
  edges.forEach(edge => {
    g.setEdge(edge.source, edge.target)
  })

  // 执行布局
  dagre.layout(g)

  // 将 dagre 计算的坐标写回节点（dagre 的坐标是节点中心，ReactFlow 的 position 是左上角）
  return nodes.map(node => {
    const dagreNode = g.node(node.id)
    if (!dagreNode) return { ...node }
    const width = node.width ?? node.measured?.width ?? DEFAULT_NODE_WIDTH
    const height = node.height ?? node.measured?.height ?? DEFAULT_NODE_HEIGHT
    return {
      ...node,
      position: {
        x: dagreNode.x - width / 2,
        y: dagreNode.y - height / 2,
      },
    }
  })
}
