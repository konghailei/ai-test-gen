import React, { useEffect, useRef, useState, useCallback } from 'react'
import { useSearchParams, Link } from 'react-router-dom'
import MindMap from 'simple-mind-map'
import Drag from 'simple-mind-map/src/plugins/Drag.js'
import Select from 'simple-mind-map/src/plugins/Select.js'
import { ArrowLeft, Save, Loader2, RefreshCw, FileText, Layers, ChevronsDownUp, ChevronsUpDown, Pencil, Undo2, Redo2, Maximize2, Minimize2 } from 'lucide-react'
import { testCases as tcApi, requirements as reqApi, products as prodApi } from '../api/client'
import { useProduct } from '../store/ProductContext'
import CaseEditDialog from '../components/CaseEditDialog'
import CaseNoteDialog from '../components/CaseNoteDialog'
import { notifyCasesChanged } from '../utils/caseSync'
import {
  buildCaseNote, parseCaseNote, formatNoteErrors,
  labelType, type ParsedNote,
} from '../utils/caseNote'
import { caseTypeGroup, caseTypeGroupLabel, caseTypeGroupRepresentative } from '../utils/caseType'
import type { TestCase, Requirement, Project } from '../types'

/**
 * 真实 XMind 网页编辑页（基于 simple-mind-map / 思绪思维导图）
 * 将当前选中项目的测试用例渲染成可编辑思维导图。
 * 节点层级：项目(根) → 需求 → 功能点/业务场景 → 测试类型分组 → 用例
 * （v3.5.11 类型层按展示分组聚合，见 utils/caseType.ts 镜像 docs/04 类型体系）
 * 节点 data 内记录用例元数据，编辑后保存回系统用例。
 *
 * 进入方式：测试用例页点「打开 XMind」→ 新标签页打开本页面。
 * 路由：/testcases/xmind-edit?project_id=xxx&product_id=yyy
 */

// 常量：无 functional_point 的用例归入此节点
const BIZ_SCENE = '业务场景'

// 用例类型中文名、备注序列化统一来自 utils/caseNote（与后端 XMind 导出同一套标签）

interface XNodeData {
  text?: string
  kind?: 'case' | 'type' | 'fp' | 'req' | 'root'
  caseId?: string
  reqId?: string
  fp?: string
  type?: string
  /** v3.5.11: 类型层分组节点的分组 key（functional/scenario/…） */
  group?: string
  note?: string
}

// 编辑弹窗状态：目标节点引用 + 用例数据（id 为空 = 新建模式）
interface EditState {
  node: any
  caseData: TestCase
}

// 备注编辑弹窗状态：目标节点 + 用例数据 + 当前备注文本
interface NoteState {
  node: any
  caseData: TestCase
  text: string
}

// 注册插件：节点拖拽 + 多选
MindMap.usePlugin(Drag)
MindMap.usePlugin(Select)

/**
 * 草稿指纹（v3.5.12）：只取"用户可见语义"字段做结构化投影再序列化。
 * 不能直接 JSON 比对整棵树 —— 库内部 uid 每次渲染都会重生成，
 * 同一份内容在两次会话里的原始 JSON 必然不同，会把"无改动"误判成"有草稿"。
 */
function draftFingerprint(tree: any): string {
  const norm = (n: any): any => ({
    text: n?.data?.text ?? '',
    kind: n?.data?.kind ?? '',
    caseId: n?.data?.caseId ?? '',
    type: n?.data?.type ?? '',
    group: n?.data?.group ?? '',
    note: String(n?.data?.note ?? '').trim(),
    children: (n?.children || []).map(norm),
  })
  try { return JSON.stringify(norm(tree)) } catch { return '' }
}

export default function XMindEditPage() {
  const { selectedProductId } = useProduct()
  const [searchParams] = useSearchParams()
  const projectId = searchParams.get('project_id') || ''
  const productId = searchParams.get('product_id') || selectedProductId || ''

  const containerRef = useRef<HTMLDivElement>(null)
  const meRef = useRef<any>(null)
  // v3.5.12 全屏编辑：整页进全屏（保留页头保存/撤销/重做，只把画布撑满）
  const pageRef = useRef<HTMLDivElement>(null)
  const [isFullscreen, setIsFullscreen] = useState(false)
  // 撤销/重做可用性（由库 back_forward 事件驱动，不能靠猜）
  const [histState, setHistState] = useState({ canBack: false, canForward: false })
  // 本地草稿最近落盘时间（仅提示"草稿存在"，不代表已写入系统）
  const [draftAt, setDraftAt] = useState(0)
  // 最新用例数据（事件回调/保存时读取，避免闭包过期）
  const casesRef = useRef<TestCase[]>([])
  // 数据库基线（最近一次从库中加载/经弹窗确认写入的状态）。
  // 备注编辑只更新 casesRef（工作区展示用），handleSave 必须以 dbCasesRef 为
  // diff 基线——否则备注反解字段合并进 casesRef 后，差异比对恒为"无变更"，
  // 备注修改永远不会写回数据库（B-1）
  const dbCasesRef = useRef<TestCase[]>([])
  // 树重建版本号：仅在数据加载完成或结构性变化时递增，
  // 弹窗编辑单条用例只 setData 局部更新，不整树重建（防止丢失未保存的结构修改）
  const [treeVersion, setTreeVersion] = useState(0)

  const [cases, setCases] = useState<TestCase[]>([])
  const [reqs, setReqs] = useState<Requirement[]>([])
  const [projects, setProjects] = useState<Project[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [saveMsg, setSaveMsg] = useState('')
  const [saveFail, setSaveFail] = useState(false)
  const [err, setErr] = useState('')
  // 自定义右键菜单（menuNodeRef 记录右键目标节点实例）
  const menuNodeRef = useRef<any>(null)
  const [menu, setMenu] = useState<{ x: number; y: number; isRoot: boolean; isCase: boolean } | null>(null)
  // 用例详情编辑弹窗
  const [editState, setEditState] = useState<EditState | null>(null)
  // 备注文本编辑弹窗（改备注 = 改用例，保存时按格式反解回用例字段）
  const [noteState, setNoteState] = useState<NoteState | null>(null)
  // 备注弹窗保存后的解析结果：node → 解析出的用例字段（新建节点无 id，只能以节点为 key）
  const pendingNotesRef = useRef<Map<any, ParsedNote>>(new Map())

  const selectedProject = projects.find(p => p.id === projectId)

  // 加载数据
  const load = useCallback(async () => {
    setLoading(true)
    try {
      const pid = productId || undefined
      // 项目
      if (pid) {
        const pjs = await prodApi.listProjects(pid)
        setProjects(Array.isArray(pjs) ? pjs : [])
      }
      // 项目下需求（循环分页拉全；B-7：接口 page_size 上限 100，传 300 会 422）
      let reqsData: Requirement[] = []
      try {
        let rp = 1
        while (rp <= 10) {
          const r = await reqApi.list(rp, 100, pid, projectId || undefined)
          const items = r.items || []
          reqsData.push(...items)
          if (items.length < 100 || reqsData.length >= (r.total || 0)) break
          rp++
        }
      } catch { reqsData = [] }
      setReqs(reqsData)
      // 项目下用例（循环分页拉全）
      const all: TestCase[] = []
      let p = 1
      while (p <= 50) {
        const r = await tcApi.list(undefined, projectId || undefined, undefined, p, 100, pid)
        const items = r.items || []
        all.push(...items)
        if (items.length < 100 || all.length >= (r.total || 0)) break
        p++
      }
      setCases(all)
      casesRef.current = all
      dbCasesRef.current = all
      // 树重建后节点实例全部失效，丢弃未提交的备注草稿（刷新 = 放弃未保存修改）
      pendingNotesRef.current.clear()
      // 数据加载完成 → 重建树
      setTreeVersion(v => v + 1)
    } catch (e: any) { setErr(String(e?.message || e)) }
    finally { setLoading(false) }
  }, [projectId, productId])

  // 构建 simple-mind-map 数据：{data: {text, ...元数据}, children: []}
  const buildMindData = useCallback(() => {
    const rootTitle = selectedProject?.name || '测试用例'
    const root: any = {
      data: { text: rootTitle, kind: 'root' },
      children: [],
    }
    // 需求 → 功能点 → 类型 → 用例
    const reqMap = new Map<string, { reqId: string; title: string; nodes: Map<string, any> }>()
    for (const c of cases) {
      const reqId = c.req_id || '未分类'
      if (!reqMap.has(reqId)) {
        reqMap.set(reqId, {
          reqId,
          title: reqs.find(r => r.id === reqId)?.title || c.req_title || reqId,
          nodes: new Map(),
        })
      }
      const entry = reqMap.get(reqId)!
      const fp = c.functional_point?.trim() ? c.functional_point : BIZ_SCENE
      if (!entry.nodes.has(fp)) {
        entry.nodes.set(fp, {
          data: { text: fp, kind: 'fp', reqId, fp },
          children: [],
        })
      }
      const fpNode = entry.nodes.get(fp)!
      const type = c.type || 'other'
      // v3.5.11（docs/04 类型体系定稿）：类型层按展示分组聚合（功能测试/场景测试/…），
      // 与后端 XMind 导出同构；细分 type 保留在用例节点 data.type 与备注里不丢
      const group = caseTypeGroup(c.type)
      let typeNode = fpNode.children.find((t: any) => t.data?.group === group && t.data?.kind === 'type')
      if (!typeNode) {
        typeNode = {
          data: { text: caseTypeGroupLabel(c.type), kind: 'type', reqId, fp, group },
          children: [],
        }
        fpNode.children.push(typeNode)
      }
      typeNode.children.push({
        data: { text: c.title, kind: 'case', caseId: c.id, reqId, fp, type, note: buildCaseNote(c) },
        children: [],
      })
    }
    // 需求节点
    for (const { reqId, title, nodes } of reqMap.values()) {
      root.children.push({
        data: { text: title, kind: 'req', reqId },
        children: Array.from(nodes.values()),
      })
    }
    return root
  }, [cases, reqs, selectedProject])

  // ==========================================================================
  // v3.5.12：全屏编辑 / 撤销重做 / 本地草稿（防意外退出丢改动）
  // ==========================================================================

  // ---- 本地草稿（localStorage，不写库；点「保存」才是正式写入系统）----
  const draftKeyRef = useRef('')
  draftKeyRef.current = `aitg-xmind-draft:${productId || 'p'}:${projectId || 'p'}`
  const draftTimerRef = useRef<number | null>(null)
  // "干净树指纹"：每次从库内加载/重建后的树指纹。写草稿前若当前树与它一致，
  // 说明用户没改动（或已改回原状、或刚保存完/刷新完），**不产草稿**。
  // 背景（v3.5.12 实证 bug）：addHistoryOnInit 默认 true → 每次 new MindMap 都会
  // emit data_change → scheduleDraft 把刚 clearDraft 的草稿"复活"，
  // 导致保存成功后"本地草稿"提示常驻、点刷新（=放弃）后草稿又回来。
  const baselineFpRef = useRef('')
  // 同一挂载周期内草稿恢复只提示一次：StrictMode 下 effect 会双跑
  // （mount → cleanup → mount），且 load() 后 treeVersion 变化也会重跑 effect，
  // 不拦会让用户连看两次"是否恢复草稿"
  const draftPromptedRef = useRef('')

  const readDraft = useCallback((): { savedAt: number; tree: any; fp: string } | null => {
    try {
      const raw = localStorage.getItem(draftKeyRef.current)
      if (!raw) return null
      const obj = JSON.parse(raw)
      return obj && obj.tree ? obj : null
    } catch { return null }
  }, [])

  const writeDraft = useCallback(() => {
    const me = meRef.current
    if (!me) return
    try {
      const tree = me.getData()
      if (!tree) return
      const fp = draftFingerprint(tree)
      // 与库内基线一致 → 不写草稿（堵住"复活"与"无意义草稿"）
      if (fp && baselineFpRef.current && fp === baselineFpRef.current) {
        setDraftAt(0)
        return
      }
      localStorage.setItem(draftKeyRef.current, JSON.stringify({
        savedAt: Date.now(), tree, fp,
      }))
      setDraftAt(Date.now())
    } catch { /* 配额超限等：静默降级，绝不打断编辑 */ }
  }, [])

  const clearDraft = useCallback(() => {
    try { localStorage.removeItem(draftKeyRef.current) } catch { /* noop */ }
    setDraftAt(0)
  }, [])

  // 编辑动作密集（拖拽/连击），草稿落盘做 1.2s 防抖
  const scheduleDraft = useCallback(() => {
    if (draftTimerRef.current) window.clearTimeout(draftTimerRef.current)
    draftTimerRef.current = window.setTimeout(() => {
      draftTimerRef.current = null
      writeDraft()
    }, 1200)
  }, [writeDraft])

  // ---- 撤销 / 重做 ----
  const refreshHist = useCallback(() => {
    const cmd = meRef.current?.command
    if (!cmd) return
    const i = typeof cmd.activeHistoryIndex === 'number' ? cmd.activeHistoryIndex : 0
    const len = Array.isArray(cmd.history) ? cmd.history.length : 0
    const next = { canBack: i > 0, canForward: i < len - 1 }
    // 相等则跳过 setState：data_change 在拖拽/打字时高频触发，无脑 set 会每帧重渲染顶栏
    setHistState(prev => (prev.canBack === next.canBack && prev.canForward === next.canForward) ? prev : next)
  }, [])

  const applyHistory = useCallback((dir: 'back' | 'forward') => {
    const me = meRef.current
    const cmd = me?.command
    if (!me || !cmd) return
    // 本 fork 的 back()/forward() 只移动历史指针并返回数据，**不重渲染**
    //（上游 Ctrl+Z 绑定在此 fork 已移除，全库无任何调用方），
    // 且期间必须 pause：否则后续 addHistory 会把回退结果再压入历史栈，撤销栈塌成单点。
    cmd.pause()
    try {
      const data = dir === 'back' ? cmd.back() : cmd.forward()
      if (!data) return
      me.renderer?.setData?.(data)
      me.render?.()
    } catch { /* noop */ } finally {
      cmd.recovery()
    }
    // 节点实例已被重建：按实例索引的备注草稿全部失效，必须清掉（防误提交到新节点）
    pendingNotesRef.current.clear()
    refreshHist()
    scheduleDraft()
  }, [refreshHist, scheduleDraft])

  // ---- 全屏 ----
  const toggleFullscreen = useCallback(() => {
    const el = pageRef.current
    if (!el) return
    const docAny = document as any
    if (docAny.fullscreenElement || docAny.webkitFullscreenElement) {
      const exit = docAny.exitFullscreen || docAny.webkitExitFullscreen
      exit?.call(document)
      return
    }
    const elAny = el as any
    const req = elAny.requestFullscreen || elAny.webkitRequestFullscreen
    // 库自带的 fullScreen() 用的是已废弃的 requestFullScreen 别名，现代浏览器会静默失效，故自己调标准 API
    if (typeof req !== 'function') { setIsFullscreen(true); return }
    try {
      const p = req.call(el)
      // 被拒（iframe 无 allow-fullscreen / 非用户手势）→ 退回 CSS 沉浸模式，功能不失效
      if (p && typeof p.catch === 'function') p.catch(() => setIsFullscreen(true))
    } catch { setIsFullscreen(true) }
  }, [])

  const exitFullscreen = useCallback(() => {
    const docAny = document as any
    if (docAny.fullscreenElement || docAny.webkitFullscreenElement) {
      const exit = docAny.exitFullscreen || docAny.webkitExitFullscreen
      exit?.call(document)
    } else {
      setIsFullscreen(false)
    }
  }, [])

  // 同步原生全屏状态：用户按 ESC / F11 退出时，按钮状态也要跟着回退
  useEffect(() => {
    const docAny = document as any
    const sync = () => setIsFullscreen(!!docAny.fullscreenElement || !!docAny.webkitFullscreenElement)
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      // 原生全屏：ESC 由浏览器处理（退出后 fullscreenchange 会同步状态），页面不抢
      if (docAny.fullscreenElement || docAny.webkitFullscreenElement) return
      // 焦点在编辑态（节点文本编辑 / 输入框）时，ESC 的语义是"结束编辑"而非退出全屏
      // ——否则沉浸模式改标题按一下 ESC 连全屏一起退，改一半被踢出沉浸态
      const ae = document.activeElement as any
      if (ae && (ae.isContentEditable || ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA')) return
      setIsFullscreen(false)
    }
    document.addEventListener('fullscreenchange', sync)
    document.addEventListener('webkitfullscreenchange', sync)
    window.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('fullscreenchange', sync)
      document.removeEventListener('webkitfullscreenchange', sync)
      window.removeEventListener('keydown', onKey)
    }
  }, [])

  // 全屏切换后画布重算尺寸（库不监听 window resize，容器高度变了必须显式调）
  useEffect(() => {
    if (!meRef.current) return
    const t = window.setTimeout(() => {
      // 容器宽高为 0 时库会抛错（getElRectInfo），吞掉即可：下一帧自然重试
      try { meRef.current?.resize?.() } catch { /* noop */ }
    }, 60)
    return () => window.clearTimeout(t)
  }, [isFullscreen])

  // 窗口拉大拉小跟随自适应（顺手修：原先画布完全不跟手）
  useEffect(() => {
    const onResize = () => { try { meRef.current?.resize?.() } catch { /* noop */ } }
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  // 初始化 simple-mind-map（仅 loading 完成 + treeVersion 变化时重建；
  // 弹窗编辑单条用例走 setData 局部更新，不触发重建）
  useEffect(() => {
    if (loading || treeVersion === 0) return
    if (!containerRef.current) return
    const data = buildMindData()
    if (meRef.current) {
      try { meRef.current.destroy() } catch { /* noop */ }
      meRef.current = null
    }
    const me = new MindMap({
      el: containerRef.current,
      data,
      // 逻辑结构图：根在左、子节点全部向右展开
      layout: 'logicalStructure',
      editable: true,
      // 新增节点默认名称
      defaultInsertSecondLevelNodeText: '新节点',
      defaultInsertBelowSecondLevelNodeText: '新节点',
      // 允许用户拖动节点整理结构
      enableDrag: true,
      // 备注 tooltip 层级降到弹窗（z-50）之下，避免覆盖编辑弹窗的按钮
      nodeNoteTooltipZIndex: 40,
      // 修复折叠徽标显示为 0 的问题：
      // 库内部徽标数字依赖 _lastExpandBtnType 缓存，仅在展开状态变化时重算，
      // 特定时序下（收起状态下子树结构变化、节点实例复用等）count 会被算成 0。
      // 此处强制校正：count 异常时从节点数据重新统计后代数。
      expandBtnNumHandler: (count: number, node: any) => {
        if (!count || count <= 0) {
          try {
            const children = node?.nodeData?.children || []
            if (children.length > 0) {
              const sum = (list: any[]): number =>
                list.reduce((t: number, c: any) => t + sum(c.children || []), list.length)
              return sum(children)
            }
          } catch { /* 保持原值 */ }
        }
        return undefined // undefined = 沿用库内部计算值
      },
    })
    meRef.current = me

    // 右键菜单：simple-mind-map 核心不带 UI，右键节点时已自动激活该节点
    const onNodeContextmenu = (e: MouseEvent, node: any) => {
      e.preventDefault?.()
      const d: XNodeData = node?.getData?.() || {}
      menuNodeRef.current = node
      setMenu({
        x: e.clientX,
        y: e.clientY,
        isRoot: !!node?.isRoot,
        // 已有用例节点（有 caseId）或用户新建的叶子节点（可走弹窗新建用例）
        isCase: (d.kind === 'case' && !!d.caseId) || (!d.kind && !(node?.nodeData?.children || []).length && !node?.isRoot),
      })
    }
    const onBlankContextmenu = (e: MouseEvent) => {
      e.preventDefault?.()
    }
    // 点击备注图标 → 打开用例详情编辑弹窗
    const onNoteClick = (node: any) => {
      openCaseEditRef.current?.(node)
    }
    // 拖拽改型同步（B-2 + v3.5.11 分组节点）：拖到别的类型分组 = 位置即意图，
    // 拖拽结束时把用例节点的细分 type 同步为所在分组代表细分；
    // 保存时优先读节点元数据，避免表单弹窗改的类型被分组推断回退
    let dragNode: any = null
    const onNodeMousedown = (node: any) => { dragNode = node }
    const onNodeDragend = () => {
      const n = dragNode
      dragNode = null
      if (!n) return
      try {
        const d: XNodeData = n.getData?.() || {}
        if (d.kind !== 'case' || !d.caseId) return
        let p = n.parent
        while (p) {
          const pd: XNodeData = p.getData?.() || {}
          if (pd.kind === 'type') {
            // 类型节点现按分组组织（data.group）；旧树/手建节点回退读 data.type
            const branchType = pd.group ? caseTypeGroupRepresentative(pd.group) : (pd.type || '')
            if (branchType !== (d.type || '')) n.setData?.({ type: branchType })
            return
          }
          if (pd.kind === 'fp' || pd.kind === 'req' || pd.kind === 'root') return
          p = p.parent
        }
      } catch { /* noop */ }
    }
    me.on('node_contextmenu', onNodeContextmenu)
    me.on('contextmenu', onBlankContextmenu)
    me.on('node_note_click', onNoteClick)
    me.on('node_mousedown', onNodeMousedown)
    me.on('node_dragend', onNodeDragend)

    // v3.5.12：编辑即落本地草稿（防意外退出）；撤销/重做可用性跟随历史指针
    const onDataChange = () => { scheduleDraft(); refreshHist() }
    me.on('data_change', onDataChange)
    me.on('back_forward', refreshHist)
    refreshHist()

    // 重建完成 → 记下"干净树指纹"（此后写草稿都以此为基线比对）
    try {
      const t0 = me.getData()
      baselineFpRef.current = t0 ? draftFingerprint(t0) : ''
    } catch { baselineFpRef.current = '' }

    // 居中显示
    try { me.view?.setViewCenter?.() } catch { /* noop */ }

    // v3.5.12 本地草稿恢复：与库内基线指纹一致 → 视为无改动，静默清理；
    // 不一致才问用户（草稿只在浏览器本机，未点「保存」从未写库）。
    // 注意：这里不能用 return 提前退出 effect——会跳过 cleanup 注册导致监听泄漏，
    // 所以整个提示块包在 if 里；且同周期只提示一次（StrictMode / treeVersion 重跑）
    const draft = readDraft()
    if (draft && draftPromptedRef.current !== draftKeyRef.current) {
      draftPromptedRef.current = draftKeyRef.current
      let same = false
      try { same = !!draft.fp && draft.fp === draftFingerprint(me.getData()) } catch { same = false }
      if (same) {
        clearDraft()
      } else {
        const when = new Date(draft.savedAt).toLocaleString()
        if (window.confirm(
          `检测到 ${when} 的本地草稿。\n\n` +
          `· 草稿只存在本机浏览器，尚未写入系统用例\n` +
          `· 点「保存」才会正式写库\n\n` +
          `恢复草稿继续编辑？取消 = 丢弃草稿，以系统数据为准。`
        )) {
          try {
            me.setData(draft.tree)
            setDraftAt(draft.savedAt)
            refreshHist()
            setSaveMsg(`已恢复 ${when} 的本地草稿，确认无误后点「保存」写入系统`)
          } catch { clearDraft() }
        } else {
          clearDraft()
        }
      }
    }

    return () => {
      me.off('node_contextmenu', onNodeContextmenu)
      me.off('contextmenu', onBlankContextmenu)
      me.off('node_note_click', onNoteClick)
      me.off('node_mousedown', onNodeMousedown)
      me.off('node_dragend', onNodeDragend)
      me.off('data_change', onDataChange)
      me.off('back_forward', refreshHist)
      if (meRef.current) { try { meRef.current.destroy() } catch { /* noop */ } ; meRef.current = null }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading, treeVersion])

  // 点击任意处关闭右键菜单
  useEffect(() => {
    if (!menu) return
    const close = () => setMenu(null)
    window.addEventListener('mousedown', close)
    return () => window.removeEventListener('mousedown', close)
  }, [menu])

  // 打开用例详情编辑弹窗
  // - 已有用例节点（caseId）：编辑模式，直接复用 CaseEditDialog（固定格式编辑步骤）
  // - 用户新建的叶子节点：向上找最近祖先的 reqId → 新建模式（保存后 API 创建用例）
  const openCaseEdit = useCallback((node: any) => {
    const d: XNodeData = node?.getData?.() || {}
    // 已有用例
    if (d.kind === 'case' && d.caseId) {
      const c = casesRef.current.find(x => x.id === d.caseId)
      if (c) {
        setEditState({ node, caseData: c })
        return
      }
    }
    // 新建叶子节点：向上找需求 ID / 功能点 / 类型分支（作为新建用例的默认归属）
    const hasChildren = (node?.nodeData?.children || []).length > 0
    if (d.caseId || node?.isRoot || hasChildren) return
    let reqId = ''
    let fp = ''
    let branchType = ''
    let p = node?.parent
    while (p) {
      const pd: XNodeData = p.getData?.() || {}
      if (!reqId && pd.reqId) reqId = pd.reqId
      if (!branchType && pd.kind === 'type') {
        branchType = pd.group ? caseTypeGroupRepresentative(pd.group) : (pd.type || '')
      }
      if (!fp && pd.kind === 'fp') fp = pd.fp || ''
      if (pd.kind === 'req' || pd.kind === 'root') break
      p = p.parent
    }
    // 兜底：项目下只有一个需求时直接使用
    if (!reqId && reqs.length === 1) reqId = reqs[0].id
    if (!reqId) {
      setErr('无法确定该节点所属需求，请将节点挂到需求分支下再编辑')
      return
    }
    setEditState({
      node,
      caseData: {
        id: '',
        req_id: reqId,
        product_id: productId,
        title: d.text || '',
        type: branchType || 'functional',
        priority: 'P1',
        status: 'draft',
        confidence: 0,
        // 节点所在功能点分支作为默认归属（"业务场景"分支 = 无功能点）
        functional_point: fp && fp !== BIZ_SCENE ? fp : '',
        description: '',
        preconditions: [],
        steps: [],
        created_at: '',
      },
    })
  }, [productId, reqs])
  // 供 MindMap 事件回调读取最新函数（避免闭包过期）
  const openCaseEditRef = useRef(openCaseEdit)
  useEffect(() => { openCaseEditRef.current = openCaseEdit }, [openCaseEdit])

  // 把用例节点移动到功能点下对应类型的分支（类型变化后保持树与数据一致）
  // v3.5.11：类型节点按分组组织 → 按 newType 所属分组定位目标分支；
  // 目标分组分支不存在时不强行创建：保存后整树重建会落到正确位置
  const moveNodeToType = useCallback((node: any, newType: string) => {
    const me = meRef.current
    if (!node || !me || !newType) return
    try {
      const typeNode = node.parent
      const fpNode = typeNode?.parent
      if (!fpNode) return
      const targetGroup = caseTypeGroup(newType)
      const target = fpNode.children?.find(
        (ch: any) => ch.getData?.()?.kind === 'type' &&
          (ch.getData?.()?.group || '') === targetGroup
      )
      if (target && target !== typeNode) me.execCommand('MOVE_NODE_TO', node, target)
    } catch { /* 移动失败不影响数据，保存后重建树即可 */ }
  }, [])

  // 打开备注编辑弹窗（改备注即改用例；新建节点也能先写备注）
  const openNoteEdit = useCallback((node: any) => {
    const d: XNodeData = node?.getData?.() || {}
    const existing = d.caseId ? casesRef.current.find(x => x.id === d.caseId) : undefined
    const caseData: TestCase = existing || {
      id: '', req_id: '', product_id: productId, title: d.text || '',
      type: 'functional', priority: 'P1', status: 'draft', confidence: 0,
      functional_point: '', description: '', preconditions: [], steps: [], created_at: '',
    }
    setNoteState({ node, caseData, text: (d.note || '').trim() ? d.note! : buildCaseNote(caseData) })
  }, [productId])

  // 用例详情弹窗保存成功回调：更新本地数据 + 局部刷新节点
  const handleCaseSaved = useCallback((updated: TestCase) => {
    const orig = dbCasesRef.current.find(c => c.id === updated.id)
    const typeChanged = !!orig && orig.type !== updated.type
    const isNew = !orig
    // 表单弹窗已直接写库 → 工作区（casesRef）与数据库基线（dbCasesRef）同步更新，
    // 保证 handleSave 的 diff 仍以库中真实状态为基线（B-1）
    const merge = (list: TestCase[]) =>
      isNew ? [...list, updated] : list.map(c => (c.id === updated.id ? { ...c, ...updated } : c))
    casesRef.current = merge(casesRef.current)
    dbCasesRef.current = merge(dbCasesRef.current)
    setCases(casesRef.current)
    const node = editState?.node
    setEditState(null)
    setErr('')
    // 表单保存已直接落库并重建节点备注 → 该节点未提交的备注草稿作废，
    // 避免保存时旧草稿覆盖表单刚写入的新值（B-5）
    if (node) pendingNotesRef.current.delete(node)
    // 新建用例的功能点归属已随 create 请求直接写入（B-6：后端 create 已支持
    // functional_point 字段，避免用户不点页面「保存」时用例丢失 FP 归属）
    // 局部更新节点（普通编辑 & 新建均适用；不整树重建，保留未保存的结构修改）
    try {
      // 元数据仅更新数据层（SET_NODE_DATA 不触发渲染）
      node?.setData?.({ caseId: updated.id, kind: 'case', type: updated.type })
      // 标题/备注走专用命令（SET_NODE_TEXT/SET_NODE_NOTE 会重新渲染节点，
      // 同步重建备注 tooltip 内容，修复保存后 tooltip 显示旧值的问题）
      node?.setText?.(updated.title)
      node?.setNote?.(buildCaseNote(updated))
    } catch { /* noop */ }
    // 类型变化：将节点移动到功能点下对应类型的分支（保留未保存的结构修改）
    if (typeChanged && node) moveNodeToType(node, updated.type)
    setSaveMsg(`用例「${updated.title}」已保存`)
  }, [editState, moveNodeToType])

  // 备注编辑弹窗保存：写回节点备注 + 记录解析结果（点页面「保存」后写入系统）
  const handleNoteSaved = useCallback((text: string, parsed: ParsedNote) => {
    const st = noteState
    if (!st) return
    try { st.node?.setNote?.(text) } catch { /* noop */ }
    // 以节点为 key 记录（新建用例尚无 id）
    pendingNotesRef.current.set(st.node, parsed)
    // 备注里改了类型 → 同步移动节点分支
    if (parsed.type && st.caseData.id && parsed.type !== st.caseData.type) {
      moveNodeToType(st.node, parsed.type)
    }
    // 本地数据同步（仅工作区 casesRef，供再次打开弹窗/详情时显示最新内容）：
    // 刻意不更新 dbCasesRef——备注尚未写库，保存时以 dbCasesRef 为 diff 基线才能检出变更
    if (st.caseData.id) {
      const merged = { ...st.caseData, ...parsed } as TestCase
      casesRef.current = casesRef.current.map(c => (c.id === merged.id ? merged : c))
      setCases(casesRef.current)
    }
    setNoteState(null)
    setErr('')
    setSaveMsg(`用例「${st.caseData.title || '新建用例'}」备注已更新，点页面「保存」写入系统`)
  }, [noteState, moveNodeToType])

  // 右键菜单操作（命令作用于当前激活节点，右键时已自动激活）
  const execMenu = useCallback((cmd: string, ...args: any[]) => {
    const me = meRef.current
    if (!me) return
    try { me.execCommand(cmd, ...args) } catch { /* noop */ }
    setMenu(null)
  }, [])

  // 保存：将思维导图当前状态回写系统用例
  const handleSave = useCallback(async () => {
    const me = meRef.current
    if (!me) return
    setSaving(true); setSaveMsg('保存中...'); setErr('')
    try {
      const tree = me.getData()
      // 收集当前树中的所有用例节点（叶子）+ 其所在需求/功能点/类型
      type Row = { node: any; reqId: string; fp: string; type: string }
      const rows: Row[] = []
      const walk = (node: any, depth: number, reqId: string, fp: string, type: string) => {
        const d: XNodeData = node.data || {}
        const children = node.children || []
        // 未知 kind 的节点为用户新建：按深度推断层级（1=需求 2=功能点 3=类型）
        let kind = d.kind
        if (!kind) {
          if (children.length > 0 && depth <= 3) {
            kind = depth === 1 ? 'req' : depth === 2 ? 'fp' : 'type'
          } else if (children.length === 0 && depth >= 4) {
            kind = 'case'
          }
        }
        if (kind === 'req') reqId = d.reqId || reqId
        if (kind === 'fp') { fp = d.fp || node.data?.text || BIZ_SCENE; type = '' }
        // v3.5.11：类型层为分组节点（data.group）→ 新建用例兜底取分组代表细分
        // （功能测试→functional / 场景测试→integration / …）；旧树/用户手建节点
        // 仍按 data.type 或节点文本反查（labelType）兼容
        if (kind === 'type') {
          type = d.type
            || (d.group ? caseTypeGroupRepresentative(d.group) : '')
            || labelType(node.data?.text || '')
        }
        if (kind === 'case' || (!kind && children.length === 0)) {
          // kind='case' 的节点即使被用户加了子节点也必须计入用例行（B-3）：
          // 否则它不进 seenIds，保存时会被当作"已从树中删除"而误删该用例
          rows.push({ node, reqId, fp, type })
        }
        children.forEach((c: any) => walk(c, depth + 1, reqId, fp, type))
      }
      if (tree) walk(tree, 0, '', '', '')
      // 对照数据库基线：用例 id → 库中状态（B-1：备注编辑只进工作区 casesRef，
      // diff 必须以 dbCasesRef 为准，否则备注修改会被判为"无变更"而永不落库）
      const origById = new Map(dbCasesRef.current.map(c => [c.id, c]))
      // 变更集
      const addOps: any[] = []
      const updateOps: Array<{ id: string; data: Record<string, unknown> }> = []
      const deleteIds: string[] = []
      // 当前树中用例 id 集合
      const seenIds = new Set<string>()
      // 备注解析结果：caseId → 从备注反解出的用例字段
      const notePatch = new Map<string, ParsedNote>()
      // 备注格式错误（逐条带行号，阻止保存）
      const noteErrMsgs: string[] = []
      for (const r of rows) {
        const d = r.node.data || {}
        const caseId = d.caseId
        const pending = pendingNotesRef.current.get(r.node)
        if (!caseId) {
          // 无 caseId → 新增用例（需挂在实际需求下）；备注里写好的字段一并提交
          if (r.reqId) {
            addOps.push({
              req_id: r.reqId,
              product_id: productId,
              title: d.text || '',
              type: pending?.type || r.type || 'functional',
              functional_point: r.fp === BIZ_SCENE ? '' : r.fp,
              ...(pending?.priority ? { priority: pending.priority } : {}),
              ...(pending?.description !== undefined ? { description: pending.description } : {}),
              ...(pending?.preconditions ? { preconditions: pending.preconditions } : {}),
              ...(pending?.steps ? { steps: pending.steps } : {}),
            })
          }
          continue
        }
        seenIds.add(caseId)
        const orig = origById.get(caseId)
        if (!orig) { continue }

        // 备注解析：弹窗里改过的用缓存结果，否则与基准文本比对，不同才解析
        let parsed = pending
        if (!parsed) {
          const note = (d.note || '').trim()
          if (note && note !== buildCaseNote(orig).trim()) {
            const res = parseCaseNote(note)
            if (!res.ok) {
              noteErrMsgs.push(
                `· 用例「${orig.title}」(${caseId})\n    ${formatNoteErrors(res.errors).join('\n    ')}`
              )
              continue
            }
            parsed = res.data
          }
        }
        if (parsed) notePatch.set(caseId, parsed)

        // 收集需要更新的字段
        const patch: Record<string, unknown> = {}
        if ((d.text || '') !== orig.title) patch.title = d.text || ''
        const newFp = r.fp === BIZ_SCENE ? '' : r.fp
        if (newFp !== (orig.functional_point || '')) patch.functional_point = newFp
        if ((r.reqId || '') !== orig.req_id) patch.req_id = r.reqId
        // 类型（B-2）：优先节点元数据 d.type（表单编辑保存 / 拖拽结束同步时写入），
        // 其次按所在类型分支推断。否则表单改了类型但目标分支不存在时，
        // 节点留在旧分支，分支推断会把类型改回旧值，覆盖用户刚保存的修改
        const effType = (d.type as string) || r.type || 'other'
        if (effType !== (orig.type || 'other')) patch.type = effType
        // 备注里显式修改的字段优先于树结构推断（用户主动编辑 > 位置推断）
        if (parsed) {
          if (parsed.type && parsed.type !== (orig.type || 'other')) patch.type = parsed.type
          if (parsed.priority && parsed.priority !== orig.priority) patch.priority = parsed.priority
          if (parsed.description !== undefined && parsed.description !== (orig.description || '')) {
            patch.description = parsed.description
          }
          // 内容未变时不提交，避免备注格式化后产生无意义的更新
          if (parsed.preconditions &&
              JSON.stringify(parsed.preconditions) !== JSON.stringify(orig.preconditions || [])) {
            patch.preconditions = parsed.preconditions
          }
          if (parsed.steps && JSON.stringify(parsed.steps) !== JSON.stringify(orig.steps || [])) {
            patch.steps = parsed.steps
          }
        }
        if (Object.keys(patch).length > 0) updateOps.push({ id: caseId, data: patch })
      }
      // 删除：数据库基线中已有、但不在当前树的用例
      for (const c of dbCasesRef.current) {
        if (!seenIds.has(c.id)) deleteIds.push(c.id)
      }

      // v3.2 备注格式校验不通过：阻止保存并逐条提示位置与原因
      if (noteErrMsgs.length > 0) {
        setErr(`以下用例的备注格式有误，已阻止保存：\n${noteErrMsgs.join('\n')}`)
        setSaveMsg('')
        setSaving(false)
        return
      }

      // v3.2 安全网：导图里一个用例节点都没有，但库里明明有用例
      // —— 说明是误删了根/需求分支或渲染异常，直接保存会清空全部用例，必须拦下
      if (rows.length === 0 && casesRef.current.length > 0) {
        setErr(
          `检测到导图中没有任何用例节点，保存会删除全部 ${casesRef.current.length} 条用例。` +
          `已阻止本次保存，请刷新页面后重试。`
        )
        setSaveMsg('')
        setSaving(false)
        return
      }

      const totalOps = addOps.length + updateOps.length + deleteIds.length
      if (totalOps === 0) {
        setSaveMsg('无明显变更')
        setSaving(false)
        return
      }

      // v3.2 二次确认：删除不可逆（删掉一个需求分支 = 删掉其下全部用例），必须让用户看清影响面
      const summary = `新增 ${addOps.length} 条，更新 ${updateOps.length} 条，删除 ${deleteIds.length} 条`
      let detail = ''
      if (deleteIds.length > 0) {
        const doomed = casesRef.current.filter(c => deleteIds.includes(c.id))
        const preview = doomed.slice(0, 10).map(c => `· ${c.title || c.id}`).join('\n')
        detail =
          `\n\n将被删除（共 ${deleteIds.length} 条，前 10 条）：\n${preview}` +
          (doomed.length > 10 ? `\n… 其余 ${doomed.length - 10} 条略` : '') +
          '\n\n⚠️ 删除不可恢复，确认继续？'
      } else {
        detail = '\n\n确认执行？'
      }
      if (!window.confirm(`即将把导图结构写回系统用例：\n\n${summary}${detail}`)) {
        setSaveMsg('已取消保存')
        setSaveFail(false)
        setSaving(false)
        return
      }

      // 执行
      let addCount = 0, updateCount = 0, deleteCount = 0, failCount = 0
      for (const op of addOps) {
        try { await tcApi.create(op as any); addCount++ } catch { failCount++ }
      }
      for (const op of updateOps) {
        try { await tcApi.update(op.id, op.data); updateCount++ } catch { failCount++ }
      }
      for (const id of deleteIds) {
        try { await tcApi.delete(id); deleteCount++ } catch { failCount++ }
      }
      // P2: 部分失败时红色醒目提示，不能只靠绿色提示条里的"失败 N"文字
      setSaveFail(failCount > 0)
      setSaveMsg(
        failCount > 0
          ? `⚠ 保存部分失败：新增 ${addCount}，更新 ${updateCount}，删除 ${deleteCount}，失败 ${failCount} 条（可能是权限或网络问题），导图已按数据库实际状态刷新，请检查后重试`
          : `保存完成：新增 ${addCount}，更新 ${updateCount}，删除 ${deleteCount}`
      )
      // 备注解析结果已入库，清空缓存（避免下次保存重复提交）
      pendingNotesRef.current.clear()
      // v3.5.12：全部写库成功才算正式落库，本地草稿随之作废；
      // 部分失败时**保留草稿**——下面会重拉库内状态，未写进去的部分还能恢复
      if (failCount === 0) clearDraft()
      // 通知其他标签页（用例列表页）刷新，保证列表与导图一致
      notifyCasesChanged()
      // 无论成败都重拉数据并重建树：失败时也必须让导图回到与库一致的状态，
      // 否则用户会在过期结构上继续编辑，产生二次覆盖（B-4：全部失败时同样刷新，
      // 与错误提示"已按数据库实际状态刷新"保持一致）
      if ((addCount + updateCount + deleteCount) > 0 || failCount > 0) {
        await load()
        notifyCasesChanged()
      }
    } catch (e: any) {
      setSaveFail(true)
      setSaveMsg('')
      setErr(String(e?.message || e))
    } finally { setSaving(false) }
  }, [productId, load])

  // 重载
  const handleReload = useCallback(async () => {
    await load()
    // 刷新 = 用户明确放弃当前改动，本地草稿同步作废（否则刷新后又提示恢复旧草稿）
    clearDraft()
  }, [load])

  useEffect(() => { load() }, [load])

  // 工具栏按钮组：展开/收起
  const expandBtns = (
    <div className="flex items-center gap-1.5">
      <button
        onClick={() => meRef.current?.execCommand('EXPAND_ALL')}
        className="btn-secondary flex items-center gap-1.5 text-xs"
        title="展开所有节点"
      ><ChevronsUpDown className="w-3.5 h-3.5" />全部展开</button>
      <button
        onClick={() => meRef.current?.execCommand('UNEXPAND_ALL')}
        className="btn-secondary flex items-center gap-1.5 text-xs"
        title="收起所有节点"
      ><ChevronsDownUp className="w-3.5 h-3.5" />全部收起</button>
      <button
        onClick={() => meRef.current?.execCommand('UNEXPAND_TO_LEVEL', 2)}
        className="btn-secondary text-xs"
        title="展开到需求层级"
      >到2层</button>
      <button
        onClick={() => meRef.current?.execCommand('UNEXPAND_TO_LEVEL', 3)}
        className="btn-secondary text-xs"
        title="展开到功能点层级"
      >到3层</button>
    </div>
  )

  return (
    // v3.5.12：整页进全屏（页头保留 → 全屏里依然能撤销/重做/保存）；
    // 全屏 API 不可用时退回 CSS 沉浸模式（fixed inset-0），功能不失效
    <div
      ref={pageRef}
      className={isFullscreen
        ? 'fixed inset-0 z-[1000] flex flex-col gap-3 p-3 overflow-hidden'
        : 'space-y-4'}
      style={isFullscreen ? { background: 'var(--background)' } : undefined}
    >
      {/* 顶栏 */}
      <div className="flex items-center justify-between flex-shrink-0">
        <div className="flex items-center gap-3">
          <h1 className="page-title flex items-center gap-2"><FileText className="w-5 h-5" />XMind 思维导图编辑</h1>
          <span className="badge text-xs" style={{ background: 'rgba(0, 122, 255, 0.08)', color: 'var(--brand-500)' }}>
            {selectedProject?.name || projectId || '未选择项目'}
          </span>
          <span className="text-sm" style={{ color: 'var(--foreground-tertiary)' }}>{cases.length} 个用例</span>
          {draftAt > 0 && (
            <span
              className="text-xs"
              style={{ color: 'var(--foreground-tertiary)' }}
              title="本地草稿只存在本机浏览器，点「保存」才写入系统"
            >本地草稿 {new Date(draftAt).toLocaleTimeString()}</span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {expandBtns}
          {/* 撤销 / 重做：库无 Ctrl+Z 绑定（上游已移除），走 command.back/forward */}
          <button
            onClick={() => applyHistory('back')}
            disabled={!histState.canBack}
            className="btn-secondary flex items-center gap-1.5 disabled:opacity-40"
            title="撤销（回退一步）"
          ><Undo2 className="w-4 h-4" />回退</button>
          <button
            onClick={() => applyHistory('forward')}
            disabled={!histState.canForward}
            className="btn-secondary flex items-center gap-1.5 disabled:opacity-40"
            title="重做（前进一步）"
          ><Redo2 className="w-4 h-4" />前进</button>
          {!isFullscreen && (
            <Link to="/testcases" className="btn-secondary flex items-center gap-2"><ArrowLeft className="w-4 h-4" />返回测试用例</Link>
          )}
          <button onClick={handleReload} disabled={loading || saving} className="btn-ghost flex items-center gap-2"><RefreshCw className="w-4 h-4" />刷新</button>
          <button
            onClick={isFullscreen ? exitFullscreen : toggleFullscreen}
            disabled={loading}
            className="btn-secondary flex items-center gap-2 disabled:opacity-40"
            title={isFullscreen ? '退出全屏（ESC）' : '全屏编辑（沉浸式，ESC 退出）'}
          >{isFullscreen ? <Minimize2 className="w-4 h-4" /> : <Maximize2 className="w-4 h-4" />}{isFullscreen ? '退出全屏' : '全屏'}</button>
          <button onClick={handleSave} disabled={saving || loading || cases.length === 0} className="btn-primary flex items-center gap-2 disabled:opacity-50">
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}保存
          </button>
        </div>
      </div>

      {/* 提示条 */}
      {(saveMsg || err) && (
        <div className="card px-4 py-2.5 flex items-center gap-2 text-sm flex-shrink-0"
          style={(err || saveFail)
            ? { background: 'rgba(255, 59, 48, 0.08)', color: 'var(--state-error)', border: '1px solid rgba(255, 59, 48, 0.35)' }
            : { background: 'rgba(52, 199, 89, 0.08)', color: 'var(--state-success)' }}>
          <span>{err || saveMsg}</span>
        </div>
      )}

      {/* 主体 */}
      {/* 容器 div 始终渲染（loading/空态用 overlay 覆盖）：
          若条件渲染卸载容器，simple-mind-map 的 destroy/DOM 操作会因父节点
          已被 React 移除而抛 removeChild NotFoundError，导致 React 崩溃白屏 */}
      <div className={isFullscreen ? 'card relative flex-1 min-h-0 flex flex-col overflow-hidden' : 'card relative'}>
        <div
          ref={containerRef}
          style={isFullscreen
            ? { width: '100%', flex: 1, minHeight: 0 }
            : { width: '100%', height: '74vh', minHeight: '600px' }}
        />
        {loading && (
          <div className="absolute inset-0 flex items-center justify-center gap-3 bg-white/80 backdrop-blur-[1px]">
            <Loader2 className="w-5 h-5 animate-spin" style={{ color: 'var(--brand-500)' }} />加载用例...
          </div>
        )}
        {!loading && cases.length === 0 && (
          <div className="absolute inset-0 flex flex-col items-center justify-center p-16 text-center bg-white">
            <Layers className="w-12 h-12 mb-3" style={{ color: 'var(--foreground-quaternary)' }} />
            <p className="font-medium" style={{ color: 'var(--foreground-tertiary)' }}>该项目暂无用例</p>
            <p className="text-sm mt-1" style={{ color: 'var(--foreground-tertiary)' }}>请先在测试用例页生成用例后再打开 XMind 编辑</p>
          </div>
        )}
      </div>

      {/* 操作说明（全屏时隐藏，把屏幕全留给导图） */}
      {!isFullscreen && !loading && cases.length > 0 && (
        <div className="card px-4 py-2.5 text-xs flex-shrink-0" style={{ color: 'var(--foreground-tertiary)' }}>
          编辑说明：双击节点可编辑标题；Tab 新增子节点、Enter 新增同级节点、Delete 删除节点；右键节点可插入/删除节点；
          节点左侧圆点可折叠/展开；画布支持拖动和滚轮缩放；可拖拽节点调整层级。
          「回退/前进」撤销重做，「全屏」进入沉浸式编辑（ESC 退出）；
          编辑过程自动存本地草稿（只在本机浏览器，防意外退出），点「保存」才写入系统。
          右键用例节点选「编辑用例详情」可用表单编辑优先级/描述/前置条件/测试步骤，保存后立即写回系统用例；
          选「编辑备注（文本）」可直接按格式改备注文本（改步骤/前置条件最快捷），格式有误会逐行提示、无法保存。
          修改备注或树结构后点击「保存」一次性写回系统用例（新增/更新/删除）。
          节点层级：项目 → 需求 → 功能点(业务场景) → 测试类型分组 → 用例（功能测试组内细分保留在用例标题前缀与备注）。
        </div>
      )}

      {/* 自定义右键菜单 */}
      {menu && (
        <div
          className="fixed z-50 py-1 rounded-lg border shadow-lg text-sm"
          style={{
            left: Math.min(menu.x, window.innerWidth - 180),
            top: Math.min(menu.y, window.innerHeight - 260),
            background: 'var(--panel-bgcolor, #fff)',
            borderColor: 'var(--panel-border-color, #e5e5e5)',
            color: 'var(--panel-color, #333)',
          }}
          onMouseDown={e => e.stopPropagation()}
        >
          {menu.isCase && (
            <>
              <button
                className="block w-full text-left px-3 py-1.5 hover:bg-black/5 flex items-center gap-2"
                style={{ color: 'var(--brand-500)' }}
                onClick={() => { setMenu(null); if (menuNodeRef.current) openCaseEdit(menuNodeRef.current) }}
              >
                <Pencil className="w-3.5 h-3.5" />编辑用例详情 <span className="opacity-50 ml-auto">表单</span>
              </button>
              <button
                className="block w-full text-left px-3 py-1.5 hover:bg-black/5 flex items-center gap-2"
                style={{ color: 'var(--brand-500)' }}
                onClick={() => { setMenu(null); if (menuNodeRef.current) openNoteEdit(menuNodeRef.current) }}
              >
                <FileText className="w-3.5 h-3.5" />编辑备注（文本） <span className="opacity-50 ml-auto">改步骤</span>
              </button>
              <div className="my-1 border-t" style={{ borderColor: 'var(--panel-border-color, #eee)' }} />
            </>
          )}
          <button className="block w-full text-left px-3 py-1.5 hover:bg-black/5" onClick={() => execMenu('INSERT_CHILD_NODE')}>
            插入子节点 <span className="opacity-50 ml-2">Tab</span>
          </button>
          <button className="block w-full text-left px-3 py-1.5 hover:bg-black/5" onClick={() => execMenu('INSERT_NODE')}>
            插入同级节点 <span className="opacity-50 ml-2">Enter</span>
          </button>
          <button className="block w-full text-left px-3 py-1.5 hover:bg-black/5" onClick={() => execMenu('INSERT_PARENT_NODE')}>
            插入父节点 <span className="opacity-50 ml-2">Ctrl+Enter</span>
          </button>
          <div className="my-1 border-t" style={{ borderColor: 'var(--panel-border-color, #eee)' }} />
          <button className="block w-full text-left px-3 py-1.5 hover:bg-black/5 text-red-500" onClick={() => execMenu('REMOVE_NODE')}>
            删除节点 <span className="opacity-50 ml-2">Delete</span>
          </button>
          <div className="my-1 border-t" style={{ borderColor: 'var(--panel-border-color, #eee)' }} />
          <button
            className="block w-full text-left px-3 py-1.5 hover:bg-black/5 disabled:opacity-40"
            disabled={!histState.canBack}
            onClick={() => { applyHistory('back'); setMenu(null) }}
          >回退一步</button>
          <button
            className="block w-full text-left px-3 py-1.5 hover:bg-black/5 disabled:opacity-40"
            disabled={!histState.canForward}
            onClick={() => { applyHistory('forward'); setMenu(null) }}
          >前进一步</button>
          <div className="my-1 border-t" style={{ borderColor: 'var(--panel-border-color, #eee)' }} />
          <button className="block w-full text-left px-3 py-1.5 hover:bg-black/5" onClick={() => execMenu('EXPAND_ALL')}>全部展开</button>
          <button className="block w-full text-left px-3 py-1.5 hover:bg-black/5" onClick={() => execMenu('UNEXPAND_ALL')}>全部收起</button>
          <button className="block w-full text-left px-3 py-1.5 hover:bg-black/5" onClick={() => execMenu('UNEXPAND_TO_LEVEL', 2)}>展开到第2层</button>
          <button className="block w-full text-left px-3 py-1.5 hover:bg-black/5" onClick={() => execMenu('UNEXPAND_TO_LEVEL', 3)}>展开到第3层</button>
        </div>
      )}

      {/* 用例详情编辑弹窗（固定格式编辑器：优先级/描述/前置条件/步骤） */}
      {editState && (
        <CaseEditDialog
          caseData={editState.caseData}
          onClose={() => setEditState(null)}
          onSaved={handleCaseSaved}
        />
      )}

      {/* 备注文本编辑弹窗（按格式直接改备注 → 保存时反解回用例字段） */}
      {noteState && (
        <CaseNoteDialog
          caseData={noteState.caseData}
          noteText={noteState.text}
          onClose={() => setNoteState(null)}
          onSaved={handleNoteSaved}
        />
      )}
    </div>
  )
}
