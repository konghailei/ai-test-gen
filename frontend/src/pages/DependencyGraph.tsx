import { useState, useEffect, useCallback, useRef } from 'react'
import {
  ReactFlow, useNodesState, useEdgesState, Background, Controls, MiniMap,
  MarkerType, ReactFlowProvider, Panel, Handle, Position,
  type Node, type Edge, type Connection, SelectionMode,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import {
  Search, Trash2, Edit3, GitBranch, Puzzle, Link2, GripVertical,
  Maximize, Minimize, Loader2, Unlink, X, ArrowLeftRight,
} from 'lucide-react'
import { dependencyGraph, products as prodApi } from '../api/client'
import { useProduct } from '../store/ProductContext'
import type { Module as ModuleType } from '../types'

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

const nodeTypes = { moduleNode: ModuleNode }

// ============================================================================
// Edge Styles
// ============================================================================
const EDGE_STYLES: Record<string, { label: string; stroke: string }> = {
  depends_on: { label: '依赖', stroke: '#818cf8' },
  interdependent: { label: '相互依赖', stroke: '#22d3ee' },
}

const EDGE_COMMON = { type: 'smoothstep' as const, animated: true, style: { strokeWidth: 2.5 } }

function createEdge(source: string, target: string, eType: string, srcName = source, tgtName = target): any {
  const s = EDGE_STYLES[eType] || EDGE_STYLES.depends_on
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

  const [allModules, setAllModules] = useState<ModuleType[]>([])
  const [edgeType, setEdgeType] = useState('depends_on')
  const [selectedEdge, setSelectedEdge] = useState<Edge | null>(null)
  const [ctxMenu, setCtxMenu] = useState<{ x: number; y: number; nodeId?: string; edgeId?: string } | null>(null)

  // Module picker state
  const [pickerOpen, setPickerOpen] = useState(false)
  const [pickerExcludes, setPickerExcludes] = useState<string[]>([])
  const [pickerTarget, setPickerTarget] = useState<'new' | { nodeId: string }>('new')

  // ========== Data Loading ==========
  useEffect(() => {
    if (selectedProductId) {
      prodApi.listModules(selectedProductId)
        .then(mods => setAllModules(mods || []))
        .catch(() => setAllModules([]))
      loadGraph()
    } else {
      setNodes([])
      setEdges([])
      setAllModules([])
    }
  }, [selectedProductId])

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

  // ========== Edge Operations ==========
  const onConnect = useCallback(async (conn: Connection) => {
    if (!conn.source || !conn.target || conn.source === conn.target) return
    try {
      await dependencyGraph.addEdge(conn.source, conn.target, edgeType)
      // 添加成功后刷新图谱，确保数据一致
      await loadGraph()
    } catch (e: any) {
      alert('添加依赖失败: ' + (e?.message || '未知错误'))
    }
  }, [edgeType])

  const deleteEdge = async (edgeId: string) => {
    const edge = edges.find(e => e.id === edgeId)
    if (!edge || !confirm('确认删除该连线？')) return
    try {
      await dependencyGraph.removeEdge(String(edge.source), String(edge.target))
      // 删除成功后刷新图谱
      await loadGraph()
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
    try { await dependencyGraph.updateNodePosition(node.id, node.position.x, node.position.y) } catch { }
  }, [])

  // Double-click opens picker to swap module
  const onNodeDoubleClick = useCallback((_: any, node: Node) => {
    setPickerTarget({ nodeId: node.id })
    setPickerExcludes(nodes.filter(n => n.id !== node.id).map(n => n.id))
    setPickerOpen(true)
  }, [nodes])

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
    if (!confirm(`从图谱中移除 ${selNodeIds.length} 个节点、${selEdgeIds.length} 条连线？（模块不会被删除）`)) return
    selNodeIds.forEach(id => removeNodeFromGraph(id))
    selEdgeIds.forEach(id => deleteEdge(id))
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
    setPickerExcludes(nodes.map(n => n.id))
    setPickerOpen(true)
  }

  // ========== Context menu actions ==========
  const contextEdit = () => {
    if (!ctxMenu?.nodeId) return
    const node = nodes.find(n => n.id === ctxMenu.nodeId)
    if (!node) return
    setPickerTarget({ nodeId: node.id })
    setPickerExcludes(nodes.filter(n => n.id !== node.id).map(n => n.id))
    setPickerOpen(true)
    setCtxMenu(null)
  }

  const swapEdge = async () => {
    if (!ctxMenu?.edgeId) return
    const edge = edges.find(e => e.id === ctxMenu.edgeId)
    if (!edge) return
    const srcName = String(nodes.find(n => n.id === edge.source)?.data?.label || edge.source)
    const tgtName = String(nodes.find(n => n.id === edge.target)?.data?.label || edge.target)
    if (!confirm(`将依赖方向从 "${srcName} 依赖 ${tgtName}" 改为 "${tgtName} 依赖 ${srcName}"？`)) return
    try {
      await dependencyGraph.removeEdge(String(edge.source), String(edge.target))
      await dependencyGraph.addEdge(String(edge.target), String(edge.source), (edge.data as any)?.eType || 'depends_on')
      await loadGraph()
    } catch (e: any) { alert('交换方向失败: ' + (e?.message || '未知错误')) }
  }

  const contextRemove = () => {
    if (ctxMenu?.nodeId) {
      if (!confirm('从图谱中移除该模块？（模块不会被删除）')) return
      removeNodeFromGraph(ctxMenu.nodeId)
    }
    if (ctxMenu?.edgeId) deleteEdge(ctxMenu.edgeId)
    setCtxMenu(null)
  }

  // ========== Helpers ==========
  const onGraphNodes = allModules.filter(m => !nodes.some(n => n.id === m.id))
  const productName = selectedProductId
    ? products.find(p => p.id === selectedProductId)?.name || selectedProductId
    : null

  // ==========================================================================
  // Render
  // ==========================================================================
  return (
    <div className={`flex flex-col ${fullscreen ? 'fixed inset-0 z-50 bg-white' : 'h-[calc(100vh-5rem)]'}`}>
      {/* Top Toolbar */}
      <div className="flex items-center gap-3 px-5 py-2.5 border-b border-slate-200 bg-white shrink-0">
        <GitBranch className="w-5 h-5 text-primary-500" />
        <h1 className="text-sm font-semibold text-slate-800">模块依赖图谱</h1>
        {productName && <span className="text-xs text-slate-400">· {productName}</span>}
        <div className="flex-1" />

        {/* Draggable toolbar items */}
        {selectedProductId && (
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

            {/* Edge type */}
            <span className="text-[11px] text-slate-400">连线类型:</span>
            <button onClick={() => setEdgeType('depends_on')}
              className={`px-2.5 py-1 rounded-lg text-xs font-medium border transition-colors ${edgeType === 'depends_on' ? 'bg-indigo-50 border-indigo-300 text-indigo-700' : 'border-slate-200 text-slate-500 hover:border-slate-300'}`}>
              → 单向依赖
            </button>
            <button onClick={() => setEdgeType('interdependent')}
              className={`px-2.5 py-1 rounded-lg text-xs font-medium border transition-colors ${edgeType === 'interdependent' ? 'bg-cyan-50 border-cyan-300 text-cyan-700' : 'border-slate-200 text-slate-500 hover:border-slate-300'}`}>
              ↔ 相互依赖
            </button>
          </>
        )}

        <div className="w-px h-5 bg-slate-200" />
        <button onClick={loadGraph} disabled={loading} className="btn-ghost text-xs">
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
                  <span>模块节点</span>
                  <span className="font-medium">{nodes.length}</span>
                </div>
                <div className="flex justify-between">
                  <span>依赖连线</span>
                  <span className="font-medium">{edges.length}</span>
                </div>
                <div className="flex justify-between">
                  <span>可用模块</span>
                  <span className="font-medium">{onGraphNodes.length}</span>
                </div>
              </div>

              <div className="mt-4 pt-3 border-t border-slate-200">
                <p className="text-[10px] text-slate-400 uppercase tracking-wider font-medium mb-2">图例</p>
                <div className="space-y-1.5">
                  <div className="flex items-center gap-2 text-[11px]">
                    <div className="w-5 h-0.5 rounded-full" style={{ background: EDGE_STYLES.depends_on.stroke }} />
                    <span className="text-slate-600">{EDGE_STYLES.depends_on.label}</span>
                  </div>
                  <div className="flex items-center gap-2 text-[11px]">
                    <div className="w-5 h-0.5 rounded-full" style={{ background: EDGE_STYLES.interdependent.stroke }} />
                    <span className="text-slate-600">{EDGE_STYLES.interdependent.label}</span>
                  </div>
                </div>
              </div>

              <div className="mt-auto pt-3 border-t border-slate-200">
                <p className="text-[10px] text-slate-400 leading-relaxed">
                  从工具栏拖拽模块到画布 · 拖拽节点手柄连线 · 双击节点切换模块
                </p>
              </div>
            </>
          )}
        </div>

        {/* Graph Canvas */}
        <div className="flex-1 relative bg-slate-50"
          onDragOver={(e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'move' }}
          onDrop={(e) => {
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
                onInit={(inst) => { rfInstance.current = inst }}
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
                connectionLineStyle={{ stroke: EDGE_STYLES[edgeType]?.stroke || '#818cf8', strokeWidth: 2 }}
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
                      <p className="text-xs text-slate-400">从工具栏拖拽「模块」到画布添加，拖拽节点间的手柄创建连线</p>
                    </div>
                  </Panel>
                )}

                {selectedEdge && (
                  <Panel position="bottom-center">
                    <div className="bg-white/95 backdrop-blur px-4 py-2.5 rounded-2xl shadow-lg border border-slate-200 flex items-center gap-3">
                      <div className="flex items-center gap-1.5 text-xs">
                        <span className="w-2 h-2 rounded-full"
                          style={{ background: EDGE_STYLES[(selectedEdge.data as any)?.eType === 'interdependent' ? 'interdependent' : 'depends_on']?.stroke }} />
                        <span className="text-slate-600 font-medium">{selectedEdge.label}</span>
                      </div>
                      <div className="w-px h-4 bg-slate-200" />
                      <button onClick={() => deleteEdge(selectedEdge.id)}
                        className="flex items-center gap-1 text-xs text-red-500 hover:text-red-700 transition-colors">
                        <Unlink className="w-3 h-3" /> 删除连线
                      </button>
                    </div>
                  </Panel>
                )}

                <Panel position="bottom-right">
                  <p className="text-[10px] text-slate-300">Shift+框选 · Del 移除 · 右键菜单</p>
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
          {ctxMenu.nodeId && (
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

      {/* Module Picker Modal */}
      {pickerOpen && (
        <ModulePicker
          modules={allModules}
          excludeIds={pickerExcludes}
          onPick={handlePickModule}
          onClose={() => setPickerOpen(false)}
        />
      )}
    </div>
  )
}
