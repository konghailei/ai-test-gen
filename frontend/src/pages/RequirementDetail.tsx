import { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { ArrowLeft, Play, MessageCircle, Send, Loader2, FileText, AlertCircle, CheckCircle2, Edit3, Save, X, FileCode, FileType, Database, Search, BookOpen, GitBranch, Share2, History, RefreshCw, Layers, Download, FilePlus, Image as ImageIcon, ChevronDown, ChevronRight } from 'lucide-react'
import { requirements as reqApi, products as prodApi } from '../api/client'
import type { RequirementDetail, ClarifyQuestion, ClarifyHistoryEntry, Product, RequirementRevision } from '../types'
import { formatDate, priorityBadge, STATUS_LABELS, normalizeSourceType, sourceTypeLabel } from '../utils'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeRaw from 'rehype-raw'
import FunctionalPoints from '../components/FunctionalPoints'
import FPSRRViewer from '../components/FPSRRViewer'
import FieldEditor from '../components/FieldEditor'
import VersionHistoryPanel from '../components/VersionHistoryPanel'
import FpCrsrPanel from '../components/FpCrsrPanel'
import RequirementQualityReport from '../components/RequirementQualityReport'

// ============================================================================
// Markdown 自定义渲染组件
// ============================================================================

/** 将 KB 来源的 value 格式化为可读摘要 */
function formatSourceValue(value: any): string {
  if (!value) return ''
  if (typeof value === 'string') return value.length > 500 ? value.slice(0, 500) + '...' : value
  if (Array.isArray(value)) {
    return value.slice(0, 5).map((v, i) => `[${i + 1}] ${typeof v === 'object' ? (v.description || v.rule || v.action || v.trigger || JSON.stringify(v).slice(0, 80)) : String(v).slice(0, 80)}`).join('\n')
  }
  if (typeof value === 'object') {
    const lines: string[] = []
    const hp = value.happy_path
    if (hp?.steps && Array.isArray(hp.steps)) {
      lines.push('【正常流程】')
      hp.steps.slice(0, 5).forEach((s: any) => lines.push(`  ${s.order ?? ''}. ${s.action ?? ''} → ${s.expect ?? ''}`.trim()))
    }
    const af = value.alternate_flows
    if (Array.isArray(af) && af.length) {
      lines.push('【异常/边界流程】')
      af.slice(0, 3).forEach((a: any) => lines.push(`  · ${a.trigger ?? ''} → ${a.expect ?? ''}`))
    }
    const br = value.business_rules
    if (Array.isArray(br) && br.length) {
      lines.push('【业务规则】')
      br.slice(0, 5).forEach((r: any) => lines.push(`  · ${r.rule ?? r.id ?? ''}`))
    }
    const dc = value.data_constraints
    if (Array.isArray(dc) && dc.length) {
      lines.push('【数据约束】')
      dc.slice(0, 5).forEach((d: any) => lines.push(`  · ${d.field ?? ''}: ${d.constraints ?? ''}`))
    }
    if (lines.length) return lines.join('\n')
    return `共 ${Object.keys(value).length} 个维度`
  }
  return String(value).slice(0, 500)
}

/** 代码块渲染（带语言标签） */
function CodeBlock({ className, children, ...props }: any) {
  const match = /language-(\w+)/.exec(className || '')
  const code = String(children).replace(/\n$/, '')
  return (
    <div className="relative group my-4">
      {match && (
        <div className="flex items-center justify-between px-4 py-1.5 text-xs font-mono" style={{ background: 'var(--background-muted)', borderTopLeftRadius: 'calc(var(--radius) * 0.5)', borderTopRightRadius: 'calc(var(--radius) * 0.5)', borderBottom: '1px solid var(--input)', color: 'var(--foreground-tertiary)' }}>
          <span>{match[1]}</span>
        </div>
      )}
      <pre className={`p-4 overflow-x-auto text-sm leading-relaxed ${match ? '' : ''}`} style={{ background: 'var(--background-muted)', border: '1px solid var(--border)', borderRadius: match ? '0 0 calc(var(--radius) * 0.5) calc(var(--radius) * 0.5)' : 'calc(var(--radius) * 0.5)' }}>
        <code className={className || ''} {...props}>{code}</code>
      </pre>
    </div>
  )
}

/** 表格渲染（带斑马纹和悬浮高亮） */
function Table({ children, ...props }: any) {
  return (
    <div className="overflow-x-auto my-4" style={{ borderRadius: 'calc(var(--radius) * 0.6)', border: '1px solid var(--border)' }}>
      <table className="w-full text-sm" {...props}>
        {children}
      </table>
    </div>
  )
}

/**
 * 将原始文本拆分为「普通文本段」+「表格段」+「图片描述段」。
 *
 * 1. 表格段：后端 _table_to_markdown 把 docx 表格转成标准 Markdown 表格语法
 *    （| col1 | col2 | + |---|---| 分隔行），需渲染为 HTML 表格保证可读性。
 * 2. 图片描述段：以「【图片N」开头的行及其后续多模态识别内容，需折叠展示避免占用过多空间。
 *
 * 所有段在渲染时统一纳入「行号列 + 内容列」两栏布局，保持页面整齐。
 */
function splitTextIntoBlocks(text: string): Array<
  | { type: 'text'; lines: string[]; startLine: number }
  | { type: 'table'; content: string; startLine: number; lineCount: number }
  | { type: 'image_desc'; lines: string[]; startLine: number; imageIndex: number | null }
> {
  const lines = text.split('\n')
  const blocks: Array<
    | { type: 'text'; lines: string[]; startLine: number }
    | { type: 'table'; content: string; startLine: number; lineCount: number }
    | { type: 'image_desc'; lines: string[]; startLine: number; imageIndex: number | null }
  > = []
  let i = 0
  let textStart = 0
  const flushText = (endIdx: number) => {
    if (endIdx > textStart) {
      blocks.push({ type: 'text', lines: lines.slice(textStart, endIdx), startLine: textStart })
    }
  }
  const isTableLine = (s: string) => /^\s*\|.*\|\s*$/.test(s)
  const isSeparator = (s: string) => /^[|\s:-]+$/.test(s) && s.includes('-')
  const isImageStart = (s: string) => /^【图片/.test(s)

  while (i < lines.length) {
    if (isTableLine(lines[i])) {
      // 表格块：连续的表格行
      let j = i
      while (j < lines.length && isTableLine(lines[j])) j++
      const tableLines = lines.slice(i, j)
      if (tableLines.length >= 2 && isSeparator(tableLines[1])) {
        flushText(i)
        blocks.push({ type: 'table', content: tableLines.join('\n'), startLine: i, lineCount: tableLines.length })
        i = j
        textStart = j
      } else {
        i++
      }
    } else if (isImageStart(lines[i])) {
      // 图片描述块：【图片N 开头 + 后续内容行
      // v2.47: 遇到连续两个空行（段落分隔）或表格或新图片时结束，避免描述被拆分
      const match = lines[i].match(/^【图片(\d+)/)
      const imgIdx = match ? parseInt(match[1], 10) : null
      let j = i + 1
      let prevBlank = false
      while (j < lines.length) {
        if (isTableLine(lines[j])) break
        if (isImageStart(lines[j])) break
        // 连续两个空行视为段落分隔，结束图片块
        if (lines[j].trim() === '' && prevBlank) break
        prevBlank = lines[j].trim() === ''
        j++
      }
      // 移除尾部空行（避免折叠栏内有多余空行）
      while (j > i + 1 && lines[j - 1].trim() === '') j--
      flushText(i)
      blocks.push({ type: 'image_desc', lines: lines.slice(i, j), startLine: i, imageIndex: imgIdx })
      i = j
      textStart = j
    } else {
      i++
    }
  }
  flushText(lines.length)
  return blocks
}

// ============================================================================
// 可搜索下拉（SearchSelect）：支持输入过滤、多选勾选、点击外部关闭
// v3.1.3：手动添加受影响功能点使用——模块下拉 + 功能点下拉
// ============================================================================
function SearchSelect({ options, selected, onToggle, placeholder, loading, disabled, display }: {
  options: Array<{ id: string; name: string; desc?: string }>
  selected: Set<string>
  onToggle: (id: string) => void
  placeholder: string
  loading?: boolean
  disabled?: boolean
  display?: string
}) {
  const [open, setOpen] = useState(false)
  const [kw, setKw] = useState('')
  const wrapRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  const filtered = kw ? options.filter(o => o.name.toLowerCase().includes(kw.toLowerCase())) : options

  return (
    <div ref={wrapRef} className="relative w-full" style={{ position: 'relative' }}>
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        disabled={disabled}
        className="text-sm flex items-center justify-between gap-1 w-full"
        style={{ padding: '7px 10px', borderRadius: 'calc(var(--radius) * 0.5)', border: '1px solid var(--border)', background: 'var(--background)', color: 'var(--foreground-secondary)', cursor: disabled ? 'not-allowed' : 'pointer' }}>
        <span className="truncate" style={{ color: display || selected.size ? 'var(--foreground-secondary)' : 'var(--foreground-tertiary)' }}>
          {display || (selected.size ? `已选 ${selected.size} 项` : placeholder)}
        </span>
        {loading ? <Loader2 className="w-3.5 h-3.5 animate-spin" style={{ color: 'var(--foreground-tertiary)' }} /> : <ChevronDown className="w-3.5 h-3.5" style={{ color: 'var(--foreground-tertiary)' }} />}
      </button>
      {open && (
        <div style={{ position: 'absolute', top: 'calc(100% + 4px)', left: 0, right: 0, zIndex: 60, background: 'var(--background)', border: '1px solid var(--border)', borderRadius: 'calc(var(--radius) * 0.5)', boxShadow: '0 8px 24px rgba(0,0,0,0.10)' }}>
          <input
            autoFocus
            value={kw}
            onChange={e => setKw(e.target.value)}
            placeholder="输入关键字过滤..."
            className="text-sm w-full"
            style={{ padding: '7px 10px', borderRadius: 6, border: 'none', borderBottom: '1px solid var(--border)', background: 'transparent', outline: 'none', color: 'var(--foreground-secondary)' }}
          />
          {loading ? (
            <div className="flex items-center gap-2 px-3 py-2 text-xs" style={{ color: 'var(--foreground-tertiary)' }}>
              <Loader2 className="w-3.5 h-3.5 animate-spin" />加载中...
            </div>
          ) : filtered.length === 0 ? (
            <p className="px-3 py-2 text-xs" style={{ color: 'var(--foreground-tertiary)' }}>无匹配项</p>
          ) : (
            <div className="max-h-52 overflow-y-auto py-1">
              {filtered.map(o => {
                const checked = selected.has(o.id)
                return (
                  <label key={o.id} className="flex items-center gap-2 px-2 py-1.5 cursor-pointer"
                    style={{ background: checked ? 'rgba(0,122,255,0.06)' : 'transparent', borderRadius: 'calc(var(--radius) * 0.4)' }}>
                    <input type="checkbox" checked={checked} onChange={() => onToggle(o.id)} style={{ accentColor: 'var(--brand-500)', width: 15, height: 15, flexShrink: 0 }} />
                    <span className="text-sm truncate" style={{ color: 'var(--foreground-secondary)' }}>{o.name}</span>
                    {o.desc && <span className="text-xs truncate ml-auto" style={{ color: 'var(--foreground-tertiary)' }}>{o.desc}</span>}
                  </label>
                )
              })}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ============================================================================
// 主页面组件
// ============================================================================

export default function RequirementDetail() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const [req, setReq] = useState<RequirementDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [questions, setQuestions] = useState<ClarifyQuestion[]>([])
  const [showClarify, setShowClarify] = useState(false)
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const [submitting, setSubmitting] = useState(false)
  const [clarifyDone, setClarifyDone] = useState(false)
  const [clarifyError, setClarifyError] = useState('')

  // 编辑模式
  const [editing, setEditing] = useState(false)
  const [editJson, setEditJson] = useState('')
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState('')

  // 确认生成最终SRR
  const [finalizing, setFinalizing] = useState(false)
  // v1.6: 确认需求完成
  const [confirming, setConfirming] = useState(false)
  // v2.0: 导出 XMind；v2.20 支持选择 XMind 2020+ JSON / XMind 8 XML 两种格式
  const [exporting, setExporting] = useState(false)
  const [xmindFormat, setXmindFormat] = useState<'xmind' | 'xmind8'>('xmind')
  // v2.20: 下拉菜单改用 click 控制（hover 在某些浏览器/自动化测试中不稳定）
  const [xmindDropdownOpen, setXmindDropdownOpen] = useState(false)
  const xmindDropdownRef = useRef<HTMLDivElement>(null)

  // v2.33: 多人协作冲突检测——轮询 revision + 任务运行状态
  const [conflictDetected, setConflictDetected] = useState(false)
  const [taskRunningInfo, setTaskRunningInfo] = useState<{ message: string; task_type: string } | null>(null)
  const localRevisionRef = useRef<number | null>(null)
  const isLocalMutationRef = useRef(false)
  const prevTaskRunningRef = useRef(false)

  // 点击外部关闭下拉菜单
  useEffect(() => {
    if (!xmindDropdownOpen) return
    const handler = (e: MouseEvent) => {
      if (xmindDropdownRef.current && !xmindDropdownRef.current.contains(e.target as Node)) {
        setXmindDropdownOpen(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [xmindDropdownOpen])

  const handleExportXmind = async () => {
    if (!id) return
    setExporting(true)
    try {
      const res = await reqApi.exportXmind(id, xmindFormat)
      alert(`已导出 XMind 文件：${res.filename}（${(res.size / 1024).toFixed(1)} KB）`)
    } catch (e: any) {
      alert('XMind 导出失败: ' + (e?.message || '未知错误'))
    } finally {
      setExporting(false)
    }
  }

  // Tab 切换：'srr' | 'raw' | 'kb' | 'impact' | 'history' | 'versions'
  const [activeTab, setActiveTab] = useState<'srr' | 'raw' | 'kb' | 'impact' | 'history' | 'versions'>('srr')

  // 澄清历史
  const [clarifyHistory, setClarifyHistory] = useState<ClarifyHistoryEntry[]>([])
  const [historyLoading, setHistoryLoading] = useState(false)

  // 影响分析
  const [impactLoading, setImpactLoading] = useState(false)
  const [confirmImpactLoading, setConfirmImpactLoading] = useState(false)
  const [impactData, setImpactData] = useState<{
    req_title: string
    direct_modules: { id: string; name: string }[]
    indirect_modules: { id: string; name: string; reason: string; depth: number }[]
    affected_fps: {
      fp_id: string
      fp_name: string
      fp_status: string
      req_id?: string
      req_title?: string
      module_id: string
      module_name: string
      impact_reason: string
      depth: number
      source_fp_name: string
      kb_context: string
      existing_cases_count: number
      /** v3.3 P0-1: 引用热度（被依赖数），后端按此降序排序 */
      fan_in?: number
    }[]
    total_affected: number
  } | null>(null)
  // 用户勾选的受影响 FP（将用于生成回归测试用例）
  const [confirmedFpIds, setConfirmedFpIds] = useState<Set<string>>(new Set())
  // v3.2: 展开/收起受影响 FP 行（点击行展开显示最新完整 SRR）
  const [expandedFpIds, setExpandedFpIds] = useState<Set<string>>(new Set())
  const toggleFpExpand = (fpId: string) => {
    setExpandedFpIds(prev => {
      const next = new Set(prev)
      if (next.has(fpId)) next.delete(fpId)
      else next.add(fpId)
      return next
    })
  }

  // ===== 手动补选受影响功能点（v3.1.3 增强） =====
  // 依赖图谱 BFS 只覆盖已建依赖边；跨需求/漏建边时可由用户按模块+名称检索补选。
  // 补选 FP 直接并入 confirmedFpIds，复用既有 confirm_impact_analysis 写入 SRR.impact_analysis。
  const [manualOpen, setManualOpen] = useState(false)
  const [manualModuleId, setManualModuleId] = useState('')
  const [manualModules, setManualModules] = useState<{ id: string; name: string }[]>([])
  const [manualFpPool, setManualFpPool] = useState<Array<{ id: string; name: string; module_id?: string; modules?: string[]; source_req_title?: string; module_names?: string[]; source_req_id?: string }>>([])
  const [manualFpLoading, setManualFpLoading] = useState(false)
  const manualFpLoaded = useRef(false)

  useEffect(() => {
    if (!manualOpen || !req?.product_id) return
    // v3.1.3：展开即一次性加载模块与已归档 FP 池，后续本地按模块/关键字过滤，无需逐个点检索。
    if (manualModules.length === 0) {
      prodApi.listModules(req.product_id).then(m => setManualModules(Array.isArray(m) ? m : [])).catch(() => {})
    }
    if (!manualFpLoaded.current && id) {
      manualFpLoaded.current = true
      setManualFpLoading(true)
      reqApi.fpListReusable(id)
        .then(res => setManualFpPool((res?.items as any) || []))
        .catch(() => setManualFpPool([]))
        .finally(() => setManualFpLoading(false))
    }
  }, [manualOpen, req?.product_id, id]) // eslint-disable-line react-hooks/exhaustive-deps

  // 功能点下拉选项：按已选模块过滤；默认展示全部已归档 FP（供全库搜索）
  const manualFpOptions = useMemo(() => {
    return manualFpPool
      .filter(fp => !manualModuleId || fp.module_id === manualModuleId || (fp.modules || []).includes(manualModuleId))
      .map(fp => ({
        id: fp.id,
        name: fp.name,
        desc: [fp.module_names?.join(', '), fp.source_req_title].filter(Boolean).join(' · '),
      }))
  }, [manualFpPool, manualModuleId])

  // v3.1.3：手动添加受影响功能点 —— 「选择功能点 → 点添加 → 加入确认列表」。
  // manualSelFpId：功能点下拉当前选中项（暂存，未加入确认）。
  const [manualSelFpId, setManualSelFpId] = useState('')
  // 本次 BFS 候选集：用于区分「图谱分析出的候选」与「手动补选」。
  const manualCandidateSet = useMemo(
    () => new Set((impactData?.affected_fps || []).map(x => x.fp_id).filter(Boolean) as string[]),
    [impactData],
  )
  // 已添加列表 = 确认列表中不属于 BFS 候选集的功能点（即手动补选来源），从 SRR 恢复时同样成立。
  const manualAddedList = useMemo(() => {
    if (!manualFpPool.length) return []
    return Array.from(confirmedFpIds)
      .filter(fpId => !manualCandidateSet.has(fpId))
      .map(fpId => manualFpPool.find(fp => fp.id === fpId))
      .filter((fp): fp is { id: string; name: string; module_id?: string; modules?: string[]; module_names?: string[]; source_req_title?: string; source_req_id?: string } => Boolean(fp))
  }, [confirmedFpIds, manualCandidateSet, manualFpPool])

  // 功能点下拉单选（暂存选中）
  const handleManualFpPick = (fpId: string) => setManualSelFpId(prev => (prev === fpId ? '' : fpId))

  // 点击「添加」→ 加入确认列表（并入 confirmedFpIds，随确认一并写入 SRR）
  const addManualFp = (): void => {
    if (!manualSelFpId) return
    if (confirmedFpIds.has(manualSelFpId)) { alert('该功能点已在确认列表中'); return }
    setConfirmedFpIds(prev => { const next = new Set(prev); next.add(manualSelFpId); return next })
    setManualSelFpId('')
  }

  // 从确认列表移除一个已添加的功能点
  const removeManualFp = (fpId: string) => {
    setConfirmedFpIds(prev => { const next = new Set(prev); next.delete(fpId); return next })
  }

  // v3.1.3：受影响功能点"合并列表" = BFS 依赖图谱分析结果 + 手动补选的功能点。
  // 手动补选项复用主列表的行渲染（可点行展开查看功能点最新完整 SRR），
  // 并携带 is_manual 标记用于展示"手动添加"与行内移除按钮。
  const allImpactedFps = useMemo(() => {
    const manualRows = manualAddedList.map(fp => ({
      fp_id: fp.id,
      fp_name: fp.name,
      module_name: fp.module_names?.[0] || '',
      module_id: '',
      req_id: fp.source_req_id || '',
      req_title: fp.source_req_title || '',
      fp_status: '',
      source_fp_name: '',
      kb_context: '',
      depth: 0,
      fan_in: 0,
      existing_cases_count: 0,
      is_manual: true,
      impact_reason: `手动添加的受影响功能点${fp.source_req_title ? `（来自「${fp.source_req_title}」）` : ''}`,
    }))
    // BFS 项补 is_manual:false，使两侧字段结构一致，避免联合类型字段访问报错
    const bfsRows = (impactData?.affected_fps || []).map(x => ({ ...x, is_manual: false }))
    const seen = new Set<string>()
    return [...bfsRows, ...manualRows].filter(x => {
      if (seen.has(x.fp_id)) return false
      seen.add(x.fp_id)
      return true
    })
  }, [impactData, manualAddedList])

  // 从 SRR.impact_analysis 恢复已确认的 fp_id
  useEffect(() => {
    const srr = req?.srr_json as Record<string, unknown> | undefined
    const ia = srr?.impact_analysis as Array<{ fp_id?: string; is_affected?: boolean; confirmed_at?: string }> | undefined
    if (Array.isArray(ia)) {
      // 修复 M5：严格按后端 SRR 同步。只同步 is_affected=true + confirmed_at 的项。
      // 如果后端某 FP 被取消确认（is_affected=false + confirmed_at）或被移除，
      // 这里会自动从前端 confirmedFpIds 去掉，保证双向一致性。
      const set = new Set<string>()
      for (const item of ia) {
        if (item.fp_id && item.is_affected === true && item.confirmed_at) {
          set.add(item.fp_id)
        }
      }
      setConfirmedFpIds(set)
    } else if (!ia && (req?.id || id)) {
      // SRR 中不再有 impact_analysis（被后端 removed 清空了），同步清空前端勾选
      setConfirmedFpIds(prev => (prev.size === 0 ? prev : new Set()))
    }
  }, [req?.srr_json, req?.id, id])

  // v3.4: 影响分析待确认数——后端 run_impact_analysis 已将受影响 FP 以未确认态
  // 持久化到 srr.impact_analysis；存在未确认条目时需求无法 confirm/归档（链式锁定）。
  // 此处统计未确认条目数，用于影响分析 tab 标题黄色提醒。
  const impactUnconfirmedCount = useMemo(() => {
    const srr = req?.srr_json as Record<string, unknown> | undefined
    const ia = srr?.impact_analysis as Array<{ confirmed_at?: string }> | undefined
    if (!Array.isArray(ia)) return 0
    return ia.filter(x => !x.confirmed_at).length
  }, [req?.srr_json])

  // ---- v1.6.1: 泛化 SRR 字段保存（FieldEditor 用） ----
  const saveSrrField = async (fieldPath: string, newValue: unknown) => {
    if (!id) return
    const srr = JSON.parse(JSON.stringify(detail.srr_json || {}))
    const path = fieldPath.split('.')
    let target: any = srr
    for (let i = 0; i < path.length - 1; i++) {
      if (!target[path[i]]) target[path[i]] = {}
      target = target[path[i]]
    }
    target[path[path.length - 1]] = newValue
    // v3.4（docs/15 §4.8）：提交带乐观锁，被他人抢先修改时 409 提示刷新，不静默覆盖
    try {
      await reqApi.update(id, srr, undefined, localRevisionRef.current || undefined)
    } catch (e: any) {
      alert(e?.message || '保存失败：需求可能已被他人修改，请刷新页面后重试')
      refreshRevisionBaseline()
      return
    }
    setReq(prev => prev ? { ...prev, srr_json: srr } : prev)
    // v2.33: 本地变更后刷新 revision 基线，避免下次轮询误报冲突
    refreshRevisionBaseline()
  }

  // 模块名称映射（ID→名称），用于 SRR 中显示模块名称
  const [moduleMap, setModuleMap] = useState<Record<string, string>>({})

  // 加载模块列表用于解析名称
  useEffect(() => {
    if (req?.product_id) {
      prodApi.listModules(req.product_id).then(mods => {
        const map: Record<string, string> = {}
        mods.forEach((m: any) => { map[m.id] = m.name })
        setModuleMap(map)
      }).catch(() => {})
    }
  }, [req?.product_id])

  // v2.38: 加载功能点列表，用于跨功能点交互规则中的功能点选择下拉框
  const [fpOptions, setFpOptions] = useState<{ value: string; label: string }[]>([])
  useEffect(() => {
    if (!id) return
    reqApi.fpList(id).then(r => {
      setFpOptions((r.items || []).map((fp: any) => ({ value: fp.name, label: fp.name })))
    }).catch(() => {})
  }, [id])

  // v2.38: non_functional 对象↔数组转换保存
  const saveNonFunctional = async (_fieldPath: string, newValue: unknown) => {
    const arr = Array.isArray(newValue) ? newValue as any[] : []
    const obj: Record<string, string> = {}
    arr.forEach((item: any) => {
      if (item.category && item.requirement) {
        obj[item.category] = item.requirement
      }
    })
    await saveSrrField('non_functional', obj)
  }

  // 产品列表（用于名称查找）
  const [products, setProducts] = useState<Product[]>([])

  // 多文件选择（原始文本 tab 内）
  const [selectedFileIdx, setSelectedFileIdx] = useState(0)

  useEffect(() => {
    if (!id) return
    reqApi.get(id).then(setReq).catch(() => navigate('/projects')).finally(() => setLoading(false))
  }, [id])

  useEffect(() => { prodApi.list().then(list => setProducts(list || [])) }, [])

  // v2.33: 多人协作冲突检测——初次加载 revision 基线
  useEffect(() => {
    if (!id) return
    reqApi.getRevision(id).then((info: RequirementRevision) => {
      localRevisionRef.current = info.revision
      if (info.task_running) {
        setTaskRunningInfo({ message: info.task_message, task_type: info.task_type })
      }
    }).catch(() => {})
  }, [id])

  // v2.33: 每 5 秒轮询 revision，检测他人修改 + 任务运行状态
  useEffect(() => {
    if (!id) return
    const timer = setInterval(async () => {
      try {
        const info = await reqApi.getRevision(id)
        const wasTaskRunning = prevTaskRunningRef.current
        const isTaskRunning = info.task_running

        // 任务状态更新（实时反映后端任务进度）
        if (isTaskRunning) {
          setTaskRunningInfo({ message: info.task_message, task_type: info.task_type })
        } else {
          setTaskRunningInfo(null)
        }

        // 任务刚完成（running → idle）：静默刷新基线，避免任务自身 revision 变化误报为冲突
        if (wasTaskRunning && !isTaskRunning) {
          localRevisionRef.current = info.revision
          // 任务完成后自动刷新需求详情，展示最新内容
          try {
            const updated = await reqApi.get(id)
            setReq(updated)
          } catch {}
        } else if (
          !isTaskRunning &&  // 无任务运行时才检测冲突（任务运行期间锁机制保证无并发修改）
          localRevisionRef.current !== null &&
          info.revision > localRevisionRef.current &&
          !isLocalMutationRef.current
        ) {
          // 冲突检测：远程 revision > 本地基线，且非本地刚变更
          setConflictDetected(true)
        }

        // 重置标记
        isLocalMutationRef.current = false
        prevTaskRunningRef.current = isTaskRunning
      } catch {
        // 静默失败，不打扰用户
      }
    }, 5000)
    return () => clearInterval(timer)
  }, [id])

  /** 本地变更后刷新 revision 基线，避免下次轮询误报冲突 */
  const refreshRevisionBaseline = useCallback(async () => {
    if (!id) return
    try {
      isLocalMutationRef.current = true
      const info = await reqApi.getRevision(id)
      localRevisionRef.current = info.revision
      prevTaskRunningRef.current = info.task_running
      if (info.task_running) {
        setTaskRunningInfo({ message: info.task_message, task_type: info.task_type })
      } else {
        setTaskRunningInfo(null)
      }
    } catch {}
  }, [id])

  const loadQuestions = async () => {
    if (!id) return
    setClarifyError('')
    try {
      const qs = await reqApi.clarifyQuestions(id)
      setQuestions(qs)
      const init: Record<string, string> = {}
      qs.forEach(q => { init[q.id] = '' })
      setAnswers(init)
      setShowClarify(true)
    } catch (e: any) {
      setClarifyError(e?.message || '加载澄清问题失败，请稍后重试')
    }
  }

  const loadImpact = async () => {
    if (!id) return
    setImpactLoading(true)
    setImpactData(null)
    try {
      // 归档需求：只读展示历史影响分析记录（srr_json.impact_analysis），
      // 不调用重新分析端点（后端对归档需求返回 409「仅支持查看」）。
      if (detail?.status === 'archived') {
        const srr = detail.srr_json as Record<string, unknown> | undefined
        const ia = srr?.impact_analysis as Array<{
          fp_id?: string; fp_name?: string; is_affected?: boolean
          impact_description?: string; confirmed_at?: string; source_req_id?: string; fp_type?: string
        }> | undefined
        // v3.2: 只展示「确认受影响」的历史条目，不显示"确认不受影响"
        const items = (Array.isArray(ia) ? ia : []).filter(item => item.is_affected !== false)
        setImpactData({
          req_title: detail.title || id,
          direct_modules: [],
          indirect_modules: [],
          affected_fps: items.map(item => ({
            fp_id: item.fp_id || '',
            fp_name: item.fp_name || item.fp_id || '',
            fp_status: 'archived',
            req_id: item.source_req_id || '',
            req_title: '',
            module_id: '',
            module_name: '',
            impact_reason: item.impact_description || '受需求影响',
            depth: 0,
            source_fp_name: '',
            kb_context: '',
            existing_cases_count: 0,
          })),
          total_affected: items.length,
        })
        return
      }
      const result = await reqApi.impactAnalysis(id)
      setImpactData(result)
      // v3.4: 后端已将受影响 FP 以未确认态持久化到 srr.impact_analysis，
      // 刷新 detail 使影响分析 tab 的"待确认"提醒与需求状态同步
      try {
        const refreshed = await reqApi.get(id)
        if (refreshed) setReq(refreshed)
      } catch { /* 刷新失败不影响展示 */ }
    } catch (e: any) {
      alert('影响分析失败: ' + (e?.message || '未知错误'))
    } finally {
      setImpactLoading(false)
    }
  }

  const toggleFpConfirm = (fpId: string) => {
    setConfirmedFpIds(prev => {
      const next = new Set(prev)
      if (next.has(fpId)) next.delete(fpId)
      else next.add(fpId)
      return next
    })
  }

  const toggleAllFps = () => {
    if (!impactData) return
    const all = allImpactedFps.map(x => x.fp_id)
    const allSelected = all.every(id => confirmedFpIds.has(id))
    const next = new Set<string>()
    if (!allSelected) all.forEach(id => next.add(id))
    setConfirmedFpIds(next)
  }

  const handleConfirmImpact = async () => {
    if (!id) return
    // 修复 H5：按"三集合"提交
    // - confirmed_fp_ids：当前前端勾选的（视为「受影响」，生成回归用例）
    // - unconfirmed_fp_ids：候选 FP 中未勾选的（视为「用户确认不受影响」，打 confirmed_at 放行守卫）
    // - 不在候选中的旧条目（例如之前已勾选过，本次重新分析后 candidate 里不再出现）：放入 removed_fp_ids 移除
    const candidateIds = impactData?.affected_fps.map(x => x.fp_id).filter(Boolean) as string[]
    const candidateSet = new Set(candidateIds || [])

    // v3.1.3 修复：手动补选的功能点（BFS 候选之外，由用户在"手动添加受影响功能点"中检索勾选）
    // 之前被 candidateSet.has(x) 过滤掉导致无法提交。现放开：候选集的勾选 → 受影响；
    // 候选集未勾选 → 确认不受影响；候选集之外的勾选（手动补选）→ 也视为受影响一并提交，
    // 由后端 confirm_impact_analysis 的 _write_entry 以 append 方式写入新条目。
    const inCandidateConfirmed = Array.from(confirmedFpIds).filter(x => candidateSet.has(x))
    const manualConfirmed = Array.from(confirmedFpIds).filter(x => !candidateSet.has(x))
    const confirmed: string[] = Array.from(new Set([...inCandidateConfirmed, ...manualConfirmed]))
    const unaffected: string[] = Array.from(candidateSet).filter(x => !confirmedFpIds.has(x))

    // 需要移除的旧条目：SRR.impact_analysis 中出现但本次候选中没有的 FP。
    // 排除当前仍勾选的（含手动补选），避免把用户明确保留为受影响的功能点误移除。
    const srr = req?.srr_json as Record<string, unknown> | undefined
    const ia = srr?.impact_analysis as Array<{ fp_id?: string }> | undefined
    const oldIds = new Set((Array.isArray(ia) ? ia : []).map(x => x.fp_id).filter(Boolean) as string[])
    const removed: string[] = Array.from(oldIds).filter(x => !candidateSet.has(x) && !confirmedFpIds.has(x))

    if (confirmed.length === 0 && unaffected.length === 0 && removed.length === 0) {
      alert('请先勾选或取消勾选受影响的功能点')
      return
    }
    setConfirmImpactLoading(true)
    try {
      const res = await reqApi.confirmImpactAnalysis(id, {
        confirmed_fp_ids: confirmed,
        unconfirmed_fp_ids: unaffected,
        removed_fp_ids: removed,
      })
      // 刷新 detail（最新 SRR.impact_analysis）
      const detailRes = await reqApi.get(id)
      if (detailRes) setReq(detailRes)
      const totalHandled =
        Number(res.confirmed_count || 0) +
        Number(res.unconfirmed_count || 0) +
        Number(res.removed_count || 0)
      alert(
        `已确认 ${res.confirmed_count || 0} 个受影响功能点，` +
        `标记 ${res.unconfirmed_count || 0} 个不受影响，` +
        `移除 ${res.removed_count || 0} 个过期条目。\n` +
        '后续生成用例时，受影响的功能点将生成对应的「回归测试用例」。' +
        (totalHandled === 0 ? '\n\n（提示：本次提交未匹配到数据库中有效的功能点 ID，可能是 FP 已被删除）' : ''),
      )
    } catch (e: any) {
      alert('确认受影响功能点失败: ' + (e?.message || '未知错误'))
    } finally {
      setConfirmImpactLoading(false)
    }
  }

  const handleReRunImpact = async () => {
    if (!id) return
    setImpactLoading(true)
    try {
      const result = await reqApi.runImpactAnalysis(id)
      // 将 v3 POST 返回结构映射到前端现有 state 结构
      setImpactData({
        req_title: result.requirement?.title || '',
        direct_modules: result.module_impact?.direct || [],
        indirect_modules: result.module_impact?.indirect || [],
        affected_fps: result.affected_fps || [],
        total_affected: result.total_affected || 0,
      })
      // v3.4: 后端已持久化未确认候选，刷新 detail 同步 tab"待确认"提醒
      try {
        const refreshed = await reqApi.get(id)
        if (refreshed) setReq(refreshed)
      } catch { /* 刷新失败不影响展示 */ }
    } catch (e: any) {
      alert('重新分析失败: ' + (e?.message || '未知错误'))
    } finally {
      setImpactLoading(false)
    }
  }

  // 切换 tab 时自动加载影响分析
  useEffect(() => {
    if (activeTab === 'impact' && !impactData && id) {
      loadImpact()
    }
    if (activeTab === 'history' && id) {
      setHistoryLoading(true)
      reqApi.getClarifyHistory(id).then(data => {
        setClarifyHistory(data?.items || [])
      }).catch(() => {
        setClarifyHistory([])
      }).finally(() => setHistoryLoading(false))
    }
  }, [activeTab])

  const submitClarify = async () => {
    if (!id) return
    setSubmitting(true)
    try {
      await reqApi.submitClarify(id, answers)
      setClarifyDone(true)
      setShowClarify(false)
      // v2.33: 澄清提交会触发后端 revision 自增，刷新基线
      refreshRevisionBaseline()
    } catch (e: any) {
      alert('提交失败: ' + e.message)
    } finally {
      setSubmitting(false)
    }
  }

  // 进入编辑模式
  const startEditing = () => {
    if (!req) return
    setEditJson(JSON.stringify(req.srr_json || {}, null, 2))
    setEditing(true)
    setSaveError('')
  }

  // 保存 SRR 编辑
  const saveEdit = async () => {
    if (!id || !req) return
    setSaving(true)
    setSaveError('')
    try {
      const parsed = JSON.parse(editJson)
      // v3.4（docs/15 §4.8）：整体保存同样带乐观锁
      await reqApi.update(id, parsed, undefined, localRevisionRef.current || undefined)
      const updated = await reqApi.get(id)
      setReq(updated)
      setEditing(false)
      // v2.33: 本地编辑保存后刷新 revision 基线
      refreshRevisionBaseline()
    } catch (e: any) {
      if (e instanceof SyntaxError) {
        setSaveError('JSON 格式错误: ' + e.message)
      } else {
        setSaveError('保存失败: ' + e.message)
      }
    } finally {
      setSaving(false)
    }
  }

  const handleFinalize = async () => {
    if (!id) return
    setFinalizing(true)
    try {
      await reqApi.finalize(id)
      const updated = await reqApi.get(id)
      setReq(updated)
      // v2.33: 生成最终SRR后刷新 revision 基线
      refreshRevisionBaseline()
    } catch (e: any) {
      alert('生成最终SRR失败: ' + e.message)
    } finally {
      setFinalizing(false)
    }
  }

  // v1.6: 确认需求完成（pending_review → confirmed）
  const handleConfirm = async () => {
    if (!id) return
    setConfirming(true)
    try {
      await reqApi.confirm(id)
      const updated = await reqApi.get(id)
      setReq(updated)
      // v2.33: 确认状态变更后刷新 revision 基线
      refreshRevisionBaseline()
    } catch (e: any) {
      alert('确认失败: ' + e.message)
    } finally {
      setConfirming(false)
    }
  }

  if (loading) return <div className="space-y-4">{[1,2,3].map(i => <div key={i} className="skeleton h-16" />)}</div>
  if (!req) return null

  // 非空引用（供闭包函数使用，TS 类型收窄）
  const detail = req
  const srr = detail.srr_json as any
  const reqData = srr?.requirement || {}
  const flows = srr?.flows || {}

  /** 解析原始文本为文件条目列表（支持多文件合并格式 + 补充文件） */
  function parseFileEntries(): { filename: string; source_type: string; text: string; is_supplementary?: boolean }[] {
    const entries: { filename: string; source_type: string; text: string; is_supplementary?: boolean }[] = []

    // 1. 解析原始文件内容
    if (detail.raw_text) {
      const isMulti = normalizeSourceType(detail.source_type) === 'multi'
      if (!isMulti) {
        const st = normalizeSourceType(detail.source_type)
        entries.push({
          filename: `source.${st}`,
          source_type: st,
          text: detail.raw_text,
        })
      } else {
        const parts = detail.raw_text.split(/(?=【文档：)/)
        for (const part of parts) {
          const headerMatch = part.match(/^【文档：(.*?)】\n?/)
          if (!headerMatch) {
            if (entries.length > 0) entries[entries.length - 1].text += '\n' + part
            continue
          }
          const filename = headerMatch[1]
          const ext = filename.toLowerCase().split('.').pop() || 'txt'
          const sourceType = normalizeSourceType(ext)
          const text = part.slice(headerMatch[0].length).replace(/^\n---\n\n?/, '').trim()
          entries.push({ filename, source_type: sourceType, text })
        }
      }
    }

    // 2. 追加补充文件内容（v2.17: 补充文件不替代原始文件，两者都可见）
    if (detail.supplementary_text && detail.supplementary_text.trim()) {
      const suppText = detail.supplementary_text
      // 补充文件可能是多文件合并格式（--- 分隔），拆分展示
      const suppParts = suppText.split(/\n\n---\n\n/)
      if (suppParts.length > 1) {
        suppParts.forEach((part, idx) => {
          if (part.trim()) {
            entries.push({
              filename: `补充文件${idx + 1}.md`,
              source_type: 'markdown',
              text: part.trim(),
              is_supplementary: true,
            })
          }
        })
      } else {
        entries.push({
          filename: '补充文件.md',
          source_type: 'markdown',
          text: suppText.trim(),
          is_supplementary: true,
        })
      }
    }

    return entries
  }

  const fileEntries = parseFileEntries()

  // ==========================================================================
  // 渲染：SRR 解析内容
  // ==========================================================================
  function renderSrrContent() {
    return (
      <div className="space-y-5">
        {/* 元信息 */}
        <div className="grid grid-cols-3 gap-4 p-4" style={{ background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.6)' }}>
          <div><span className="text-xs block" style={{ color: 'var(--foreground-tertiary)' }}>产品</span><span className="text-sm font-medium" style={{ color: 'var(--foreground-secondary)' }}>{(products.find(p => p.id === (reqData.product_id || detail.product_id))?.name) || reqData.product_id || detail.product_id}</span></div>
          <div><span className="text-xs block" style={{ color: 'var(--foreground-tertiary)' }}>模块</span><span className="text-sm font-medium" style={{ color: 'var(--foreground-secondary)' }}>
            {detail.modules && detail.modules.length > 0
              ? detail.modules.map((m: string) => moduleMap[m] || m).join('、')
              : '-'}
          </span></div>
          <div><span className="text-xs block" style={{ color: 'var(--foreground-tertiary)' }}>状态</span><span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium" style={{
            background:
              detail.status === 'active' ? 'var(--state-success-surface)' :
              detail.status === 'draft' ? 'var(--state-warning-surface)' :
              detail.status === 'archived' ? 'var(--background-muted)' :
              detail.status === 'pending_review' ? 'rgba(0, 122, 255, 0.08)' :
              detail.status === 'confirmed' ? 'var(--state-success-surface)' :
              detail.status === 'analyzing' ? 'rgba(88, 86, 214, 0.1)' :
              'var(--background-muted)',
            color:
              detail.status === 'active' ? 'var(--state-success)' :
              detail.status === 'draft' ? 'var(--state-warning)' :
              detail.status === 'archived' ? 'var(--foreground-secondary)' :
              detail.status === 'pending_review' ? 'var(--brand-500)' :
              detail.status === 'confirmed' ? 'var(--state-success)' :
              detail.status === 'analyzing' ? '#5856d6' :
              'var(--foreground-tertiary)'
          }}>{STATUS_LABELS[detail.status] || detail.status}</span></div>
        </div>

        {/* v1.5.2: FP 审核（元信息下方、SRR 详情上方）。v?: 归档态也展示功能点 SRR（只读），由 FPSRRViewer 内部 isArchived 处理，避免归档后功能点 SRR 被隐藏 */}
        <div className="p-4" style={{ border: '1px solid var(--border)', borderRadius: 'calc(var(--radius) * 0.6)', background: 'var(--background)' }}>
          <h3 className="text-sm font-semibold mb-3 flex items-center gap-2" style={{ color: 'var(--foreground-secondary)' }}>
            <Layers className="w-4 h-4" style={{ color: 'var(--brand-500)' }} />
            功能点（FP）审核
          </h3>
          <FPSRRViewer reqId={id!} productId={detail.product_id} status={detail.status} onChanged={refreshRevisionBaseline} />
        </div>

        {/* 功能概述 */}
        {reqData.summary && (
          <FieldEditor
            label="功能概述"
            fieldPath="requirement.summary"
            value={reqData.summary}
            valueType="string"
            onSave={saveSrrField}
            disabled={detail.status === 'archived'}
            render={(v) => (
              <p className="text-sm leading-relaxed" style={{ color: 'var(--foreground-secondary)' }}>
                {String(v)}
              </p>
            )}
          />
        )}

        {/* 前置条件 */}
        <FieldEditor
          label="前置条件"
          fieldPath="preconditions"
          value={srr.preconditions || []}
          valueType="string[]"
          placeholder="一行一条前置条件"
          onSave={saveSrrField}
          disabled={detail.status === 'archived'}
          render={(v) => {
            const list = Array.isArray(v) ? v : []
            return (
              <>
                {list.length > 0 ? (
                  <ul className="list-disc list-inside text-sm space-y-1" style={{ color: 'var(--foreground-secondary)' }}>
                    {list.map((p: string, i: number) => <li key={i}>{p}</li>)}
                  </ul>
                ) : <p className="text-xs italic" style={{ color: 'var(--foreground-tertiary)' }}>点击编辑添加前置条件</p>}
              </>
            )
          }}
        />

        {/* 正常流程 */}
        <FieldEditor
          label="正常流程"
          fieldPath="flows.happy_path.steps"
          value={flows.happy_path?.steps || []}
          valueType="object[]"
          placeholder={'[{"order":1,"action":"用户操作","expect":"系统响应"}]'}
          onSave={saveSrrField}
          disabled={detail.status === 'archived'}
          render={(v) => {
            const steps = Array.isArray(v) ? v : []
            if (!steps.length) return <p className="text-xs italic" style={{ color: 'var(--foreground-tertiary)' }}></p>
            return (
              <div className="space-y-2">
                {steps.map((s: any, i: number) => (
                  <div key={i} className="flex gap-3 p-3" style={{ background: 'rgba(0, 122, 255, 0.06)', borderRadius: 'calc(var(--radius) * 0.6)' }}>
                    <span className="w-6 h-6 rounded-full text-xs font-bold flex items-center justify-center flex-shrink-0 mt-0.5" style={{ background: 'rgba(0, 122, 255, 0.12)', color: 'var(--brand-500)' }}>{s.order || i + 1}</span>
                    <div className="flex-1 min-w-0">
                      <p className="text-sm" style={{ color: 'var(--foreground-secondary)' }}>{s.action}</p>
                      <p className="text-xs mt-0.5" style={{ color: 'var(--foreground-tertiary)' }}>→ {s.expect}</p>
                    </div>
                  </div>
                ))}
              </div>
            )
          }}
        />

        {/* 异常流程 */}
        <FieldEditor
          label="异常流程"
          fieldPath="flows.alternate_flows"
          value={flows.alternate_flows || []}
          valueType="object[]"
          placeholder={'[{"name":"异常名","trigger":"触发条件","expect":"预期结果"}]'}
          onSave={saveSrrField}
          disabled={detail.status === 'archived'}
          render={(v) => {
            const afs = Array.isArray(v) ? v : []
            if (!afs.length) return <p className="text-xs italic" style={{ color: 'var(--foreground-tertiary)' }}></p>
            return (
              <div className="space-y-2">
                {afs.map((f: any, i: number) => (
                  <div key={i} className="flex gap-3 p-3" style={{ background: 'var(--state-warning-surface)', borderRadius: 'calc(var(--radius) * 0.6)' }}>
                    <AlertCircle className="w-5 h-5 flex-shrink-0 mt-0.5" style={{ color: 'var(--state-warning)' }} />
                    <div>
                      <p className="text-sm font-medium" style={{ color: 'var(--foreground-secondary)' }}>{f.name}</p>
                      <p className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>触发: {f.trigger} → {f.expect}</p>
                    </div>
                  </div>
                ))}
              </div>
            )
          }}
        />

        {/* 跨功能点交互规则（v2.32: 替代原 boundary_conditions，总 SRR 核心职责；v2.34: 4 个子字段全部可编辑；v2.38: 功能点字段改下拉选择） */}
        {(() => {
          const cfi = srr.cross_fp_interactions || {}
          const flows = Array.isArray(cfi.cross_fp_flows) ? cfi.cross_fp_flows : []
          const sharedRules = Array.isArray(cfi.shared_business_rules) ? cfi.shared_business_rules : []
          const contracts = Array.isArray(cfi.fp_contracts) ? cfi.fp_contracts : []
          const globalConstraints = Array.isArray(cfi.global_constraints) ? cfi.global_constraints : []
          const isArchived = detail.status === 'archived'
          const fpOpts = { fps: fpOptions }
          return (
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <GitBranch className="w-4 h-4" style={{ color: 'var(--brand-500)' }} />
                <h4 className="text-sm font-semibold" style={{ color: 'var(--foreground-secondary)' }}>跨功能点交互规则</h4>
              </div>
              {/* 功能点调用顺序（可编辑） */}
              <FieldEditor
                label="功能点调用顺序"
                fieldPath="cross_fp_interactions.cross_fp_flows"
                value={flows}
                valueType="object[]"
                placeholder='[{"name":"登录后加载菜单","source_fp":"用户登录","target_fp":"菜单加载","interaction_type":"sequence","description":"登录成功触发菜单加载"}]'
                onSave={saveSrrField}
                disabled={isArchived}
                optionsData={fpOpts}
                render={(v) => {
                  const list = Array.isArray(v) ? v : []
                  if (!list.length) return <p className="text-xs italic" style={{ color: 'var(--foreground-tertiary)' }}>点击编辑添加功能点调用顺序</p>
                  return (
                    <div className="space-y-1.5">
                      {list.map((f: any, i: number) => (
                        <div key={i} className="flex items-center gap-2 text-sm p-2" style={{ color: 'var(--foreground-secondary)', background: 'rgba(0, 122, 255, 0.06)', borderRadius: 'calc(var(--radius) * 0.5)' }}>
                          <span className="px-1.5 py-0.5 rounded text-xs font-medium" style={{ background: 'rgba(0, 122, 255, 0.12)', color: 'var(--brand-500)' }}>{f.source_fp || f.from || '?'}</span>
                          <span style={{ color: 'var(--foreground-tertiary)' }}>→</span>
                          <span className="px-1.5 py-0.5 rounded text-xs font-medium" style={{ background: 'rgba(0, 122, 255, 0.12)', color: 'var(--brand-500)' }}>{f.target_fp || f.to || '?'}</span>
                          {(f.description || f.contract) && <span className="text-xs ml-1" style={{ color: 'var(--foreground-tertiary)' }}>{f.description || f.contract}</span>}
                        </div>
                      ))}
                    </div>
                  )
                }}
              />
              {/* 共享业务规则（可编辑，v2.34 替代原顶层 business_rules） */}
              <FieldEditor
                label="共享业务规则"
                fieldPath="cross_fp_interactions.shared_business_rules"
                value={sharedRules}
                valueType="object[]"
                placeholder='[{"id":"SBR-01","rule":"全链路TLS加密","applied_fps":["用户登录","数据查询"]}]'
                onSave={saveSrrField}
                disabled={isArchived}
                optionsData={fpOpts}
                render={(v) => {
                  const list = Array.isArray(v) ? v : []
                  if (!list.length) return <p className="text-xs italic" style={{ color: 'var(--foreground-tertiary)' }}>点击编辑添加跨功能点共享规则</p>
                  return (
                    <div className="space-y-1.5">
                      {list.map((r: any, i: number) => (
                        <div key={i} className="flex items-center gap-2 text-sm" style={{ color: 'var(--foreground-secondary)' }}>
                          <CheckCircle2 className="w-3.5 h-3.5 flex-shrink-0" style={{ color: 'var(--state-success)' }} />
                          <span>{typeof r === 'string' ? r : `${r.id || ''}: ${r.rule || ''}`}</span>
                          {typeof r === 'object' && Array.isArray(r.applied_fps) && r.applied_fps.length > 0 && (
                            <span className="text-[10px] px-1 rounded" style={{ background: 'var(--state-success-surface)', color: 'var(--state-success)' }}>适用于 {r.applied_fps.length} 个功能点</span>
                          )}
                        </div>
                      ))}
                    </div>
                  )
                }}
              />
              {/* 功能点间契约（可编辑） */}
              <FieldEditor
                label="功能点间契约"
                fieldPath="cross_fp_interactions.fp_contracts"
                value={contracts}
                valueType="object[]"
                placeholder='[{"provider_fp":"用户登录","consumer_fp":"菜单加载","data_fields":["user_id","token"],"contract_desc":"登录返回 token"}]'
                onSave={saveSrrField}
                disabled={isArchived}
                optionsData={fpOpts}
                render={(v) => {
                  const list = Array.isArray(v) ? v : []
                  if (!list.length) return <p className="text-xs italic" style={{ color: 'var(--foreground-tertiary)' }}>点击编辑添加功能点间契约</p>
                  return (
                    <div className="space-y-1.5">
                      {list.map((c: any, i: number) => (
                        <div key={i} className="text-sm p-2" style={{ color: 'var(--foreground-secondary)', background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.5)' }}>
                          <span className="font-medium">{c.provider_fp || c.fp_a || '?'}</span>
                          <span className="mx-1" style={{ color: 'var(--foreground-tertiary)' }}>→</span>
                          <span className="font-medium">{c.consumer_fp || c.fp_b || '?'}</span>
                          {(c.contract_desc || c.contract) && <p className="text-xs mt-0.5" style={{ color: 'var(--foreground-tertiary)' }}>{c.contract_desc || c.contract}</p>}
                        </div>
                      ))}
                    </div>
                  )
                }}
              />
              {/* 全局约束（可编辑，v2.38: 改用列表式编辑） */}
              <FieldEditor
                label="全局约束"
                fieldPath="cross_fp_interactions.global_constraints"
                value={globalConstraints}
                valueType="string[]"
                placeholder='一行一条全局约束，如：所有接口响应时间 ≤ 200ms'
                onSave={saveSrrField}
                disabled={isArchived}
                render={(v) => {
                  const list = Array.isArray(v) ? v : []
                  if (!list.length) return <p className="text-xs italic" style={{ color: 'var(--foreground-tertiary)' }}>点击编辑添加全局约束</p>
                  return (
                    <div className="space-y-1.5">
                      {list.map((g: any, i: number) => (
                        <div key={i} className="flex items-center gap-2 text-sm" style={{ color: 'var(--foreground-secondary)' }}>
                          <AlertCircle className="w-3.5 h-3.5 flex-shrink-0" style={{ color: 'var(--state-warning)' }} />
                          <span>{typeof g === 'string' ? g : (g.constraints || g.rule || JSON.stringify(g))}</span>
                        </div>
                      ))}
                    </div>
                  )
                }}
              />
            </div>
          )
        })()}

        {/* v2.34: 总 SRR 不再展示顶层 business_rules（已迁移到 cross_fp_interactions.shared_business_rules） */}

        {/* 全局非功能需求基线（v2.34: 新增可编辑展示；v2.38: 改为列表式编辑，对象↔数组转换；v2.41: 修复展示模式 render 不处理数组导致显示为空的问题） */}
        <FieldEditor
          label="全局非功能需求基线"
          fieldPath="non_functional"
          value={Object.entries(srr.non_functional || {}).filter(([, v]: any) => v).map(([k, v]: any) => ({ category: k, requirement: String(v) }))}
          valueType="object[]"
          placeholder='[{"category":"performance","requirement":"响应时间 ≤ 200ms"}]'
          onSave={saveNonFunctional}
          disabled={detail.status === 'archived'}
          render={(v) => {
            // v 是数组 [{category, requirement}]（与 valueType="object[]" 一致）
            const list = Array.isArray(v) ? v : []
            if (!list.length) return <p className="text-xs italic" style={{ color: 'var(--foreground-tertiary)' }}>点击编辑添加全局非功能需求（性能/安全/兼容性/可访问性）</p>
            const labels: Record<string, string> = {
              performance: '性能', security: '安全',
              compatibility: '兼容性', accessibility: '可访问性',
              reliability: '可靠性', maintainability: '可维护性',
              scalability: '可扩展性', usability: '易用性',
            }
            return (
              <div className="space-y-1.5">
                {list.map((item: any, i: number) => {
                  const k = item?.category || ''
                  const val = item?.requirement || ''
                  return (
                    <div key={i} className="flex items-start gap-2 text-sm" style={{ color: 'var(--foreground-secondary)' }}>
                      <span className="px-1.5 py-0.5 rounded text-[10px] font-medium flex-shrink-0 mt-0.5" style={{ background: 'rgba(0, 122, 255, 0.08)', color: 'var(--brand-500)' }}>
                        {labels[k] || k}
                      </span>
                      <span>{String(val)}</span>
                    </div>
                  )
                })}
              </div>
            )
          }}
        />

        {/* 历史数据影响分析（v2.32: 替代原 data_constraints + acceptance_criteria；v2.34: 支持编辑；v2.40: 字段精简为 4 核心维度，对齐业界数据迁移影响分析框架） */}
        {(() => {
          const ldi = (srr.legacy_data_impact && typeof srr.legacy_data_impact === 'object') ? srr.legacy_data_impact : {}
          return (
            <div className="space-y-3 p-4" style={{ background: 'var(--state-warning-surface)', border: '1px solid var(--state-warning)', borderRadius: 'calc(var(--radius) * 0.6)' }}>
              <div className="flex items-center gap-2">
                <Database className="w-4 h-4" style={{ color: 'var(--state-warning)' }} />
                <h4 className="text-sm font-semibold" style={{ color: 'var(--foreground-secondary)' }}>历史数据影响分析</h4>
                {ldi.is_optimization && (
                  <span className="px-1.5 py-0.5 rounded text-[10px] font-medium" style={{ background: 'var(--state-warning-surface)', color: 'var(--state-warning)' }}>优化需求</span>
                )}
              </div>
              {/* 维度1：影响范围（合并原 impact_scope + data_inventory） */}
              <FieldEditor
                label="影响范围"
                fieldPath="legacy_data_impact.impact_scope"
                value={ldi.impact_scope || ''}
                valueType="string"
                placeholder="受影响的功能点、数据实体（主数据/在途数据/历史数据/日志）、上下游系统、用户角色"
                onSave={saveSrrField}
                disabled={false}
                render={(v) => v ? <p className="text-sm whitespace-pre-wrap" style={{ color: 'var(--foreground-secondary)' }}>{String(v)}</p> : <p className="text-xs italic" style={{ color: 'var(--foreground-tertiary)' }}>点击编辑添加影响范围（含数据盘点）</p>}
              />
              {/* 维度2：迁移方案（合并原 migration_strategy + field_mapping + compatibility_analysis） */}
              <FieldEditor
                label="迁移方案"
                fieldPath="legacy_data_impact.migration_plan"
                value={ldi.migration_plan || ''}
                valueType="string"
                placeholder="含迁移策略（全量/增量/双写/停机）、字段映射（老→新）、兼容性分析（格式/编码/关联完整性）"
                onSave={saveSrrField}
                disabled={false}
                render={(v) => v ? <p className="text-sm whitespace-pre-wrap" style={{ color: 'var(--foreground-secondary)' }}>{String(v)}</p> : <p className="text-xs italic" style={{ color: 'var(--foreground-tertiary)' }}>点击编辑添加迁移方案（含字段映射、兼容性）</p>}
              />
              {/* 维度3：验证与回滚（合并原 rollback_strategy + verification_method） */}
              <FieldEditor
                label="验证与回滚"
                fieldPath="legacy_data_impact.rollback_and_validation"
                value={ldi.rollback_and_validation || ''}
                valueType="string"
                placeholder="回滚策略（备份/回滚窗口/触发条件）+ 验证方法（数量校验/业务关联校验/并行比对）"
                onSave={saveSrrField}
                disabled={false}
                render={(v) => v ? <p className="text-sm whitespace-pre-wrap" style={{ color: 'var(--foreground-secondary)' }}>{String(v)}</p> : <p className="text-xs italic" style={{ color: 'var(--foreground-tertiary)' }}>点击编辑添加验证与回滚方案</p>}
              />
              {/* 维度4：风险与缓解（独立保留） */}
              <FieldEditor
                label="风险与缓解"
                fieldPath="legacy_data_impact.risks"
                value={Array.isArray(ldi.risks) ? ldi.risks : []}
                valueType="string[]"
                placeholder="业务逻辑丢失、数据不一致、停机超时"
                onSave={saveSrrField}
                disabled={false}
                render={(v) => {
                  const list = Array.isArray(v) ? v : []
                  if (!list.length) return <p className="text-xs italic" style={{ color: 'var(--foreground-tertiary)' }}>点击编辑添加风险与缓解</p>
                  return (
                    <ul className="list-disc list-inside text-sm space-y-0.5" style={{ color: 'var(--foreground-secondary)' }}>
                      {list.map((r: string, i: number) => <li key={i}>{r}</li>)}
                    </ul>
                  )
                }}
              />
            </div>
          )
        })()}
      </div>
    )
  }

  // ==========================================================================
  // 渲染：原始文本（Markdown / Word / 纯文本，支持多文件切换）
  // ==========================================================================
  function renderRawText() {
    if (!detail.raw_text && !detail.supplementary_text) {
      return (
        <div className="text-center py-12">
          <FileText className="w-12 h-12 mx-auto mb-3" style={{ color: 'var(--foreground-quaternary)' }} />
          <p style={{ color: 'var(--foreground-tertiary)' }}>暂无原始文本内容</p>
        </div>
      )
    }

    const entry = fileEntries[selectedFileIdx]
    if (!entry) {
      return (
        <div className="text-center py-12">
          <FileText className="w-12 h-12 mx-auto mb-3" style={{ color: 'var(--foreground-quaternary)' }} />
          <p style={{ color: 'var(--foreground-tertiary)' }}>未找到文件内容</p>
        </div>
      )
    }

    const isMarkdown = entry.source_type === 'markdown'
    const isDocx = entry.source_type === 'docx'
    const isSupplementary = entry.is_supplementary

    return (
      <div>
        {/* 多文件切换药片 */}
        {fileEntries.length > 1 && (
          <div className="flex items-center gap-2 mb-4 flex-wrap">
            {fileEntries.map((f, i) => {
              const selected = i === selectedFileIdx
              const supp = f.is_supplementary
              return (
                <button
                  key={i}
                  onClick={() => { setSelectedFileIdx(i) }}
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium border transition-all duration-150"
                  style={{
                    borderRadius: 'calc(var(--radius) * 0.5)',
                    background: selected
                      ? (supp ? 'var(--state-warning-surface)' : 'rgba(0, 122, 255, 0.08)')
                      : 'var(--background)',
                    borderColor: selected
                      ? (supp ? 'var(--state-warning)' : 'var(--brand-300)')
                      : 'var(--border)',
                    color: selected
                      ? (supp ? 'var(--state-warning)' : 'var(--brand-700)')
                      : 'var(--foreground-tertiary)',
                    boxShadow: selected ? 'var(--shadow-apple-sm)' : 'none',
                  }}
                >
                  {f.is_supplementary ? <FilePlus className="w-3.5 h-3.5" /> :
                   f.source_type === 'markdown' ? <FileCode className="w-3.5 h-3.5" /> :
                   f.source_type === 'docx' ? <FileType className="w-3.5 h-3.5" /> :
                   <FileText className="w-3.5 h-3.5" />}
                  {f.filename}
                  {f.is_supplementary && <span className="text-[10px]" style={{ color: 'var(--state-warning)' }}>补充</span>}
                </button>
              )
            })}
          </div>
        )}

        {/* 当前文件来源标识 */}
        <div className="flex items-center gap-2 mb-4 px-1">
          {isSupplementary ? (
            <span className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-medium" style={{ background: 'var(--state-warning-surface)', color: 'var(--state-warning)' }}>
              <FilePlus className="w-3.5 h-3.5" /> 补充文件
            </span>
          ) : isMarkdown ? (
            <span className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-medium" style={{ background: 'rgba(0, 122, 255, 0.08)', color: 'var(--brand-700)' }}>
              <FileCode className="w-3.5 h-3.5" /> Markdown
            </span>
          ) : isDocx ? (
            <span className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-medium" style={{ background: 'rgba(255, 149, 0, 0.1)', color: 'var(--state-warning)' }}>
              <FileType className="w-3.5 h-3.5" /> Word 文档
            </span>
          ) : (
            <span className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-medium" style={{ background: 'var(--background-muted)', color: 'var(--foreground-secondary)' }}>
              <FileText className="w-3.5 h-3.5" /> 纯文本
            </span>
          )}
          <span className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>
            {entry.text.length} 字符 · {entry.text.split('\n').length} 行
          </span>
          {fileEntries.length > 1 && (
            <span className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>
              · 文件 {selectedFileIdx + 1}/{fileEntries.length}
            </span>
          )}
        </div>

        {/* Markdown 渲染（带完整格式支持） */}
        {isMarkdown ? (
          <div className="prose prose-sm max-w-none
            prose-headings:font-semibold
            prose-h1:text-xl prose-h1:border-b prose-h1:pb-2 prose-h1:mb-4
            prose-h2:text-lg prose-h2:border-b prose-h2:pb-1 prose-h2:mb-3
            prose-a:no-underline hover:prose-a:underline
            prose-code:px-1.5 prose-code:py-0.5 prose-code:rounded-md prose-code:text-xs
            prose-pre:p-0
            prose-img:rounded-xl prose-img:border
            prose-table:text-sm prose-table:border-collapse
            prose-th:px-4 prose-th:py-2.5 prose-th:text-left prose-th:text-xs prose-th:font-semibold prose-th:border-b
            prose-td:px-4 prose-td:py-2.5 prose-td:border-b
            prose-blockquote:border-l-4 prose-blockquote:py-1 prose-blockquote:px-4 prose-blockquote:rounded-r-lg prose-blockquote:not-italic
            prose-ul:list-disc prose-ul:pl-5
            prose-ol:list-decimal prose-ol:pl-5
            prose-li:my-0.5
          "
            style={{
              ['--tw-prose-headings' as any]: 'var(--foreground)',
              ['--tw-prose-body' as any]: 'var(--foreground-secondary)',
              ['--tw-prose-bold' as any]: 'var(--foreground)',
              ['--tw-prose-links' as any]: 'var(--brand-500)',
              ['--tw-prose-code' as any]: 'var(--foreground)',
              ['--tw-prose-pre-bg' as any]: 'var(--background-muted)',
              ['--tw-prose-pre-code' as any]: 'var(--foreground)',
              ['--tw-prose-quotes' as any]: 'var(--foreground-secondary)',
              ['--tw-prose-quote-borders' as any]: 'var(--brand-300)',
              ['--tw-prose-bullets' as any]: 'var(--foreground-tertiary)',
              ['--tw-prose-hr' as any]: 'var(--border)',
              ['--tw-prose-th-borders' as any]: 'var(--border)',
              ['--tw-prose-td-borders' as any]: 'var(--border)',
              ['--tw-prose-counts' as any]: 'var(--foreground-tertiary)',
              ['--tw-prose-invert-bullets' as any]: 'var(--foreground-tertiary)',
            }}
          >
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              rehypePlugins={[rehypeRaw]}
              components={{
                code({ className, children, ...props }) {
                  const isInline = !className
                  if (isInline) {
                    return <code className="px-1.5 py-0.5 rounded-md text-xs" style={{ background: 'var(--background-muted)', fontFamily: 'var(--font-mono)', color: 'var(--foreground)' }} {...props}>{children}</code>
                  }
                  return <CodeBlock className={className} children={children} {...props} />
                },
                table({ children, ...props }) {
                  return <Table children={children} {...props} />
                },
              }}
            >
              {entry.text}
            </ReactMarkdown>
          </div>
        ) : (
          /* Word / 纯文本 — 统一两栏布局（行号列 + 内容列），表格渲染为 HTML table，图片描述折叠 */
          <div className="overflow-hidden" style={{ background: 'var(--background)', border: '1px solid var(--border)', borderRadius: 'calc(var(--radius) * 0.6)', boxShadow: 'var(--shadow-apple-sm)' }}>
            {(() => {
              const imageMap = new Map<number, string>()
              ;(detail.raw_images || []).forEach(img => imageMap.set(img.index, img.data_url))
              const blocks = splitTextIntoBlocks(entry.text)

              // 公共行号列组件
              const LineNumberCol = ({ start, end }: { start: number; end?: number }) => (
                <div className="select-none py-3 pl-3 pr-2 text-right text-xs min-w-[3rem] flex flex-col justify-start" style={{ color: 'var(--foreground-quaternary)', background: 'var(--background-muted)', borderRight: '1px solid var(--border)', fontFamily: 'var(--font-mono)' }}>
                  <div>{start + 1}</div>
                  {end != null && end > start + 1 && (
                    <>
                      <div className="leading-tight" style={{ color: 'var(--foreground-quaternary)' }}>{'\u22EE'}</div>
                      <div>{end}</div>
                    </>
                  )}
                </div>
              )

              return (
                <div className="text-sm leading-relaxed">
                  {blocks.map((block, bi) => {
                    if (block.type === 'table') {
                      // 表格块：两栏布局，行号列显示行号范围，内容列渲染 HTML 表格
                      return (
                        <div key={bi} className="flex text-sm leading-relaxed first:border-t-0" style={{ borderTop: '1px solid var(--border)' }}>
                          <LineNumberCol start={block.startLine} end={block.startLine + block.lineCount} />
                          <div className="flex-1 py-2 px-4 overflow-x-auto">
                            <ReactMarkdown
                              remarkPlugins={[remarkGfm]}
                              rehypePlugins={[rehypeRaw]}
                              components={{
                                table: ({ children }) => (
                                  <div className="overflow-x-auto my-1" style={{ borderRadius: 'calc(var(--radius) * 0.5)', border: '1px solid var(--border)' }}>
                                    <table className="w-full text-sm">{children}</table>
                                  </div>
                                ),
                                th: ({ children, ...props }: any) => (
                                  <th {...props} className="px-3 py-2 text-left text-xs font-semibold" style={{ color: 'var(--foreground-tertiary)', borderBottom: '1px solid var(--border)', background: 'var(--background-muted)' }}>
                                    {children}
                                  </th>
                                ),
                                td: ({ children, ...props }: any) => (
                                  <td {...props} className="px-3 py-2" style={{ borderBottom: '1px solid var(--border)', color: 'var(--foreground-secondary)' }}>
                                    {children}
                                  </td>
                                ),
                                tr: ({ children, ...props }: any) => (
                                  <tr {...props}>{children}</tr>
                                ),
                              }}
                            >
                              {block.content}
                            </ReactMarkdown>
                          </div>
                        </div>
                      )
                    }

                    if (block.type === 'image_desc') {
                      // 图片描述块：两栏布局，行号列显示行号，内容列将原始图片+多模态描述合并到同一折叠栏（v2.47 修复内容分成两块问题）
                      const imgIdx = block.imageIndex
                      const imgUrl = imgIdx != null ? imageMap.get(imgIdx) : undefined
                      const descLines = block.lines
                      const imgLabel = descLines[0]?.replace(/^【|\】$/g, '') || `图片${imgIdx ?? ''}描述`
                      return (
                        <div key={bi} className="flex text-sm leading-relaxed" style={{ borderTop: '1px solid var(--border)' }}>
                          <LineNumberCol start={block.startLine} end={block.startLine + descLines.length} />
                          <div className="flex-1 py-2 px-4">
                            {/* v2.47: 原始图片 + 多模态识别内容统一折叠，避免内容分成两块、避免部分内容不在折叠栏 */}
                            <details className="group overflow-hidden" style={{ background: 'rgba(0, 122, 255, 0.05)', border: '1px solid rgba(0, 122, 255, 0.2)', borderRadius: 'calc(var(--radius) * 0.5)' }}>
                              <summary className="cursor-pointer list-none flex items-center gap-2 px-3 py-2 text-xs font-medium select-none" style={{ color: 'var(--brand-700)' }}>
                                <span className="inline-block transition-transform group-open:rotate-90" style={{ color: 'var(--brand-400)' }}>{'\u25B6'}</span>
                                <ImageIcon className="w-3.5 h-3.5" />
                                <span>{imgLabel}</span>
                                <span className="font-normal" style={{ color: 'var(--brand-400)' }}>（{descLines.length} 行，点击{'\u5C55'}开）</span>
                              </summary>
                              <div className="px-3 pb-3 pt-2 space-y-2" style={{ borderTop: '1px solid rgba(0, 122, 255, 0.1)', background: 'rgba(255, 255, 255, 0.6)' }}>
                                {/* 原始图片（移入折叠栏内） */}
                                {imgUrl && (
                                  <div className="block p-2" style={{ background: 'var(--background-muted)', border: '1px solid var(--border)', borderRadius: 'calc(var(--radius) * 0.5)' }}>
                                    <div className="text-xs mb-1 flex items-center gap-1" style={{ color: 'var(--foreground-tertiary)' }}>
                                      <ImageIcon className="w-3.5 h-3.5" /> 原始图片（图片{imgIdx}）
                                    </div>
                                    <img
                                      src={imgUrl}
                                      alt={`原始图片${imgIdx}`}
                                      className="max-w-full"
                                      style={{ borderRadius: 'calc(var(--radius) * 0.5)', border: '1px solid var(--border)', maxHeight: '400px' }}
                                    />
                                  </div>
                                )}
                                {/* 多模态识别内容 */}
                                <pre className="text-xs whitespace-pre-wrap leading-relaxed" style={{ color: 'var(--foreground-secondary)', fontFamily: 'var(--font-sans)' }}>
                                  {descLines.join('\n')}
                                </pre>
                              </div>
                            </details>
                          </div>
                        </div>
                      )
                    }

                    // 文本块：行号列 + 纯文本
                    return (
                      <div key={bi} className="flex text-sm leading-relaxed first:border-t-0" style={{ borderTop: '1px solid var(--border)' }}>
                        <LineNumberCol start={block.startLine} />
                        <div className="flex-1 py-3 px-4 whitespace-pre-wrap text-sm leading-relaxed" style={{ color: 'var(--foreground-secondary)', fontFamily: 'var(--font-sans)' }}>
                          {block.lines.join('\n')}
                        </div>
                      </div>
                    )
                  })}
                </div>
              )
            })()}
          </div>
        )}
      </div>
    )
  }

  // ==========================================================================
  // 渲染：知识库查询记录
  // ==========================================================================
  function renderKbLogs() {
    // v2.8.67: 过滤掉 kb_query_gen / kb_query_gen_empty 汇总条目--
    // 这两类是「本次共生成 N 条查询」的统计占位，并非真实 KB 问答，展示在查询页面会误导用户。
    // 知识库查询页面只展示真实问答（kb_query 已解答 / kb_unresolved 未解决）及状态检查等有意义的记录。
    const _HIDDEN_TYPES = new Set(['kb_query_gen', 'kb_query_gen_empty'])
    const logs = (detail.kb_query_logs || []).filter((l: any) => !_HIDDEN_TYPES.has(l?.type))

    if (logs.length === 0) {
      return (
        <div className="text-center py-12">
          <Database className="w-12 h-12 mx-auto mb-3" style={{ color: 'var(--foreground-quaternary)' }} />
          <p className="font-medium" style={{ color: 'var(--foreground-tertiary)' }}>无需调用知识库</p>
          <p className="text-sm mt-1" style={{ color: 'var(--foreground-tertiary)' }}>当前需求分析阶段未触发知识库查询</p>
        </div>
      )
    }

    const typeIcon: Record<string, typeof Database> = {
      similar_requirements: BookOpen,
      similar_cases: FileText,
      patterns: Search,
      check_kb_status: Database,
      pg_enrich: Database,
      kb_augment: BookOpen,
      kb_query: Search,
      kb_unresolved: AlertCircle,
      // v2.8.1: 补全后端新增 type 的图标映射
      kb_query_gen: Search,
      kb_query_gen_empty: Search,
      vector_similarity: BookOpen,
    }

    return (
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <p className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>共 {logs.length} 条查询记录</p>
        </div>

        {logs.map((log, i) => {
          const Icon = typeIcon[log.type] || Database
          const hasResults = (log.result_count ?? 0) > 0 && !log.error
          const isQA = log.type === 'kb_query' || log.type === 'kb_unresolved'

          const cardStyle: React.CSSProperties = isQA
            ? (log.type === 'kb_query'
                ? { border: '1px solid var(--state-success)', background: 'rgba(52, 199, 89, 0.05)' }
                : { border: '1px solid var(--state-warning)', background: 'rgba(255, 149, 0, 0.05)' })
            : { border: '1px solid var(--border)', background: 'transparent' }

          const headerStyle: React.CSSProperties = isQA
            ? { borderBottom: '1px solid rgba(52, 199, 89, 0.2)' }
            : { borderBottom: '1px solid var(--border)', background: 'var(--background-muted)' }

          return (
            <div key={i} className="overflow-hidden" style={{ borderRadius: 'calc(var(--radius) * 0.6)', ...cardStyle }}>
              {/* 查询头部 */}
              <div className="flex items-center justify-between px-4 py-3" style={headerStyle}>
                <div className="flex items-center gap-2">
                  <Icon className="w-4 h-4" style={{ color: log.type === 'kb_unresolved' ? 'var(--state-warning)' : 'var(--brand-500)' }} />
                  <span className="text-sm font-medium" style={{ color: 'var(--foreground-secondary)' }}>{log.label}</span>
                  {isQA && (
                    <span className="text-xs px-2 py-0.5 rounded-full font-medium" style={{
                      background: log.type === 'kb_query' ? 'rgba(52, 199, 89, 0.15)' : 'rgba(255, 149, 0, 0.15)',
                      color: log.type === 'kb_query' ? 'var(--state-success)' : 'var(--state-warning)'
                    }}>
                      {log.type === 'kb_query' ? `✓ KB 已解答` : `✗ 需人工澄清`}
                    </span>
                  )}
                  {!isQA && (
                    <span className="text-xs px-2 py-0.5 rounded-full font-medium" style={{
                      background: hasResults ? 'var(--state-success-surface)' : 'var(--background-muted)',
                      color: hasResults ? 'var(--state-success)' : 'var(--foreground-tertiary)'
                    }}>
                      {hasResults ? `找到 ${log.result_count} 条` : '无匹配结果'}
                    </span>
                  )}
                </div>
                <span className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>{log.timestamp}</span>
              </div>

              {/* Q&A 内容 */}
              {isQA && (
                <div className="px-4 py-3 space-y-3" style={{ background: 'var(--background)' }}>
                  {/* 问题 */}
                  <div>
                    <span className="text-xs font-medium" style={{ color: 'var(--brand-500)' }}>❓ 向知识库提问：</span>
                    <p className="text-sm mt-0.5 leading-relaxed font-medium" style={{ color: 'var(--foreground)' }}>{log.query}</p>
                  </div>
                  {/* 答案 */}
                  {log.answer && (
                    <div className="pl-3" style={{ borderLeft: '2px solid var(--state-success)' }}>
                      <span className="text-xs font-medium" style={{ color: 'var(--state-success)' }}>💡 知识库回答：</span>
                      <p className="text-sm mt-0.5 leading-relaxed whitespace-pre-line" style={{ color: 'var(--foreground-secondary)' }}>{log.answer}</p>
                    </div>
                  )}
                  {/* 来源 */}
                  {log.sources && log.sources.length > 0 && (
                    <div className="pt-1 space-y-2">
                      <span className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>参考来源（{log.sources.length}条）：</span>
                      {log.sources.map((s: any, si: number) => {
                        const valueStr = formatSourceValue(s.value)
                        // v2.8.1: 相似度统一为百分比显示，兼容 0-1 浮点数
                        const simPct = typeof s.similarity === 'number' ? (s.similarity * 100).toFixed(0) : '—'
                        return (
                          <details key={si} className="group">
                            <summary className="cursor-pointer text-[11px] px-2 py-0.5 rounded-full inline-flex items-center gap-1 transition-colors" style={{ background: 'var(--background-muted)', color: 'var(--foreground-tertiary)' }}>
                              {s.id} · 相似度 {simPct}%
                              <span className="group-open:hidden" style={{ color: 'var(--foreground-quaternary)' }}>▸</span>
                              <span className="hidden group-open:inline" style={{ color: 'var(--foreground-quaternary)' }}>▾</span>
                            </summary>
                            {valueStr && (
                              <pre className="mt-2 p-2.5 text-[11px] leading-relaxed whitespace-pre-wrap overflow-auto max-h-48" style={{ background: 'var(--background-muted)', border: '1px solid var(--border)', borderRadius: 'calc(var(--radius) * 0.5)', color: 'var(--foreground-secondary)' }}>{valueStr}</pre>
                            )}
                          </details>
                        )
                      })}
                    </div>
                  )}
                </div>
              )}

              {/* 非 Q&A 的查询内容 */}
              {!isQA && (<>
              {/* 查询内容 */}
              <div className="px-4 py-3" style={{ background: 'var(--background)' }}>
                <div className="mb-2">
                  <span className="text-xs font-medium" style={{ color: 'var(--foreground-tertiary)' }}>查询意图：</span>
                  <p className="text-sm mt-0.5 leading-relaxed" style={{ color: 'var(--foreground-secondary)' }}>{log.query || '—'}</p>
                </div>
                {log.query_detail && (
                  <div className="mt-1.5 pl-3" style={{ borderLeft: '2px solid var(--border)' }}>
                    <p className="text-xs leading-relaxed" style={{ color: 'var(--foreground-tertiary)' }}>{log.query_detail}</p>
                  </div>
                )}

                {/* 错误信息 */}
                {log.error && (
                  <div className="p-2.5 text-xs mb-2" style={{ background: 'var(--state-error-surface)', border: '1px solid var(--state-error)', borderRadius: 'calc(var(--radius) * 0.5)', color: 'var(--state-error)' }}>
                    <AlertCircle className="w-3 h-3 inline-block mr-1 -mt-0.5" />
                    {log.error}
                  </div>
                )}

                {/* 匹配结果列表（v2.8.1: 统一使用 sources 字段，兼容旧 results） */}
                {hasResults && (() => {
                  const results = log.sources || log.results || []
                  if (results.length === 0) return null
                  return (
                  <div>
                    <span className="text-xs font-medium" style={{ color: 'var(--foreground-tertiary)' }}>匹配结果：</span>
                    <div className="mt-1.5 space-y-1.5">
                      {results.map((r: any, j: number) => {
                        const sim = typeof r.similarity === 'number' ? r.similarity : 0
                        const simStyle: React.CSSProperties = sim >= 0.8
                          ? { background: 'var(--state-success-surface)', color: 'var(--state-success)' }
                          : sim >= 0.6
                            ? { background: 'var(--state-warning-surface)', color: 'var(--state-warning)' }
                            : { background: 'var(--background-muted)', color: 'var(--foreground-tertiary)' }
                        // v2.8.1: 后端 sources 含 req_id（全局检索）或 id（其他），统一显示
                        const displayId = r.req_id || r.id || '—'
                        return (
                          <div key={j} className="flex items-center justify-between p-2.5" style={{ background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.5)' }}>
                            <div className="min-w-0 flex-1">
                              <p className="text-xs font-medium truncate" style={{ color: 'var(--foreground-secondary)' }}>
                                {r.title || r.name || displayId}
                              </p>
                              {r.summary && (
                                <p className="text-xs truncate mt-0.5" style={{ color: 'var(--foreground-tertiary)' }}>{r.summary}</p>
                              )}
                              {r.go_live_date && (
                                <span className="text-[10px] mt-0.5 inline-block mr-1" style={{ color: 'var(--foreground-tertiary)' }}>上线: {r.go_live_date}</span>
                              )}
                              {r.project_version && (
                                <span className="text-[10px] mt-0.5 inline-block" style={{ color: 'var(--foreground-tertiary)' }}>v{r.project_version}</span>
                              )}
                            </div>
                            <div className="ml-3 flex-shrink-0">
                              <span className="text-xs px-2 py-0.5 rounded-full" style={{ fontFamily: 'var(--font-mono)', ...simStyle }}>
                                {(sim * 100).toFixed(0)}%
                              </span>
                            </div>
                          </div>
                        )
                      })}
                    </div>
                  </div>
                  )
                })()}

                {/* 无结果 */}
                {!hasResults && !log.error && (
                  <div className="p-3 text-center" style={{ background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.5)' }}>
                    <p className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>知识库中未找到匹配内容</p>
                  </div>
                )}
              </div>
              </>)}
            </div>
          )
        })}
      </div>
    )
  }

  function renderImpactAnalysis() {
    if (impactLoading) {
      return (
        <div className="text-center py-12">
          <Loader2 className="w-8 h-8 mx-auto mb-3 animate-spin" style={{ color: 'var(--brand-400)' }} />
          <p className="font-medium" style={{ color: 'var(--foreground-tertiary)' }}>正在分析影响范围...</p>
          <p className="text-sm mt-1" style={{ color: 'var(--foreground-tertiary)' }}>基于功能点依赖图谱 + 模块依赖图谱分析</p>
        </div>
      )
    }

    if (!impactData) {
      return (
        <div className="text-center py-12">
          <GitBranch className="w-12 h-12 mx-auto mb-3" style={{ color: 'var(--foreground-quaternary)' }} />
          <p className="font-medium" style={{ color: 'var(--foreground-tertiary)' }}>影响分析加载失败</p>
          <button onClick={loadImpact} className="mt-3 btn-primary text-sm">
            <Loader2 className="w-4 h-4" /> 重新分析
          </button>
        </div>
      )
    }

    if (impactData.direct_modules.length === 0 && impactData.indirect_modules.length === 0 && impactData.affected_fps.length === 0) {
      return (
        <div className="text-center py-12">
          <Share2 className="w-12 h-12 mx-auto mb-3" style={{ color: 'var(--foreground-quaternary)' }} />
          <p className="font-medium" style={{ color: 'var(--foreground-tertiary)' }}>分析完成：未发现受影响的功能点或模块</p>
          <p className="text-sm mt-1" style={{ color: 'var(--foreground-tertiary)' }}>
            当前需求变更不影响其他已有功能，可直接确认需求并归档
          </p>
        </div>
      )
    }

    return (
      <div className="space-y-5">
        {/* 直接涉及的模块 */}
        {impactData.direct_modules.length > 0 && (
          <div>
            <h3 className="text-sm font-semibold mb-2" style={{ color: 'var(--foreground-secondary)' }}>直接涉及的模块</h3>
            <div className="flex flex-wrap gap-2">
              {impactData.direct_modules.map(m => (
                <span key={m.id} className="px-3 py-1.5 text-sm font-medium border" style={{ background: 'rgba(0, 122, 255, 0.08)', color: 'var(--brand-700)', borderRadius: 'calc(var(--radius) * 0.5)', borderColor: 'var(--brand-300)' }}>
                  {m.name}
                </span>
              ))}
            </div>
          </div>
        )}

        {/* 间接影响的模块（BFS 遍历结果） */}
        {impactData.indirect_modules.length > 0 && (
          <div>
            <h3 className="text-sm font-semibold mb-2" style={{ color: 'var(--foreground-secondary)' }}>
              间接影响的模块
              <span className="text-xs font-normal ml-2" style={{ color: 'var(--foreground-tertiary)' }}>（依赖图 BFS {impactData.indirect_modules.some(m => m.depth > 1) ? '多跳' : '单跳'}传播）</span>
            </h3>
            <div className="space-y-2">
              {impactData.indirect_modules.map((m, i) => (
                <div key={i} className="flex items-center gap-3 px-4 py-2.5" style={{ background: 'var(--state-warning-surface)', borderRadius: 'calc(var(--radius) * 0.6)', border: '1px solid var(--state-warning)' }}>
                  <div className="w-2 h-2 rounded-full flex-shrink-0" style={{ background: 'var(--state-warning)' }} />
                  <div className="flex-1 min-w-0">
                    <span className="text-sm font-medium" style={{ color: 'var(--state-warning)' }}>{m.name}</span>
                    <span className="text-xs ml-2" style={{ color: 'var(--state-warning)' }}>{m.reason}（{m.depth}跳）</span>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* 手动补选受影响功能点（v3.1.3：跨需求/漏建边时人工补选，写入 impact_analysis 生成回归用例） */}
        <div style={{ border: '1px dashed var(--border)', borderRadius: 'calc(var(--radius) * 0.6)', background: 'var(--background-muted/50)' }}>
          <button
            onClick={() => setManualOpen(o => !o)}
            className="flex items-center justify-between w-full px-4 py-2.5 text-left"
            style={{ background: 'transparent', border: 'none', cursor: 'pointer' }}>
            <span className="flex items-center gap-2 text-sm font-medium" style={{ color: 'var(--foreground-secondary)' }}>
              <Search className="w-4 h-4" style={{ color: 'var(--brand-500)' }} />
              手动添加受影响功能点
              <span className="text-xs font-normal" style={{ color: 'var(--foreground-tertiary)' }}>
                （BFS 依赖图谱外的跨需求/漏建边功能点，可在此检索补选）
              </span>
            </span>
            {manualOpen ? <ChevronDown className="w-4 h-4" style={{ color: 'var(--foreground-tertiary)' }} /> : <ChevronRight className="w-4 h-4" style={{ color: 'var(--foreground-tertiary)' }} />}
          </button>
          {manualOpen && (
            <div className="px-4 pb-4 space-y-3">
              {/* v3.1.3：两个可搜索下拉框 —— 模块筛选 + 受影响功能点 */}
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <div className="text-xs mb-1" style={{ color: 'var(--foreground-tertiary)' }}>按模块筛选（可搜索）</div>
                  <SearchSelect
                    options={manualModules.map(m => ({ id: m.id, name: m.name }))}
                    selected={manualModuleId ? new Set([manualModuleId]) : new Set()}
                    onToggle={(mid) => setManualModuleId(prev => (prev === mid ? '' : mid))}
                    display={manualModuleId ? (manualModules.find(m => m.id === manualModuleId)?.name || '') : undefined}
                    placeholder="选择模块（可选，默认全部）"
                  />
                </div>
                <div>
                  <div className="text-xs mb-1" style={{ color: 'var(--foreground-tertiary)' }}>受影响的功能点（可搜索）</div>
                  <div className="flex items-center gap-2">
                    <div className="flex-1 min-w-0">
                      <SearchSelect
                        options={manualFpOptions}
                        selected={manualSelFpId ? new Set([manualSelFpId]) : new Set()}
                        onToggle={handleManualFpPick}
                        display={manualSelFpId ? (manualFpOptions.find(o => o.id === manualSelFpId)?.name || '') : undefined}
                        placeholder={manualFpLoading ? '加载中...' : '选择功能点'}
                        loading={manualFpLoading}
                      />
                    </div>
                    <button onClick={addManualFp} disabled={!manualSelFpId} className="btn-primary text-xs" style={{ whiteSpace: 'nowrap' }}>
                      添加
                    </button>
                  </div>
                </div>
              </div>

              {/* 已添加（手动补选）列表 */}
              {manualAddedList.length > 0 && (
                <div>
                  <div className="text-xs mb-1" style={{ color: 'var(--foreground-tertiary)' }}>
                    已添加 {manualAddedList.length} 个受影响功能点（点击 × 移除）
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {manualAddedList.map(fp => (
                      <span key={fp.id} className="inline-flex items-center gap-1.5 px-2 py-1 text-xs"
                        style={{ background: 'var(--background)', border: '1px solid var(--brand-300)', borderRadius: 'calc(var(--radius) * 0.4)', color: 'var(--foreground-secondary)' }}>
                        {fp.name}
                        {fp.module_names?.length ? <span style={{ color: 'var(--foreground-tertiary)' }}>· {fp.module_names.join(', ')}</span> : null}
                        <button onClick={() => removeManualFp(fp.id)} title="移除"
                          style={{ border: 'none', background: 'transparent', cursor: 'pointer', color: 'var(--foreground-tertiary)', display: 'inline-flex', padding: 0 }}>
                          <X className="w-3 h-3" />
                        </button>
                      </span>
                    ))}
                  </div>
                </div>
              )}

              <p className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>
                默认展示产品内全部已归档功能点，可用模块 / 关键字过滤；选择后点击「添加」即可计入下方确认列表，随「确认」一并写入 SRR。
              </p>
            </div>
          )}
        </div>

        {/* 受影响的功能点（BFS 依赖图谱分析 + 手动补选） */}
        {allImpactedFps.length > 0 ? (
          <div>
            <div className="flex items-center justify-between mb-2">
              <h3 className="text-sm font-semibold" style={{ color: 'var(--foreground-secondary)' }}>
                受影响的功能点
                <span className="text-xs font-normal ml-2" style={{ color: 'var(--foreground-tertiary)' }}>（{allImpactedFps.length} 个，按引用热度排序，点击行展开最新完整 SRR，手动项可移除）</span>
              </h3>
              <div className="flex items-center gap-2">
                <label className="flex items-center gap-1.5 text-xs cursor-pointer select-none" style={{ color: 'var(--foreground-secondary)' }}>
                  <input
                    type="checkbox"
                    checked={allImpactedFps.length > 0 && allImpactedFps.every(fp => confirmedFpIds.has(fp.fp_id))}
                    onChange={toggleAllFps}
                    style={{ accentColor: 'var(--brand-500)' }}
                  />
                  全选
                </label>
              </div>
            </div>
            <div className="space-y-3">
              {allImpactedFps.map(fp => {
                const checked = confirmedFpIds.has(fp.fp_id)
                const expanded = expandedFpIds.has(fp.fp_id)
                return (
                  <div key={fp.fp_id} className="px-4 py-3 transition-all"
                    onClick={() => toggleFpExpand(fp.fp_id)}
                    style={{
                      background: checked ? 'rgba(0, 122, 255, 0.06)' : 'var(--background)',
                      borderRadius: 'calc(var(--radius) * 0.6)',
                      border: checked ? '1px solid var(--brand-400)' : '1px solid var(--border)',
                      boxShadow: checked ? '0 0 0 1px var(--brand-300) inset' : 'none',
                    }}>
                    <div className="flex items-center justify-between mb-1 gap-2">
                      <div className="flex items-center gap-2 min-w-0">
                        {/* 展开指示（点击行展开最新完整 SRR） */}
                        <span className="flex-shrink-0" style={{ color: 'var(--foreground-tertiary)', display: 'inline-flex' }}>
                          {expanded ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
                        </span>
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={e => { e.stopPropagation(); toggleFpConfirm(fp.fp_id) }}
                          onClick={e => e.stopPropagation()}
                          className="flex-shrink-0 mt-0.5"
                          style={{ accentColor: 'var(--brand-500)', width: 15, height: 15 }}
                        />
                        <p className="text-sm font-medium truncate" style={{ color: checked ? 'var(--brand-700)' : 'var(--foreground-secondary)' }}>{fp.fp_name}</p>
                        {fp.fp_status && (
                          <span className="text-[10px] px-1.5 py-0.5 rounded font-medium flex-shrink-0"
                            style={{
                              background: fp.fp_status === 'archived' ? 'var(--state-success-surface)' : 'var(--state-warning-surface)',
                              color: fp.fp_status === 'archived' ? 'var(--state-success)' : 'var(--state-warning)',
                            }}>
                            {STATUS_LABELS[fp.fp_status] || fp.fp_status}
                          </span>
                        )}
                      </div>
                      {fp.is_manual ? (
                        <span className="text-[10px] px-1.5 py-0.5 rounded font-medium flex-shrink-0"
                          style={{ background: 'var(--state-warning-surface)', color: 'var(--state-warning)' }}>
                          手动添加
                        </span>
                      ) : (
                        <span className="text-xs flex-shrink-0" style={{ color: 'var(--foreground-tertiary)' }}>{fp.depth}跳</span>
                      )}
                      {fp.is_manual && (
                        <button
                          onClick={e => { e.stopPropagation(); removeManualFp(fp.fp_id) }}
                          title="移除该手动添加的功能点"
                          className="flex-shrink-0"
                          style={{ border: 'none', background: 'transparent', cursor: 'pointer', color: 'var(--state-danger)', display: 'inline-flex', padding: 2, marginLeft: 2 }}>
                          <X className="w-3.5 h-3.5" />
                        </button>
                      )}
                    </div>
                    <div className="flex items-center gap-2 mb-1 ml-[23px] flex-wrap">
                      {/* v3.2: 模块始终显示，未分配时兜底提示 */}
                      <span className="text-xs px-1.5 py-0.5 rounded" style={{ background: 'var(--background-muted)', color: fp.module_name ? 'var(--foreground-tertiary)' : 'var(--foreground-quaternary)' }}>
                        {fp.module_name || '未分配模块'}
                      </span>
                      {/* v3.3 P0-1: 引用热度徽标——被依赖越多，回归优先级越高（排序列首） */}
                      {(fp.fan_in ?? 0) > 0 && (
                        <span className="text-xs px-1.5 py-0.5 rounded font-medium" title="被依赖数（引用热度）：越高表示变更牵连面越大，建议优先回归"
                          style={{ background: (fp.fan_in ?? 0) >= 6 ? 'rgba(0, 122, 255, 0.12)' : 'var(--background-muted)', color: (fp.fan_in ?? 0) >= 6 ? 'var(--brand-700)' : 'var(--foreground-tertiary)' }}>
                          被依赖 {fp.fan_in}
                        </span>
                      )}
                      {fp.existing_cases_count > 0 && (
                        <span className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>已有 {fp.existing_cases_count} 条用例</span>
                      )}
                    </div>
                    <p className="text-xs mb-1 ml-[23px]" style={{ color: 'var(--foreground-tertiary)' }}>{fp.impact_reason}</p>
                    {fp.source_fp_name && (
                      <p className="text-xs ml-[23px]" style={{ color: 'var(--foreground-tertiary)' }}>来源：依赖「{fp.source_fp_name}」</p>
                    )}
                    {/* v3.2: 展开区 — 功能点最新完整 SRR（跨版本聚合） */}
                    {expanded && (
                      <div className="mt-2 pt-2 ml-[23px]" style={{ borderTop: '1px dashed var(--border)' }}
                        onClick={e => e.stopPropagation()}>
                        <FpCrsrPanel fpId={fp.fp_id} compact />
                      </div>
                    )}
                    {/* KB 知识库说明（仅已归档 FP 有值） */}
                    {fp.kb_context && (
                      <div className="mt-2 p-2.5 ml-[23px]" style={{ background: 'rgba(0, 122, 255, 0.05)', borderRadius: 'calc(var(--radius) * 0.5)', border: '1px solid rgba(0, 122, 255, 0.15)' }} onClick={e => e.stopPropagation()}>
                        <div className="flex items-center gap-1 mb-1">
                          <Database className="w-3 h-3" style={{ color: 'var(--brand-500)' }} />
                          <span className="text-xs font-medium" style={{ color: 'var(--brand-700)' }}>知识库说明</span>
                        </div>
                        <p className="text-xs whitespace-pre-wrap" style={{ color: 'var(--foreground-secondary)' }}>{fp.kb_context}</p>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
            {/* 确认操作栏 */}
            <div className="mt-4 flex items-center justify-between px-4 py-3"
              style={{
                background: 'var(--background-muted)',
                borderRadius: 'calc(var(--radius) * 0.6)',
                border: '1px solid var(--border)',
                position: 'sticky', bottom: 8, backdropFilter: 'blur(6px)',
              }}>
              <div className="text-xs" style={{ color: 'var(--foreground-secondary)' }}>
                已勾选 <span className="font-semibold" style={{ color: 'var(--brand-700)' }}>{confirmedFpIds.size}</span> / {allImpactedFps.length} 个受影响功能点
                <span className="ml-2" style={{ color: 'var(--foreground-tertiary)' }}>（勾选后将生成对应的回归测试用例）</span>
              </div>
              <div className="flex items-center gap-2">
                <button onClick={e => { e.stopPropagation(); handleReRunImpact() }} disabled={impactLoading} className="btn-ghost text-xs">
                  <Loader2 className={`w-3.5 h-3.5 ${impactLoading ? 'animate-spin' : ''}`} /> 重新分析
                </button>
                <button onClick={e => { e.stopPropagation(); handleConfirmImpact() }} disabled={confirmImpactLoading || confirmedFpIds.size === 0} className="btn-primary text-xs">
                  {confirmImpactLoading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <CheckCircle2 className="w-3.5 h-3.5" />}
                  确认选中为受影响
                </button>
              </div>
            </div>
          </div>
        ) : (
          <div className="text-center py-6">
            <p className="text-sm font-medium" style={{ color: 'var(--foreground-tertiary)' }}>未发现受影响的功能点</p>
            <p className="text-xs mt-1" style={{ color: 'var(--foreground-quaternary)' }}>
              当前需求变更不涉及其他功能点的回归，可直接确认需求并归档
            </p>
          </div>
        )}

        {/* 重新分析按钮（无受影响 FP 时也可见） */}
        <div className="flex justify-center pt-2">
          <button onClick={handleReRunImpact} disabled={impactLoading || confirmImpactLoading} className="btn-ghost text-xs">
            <Loader2 className={`w-3.5 h-3.5 ${impactLoading ? 'animate-spin' : ''}`} /> 重新分析
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      {/* 返回 + 操作按钮 */}
      <div className="flex items-center justify-between">
        <button onClick={() => navigate(-1)} className="btn-ghost">
          <ArrowLeft className="w-4 h-4" /> 返回
        </button>
        <div className="flex items-center gap-2">
          {!editing && (
            <>
              {/* draft → 有人工澄清问题 → 去澄清页 */}
              {detail.status === 'draft' && (
                <button onClick={() => navigate(`/requirements/${id}/clarify`)} className="btn-secondary">
                  <MessageCircle className="w-4 h-4" /> 人工填写补充
                </button>
              )}
              {/* v1.6: pending_review → 用户确认后进入 confirmed，可生成用例 */}
              {detail.status === 'pending_review' && (
                <button onClick={handleConfirm} disabled={confirming} className="btn-primary">
                  {confirming ? <Loader2 className="w-4 h-4 animate-spin" /> : <CheckCircle2 className="w-4 h-4" />}
                  确认完成
                </button>
              )}
              {/* active / confirmed → 可补充信息重新生成 */}
              {(detail.status === 'active' || detail.status === 'confirmed') && (
                <button onClick={() => navigate(`/requirements/${id}/clarify`)} className="btn-secondary">
                  <RefreshCw className="w-4 h-4" /> 补充重新生成
                </button>
              )}
              {/* draft / pending_review → 也可重新生成（用户反馈：确认完成前应可点击） */}
              {(detail.status === 'draft' || detail.status === 'pending_review') && (
                <button onClick={() => navigate(`/requirements/${id}/clarify`)} className="btn-secondary">
                  <RefreshCw className="w-4 h-4" /> 补充重新生成
                </button>
              )}
              {detail.status !== 'archived' && (
                <button onClick={startEditing} className="btn-secondary">
                  <Edit3 className="w-4 h-4" /> 编辑 SRR
                </button>
              )}
              {/* v2.0: 导出 XMind 思维导图（任何状态都可导出）；v2.20 支持选择格式（click 控制） */}
              <div className="relative inline-flex" ref={xmindDropdownRef}>
                <button
                  onClick={handleExportXmind}
                  disabled={exporting}
                  className="btn-secondary"
                  style={{ borderRadius: 'calc(var(--radius) * 0.5) 0 0 calc(var(--radius) * 0.5)', borderRight: 'none', paddingRight: 12 }}
                >
                  {exporting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
                  {xmindFormat === 'xmind8' ? '导出 XMind 8' : '导出 XMind'}
                </button>
                <button
                  disabled={exporting}
                  onClick={() => setXmindDropdownOpen(o => !o)}
                  className="btn-secondary"
                  style={{ borderRadius: '0 calc(var(--radius) * 0.5) calc(var(--radius) * 0.5) 0', paddingLeft: 10, paddingRight: 10 }}
                  aria-expanded={xmindDropdownOpen}
                  aria-haspopup="menu"
                  title="选择导出格式"
                >
                  <ChevronDown className={`w-4 h-4 transition-transform ${xmindDropdownOpen ? 'rotate-180' : ''}`} />
                </button>
                {xmindDropdownOpen && (
                  <div
                    className="absolute right-0 top-full z-20 min-w-[200px] py-1.5"
                    style={{
                      marginTop: 6,
                      background: 'var(--background)',
                      border: '1px solid var(--border)',
                      borderRadius: 'calc(var(--radius) * 0.5)',
                      boxShadow: 'var(--shadow-apple-xl)',
                    }}
                  >
                    <button
                      onClick={() => { setXmindFormat('xmind'); setXmindDropdownOpen(false) }}
                      className="w-full text-left transition-colors flex items-center gap-2"
                      style={{
                        padding: '8px 12px',
                        fontSize: 13,
                        background: xmindFormat === 'xmind' ? 'rgba(0, 122, 255, 0.08)' : 'transparent',
                        color: xmindFormat === 'xmind' ? 'var(--brand-500)' : 'var(--foreground-secondary)',
                        fontWeight: xmindFormat === 'xmind' ? 500 : 400,
                      }}
                    >
                      <span className="w-4 flex-shrink-0">
                        {xmindFormat === 'xmind' && <CheckCircle2 className="w-3.5 h-3.5" />}
                      </span>
                      <span className="flex-1">
                        <span className="block">XMind 2020+ / Zen</span>
                        <span className="block text-[11px]" style={{ color: 'var(--foreground-tertiary)' }}>JSON 格式，兼容新版 XMind</span>
                      </span>
                    </button>
                    <button
                      onClick={() => { setXmindFormat('xmind8'); setXmindDropdownOpen(false) }}
                      className="w-full text-left transition-colors flex items-center gap-2"
                      style={{
                        padding: '8px 12px',
                        fontSize: 13,
                        background: xmindFormat === 'xmind8' ? 'rgba(0, 122, 255, 0.08)' : 'transparent',
                        color: xmindFormat === 'xmind8' ? 'var(--brand-500)' : 'var(--foreground-secondary)',
                        fontWeight: xmindFormat === 'xmind8' ? 500 : 400,
                      }}
                    >
                      <span className="w-4 flex-shrink-0">
                        {xmindFormat === 'xmind8' && <CheckCircle2 className="w-3.5 h-3.5" />}
                      </span>
                      <span className="flex-1">
                        <span className="block">XMind 8</span>
                        <span className="block text-[11px]" style={{ color: 'var(--foreground-tertiary)' }}>XML 格式，兼容旧版 XMind</span>
                      </span>
                    </button>
                  </div>
                )}
              </div>
              {/* v1.6: confirmed → 允许生成测试用例（active 兼容旧数据） */}
              {(detail.status === 'active' || detail.status === 'confirmed') && (
                <button onClick={() => navigate(`/requirements/${id}/generate`)} className="btn-primary">
                  <Play className="w-4 h-4" /> 生成用例
                </button>
              )}
            </>
          )}
        </div>
      </div>

      {/* 澄清报错 */}
      {clarifyError && !editing && (
        <div className="p-3 text-sm flex items-start gap-2" style={{ background: 'var(--state-error-surface)', border: '1px solid var(--state-error)', borderRadius: 'calc(var(--radius) * 0.6)', color: 'var(--state-error)' }}>
          <AlertCircle className="w-4 h-4 mt-0.5 flex-shrink-0" />
          <span>{clarifyError}</span>
        </div>
      )}

      {/* v2.33: 多人协作冲突提示条 —— 他人修改了需求内容，提示刷新 */}
      {conflictDetected && (
        <div className="p-3 text-sm flex items-center justify-between gap-3 animate-slide-up" style={{ background: 'var(--state-warning-surface)', border: '1px solid var(--state-warning)', borderRadius: 'calc(var(--radius) * 0.6)', color: 'var(--state-warning)' }}>
          <div className="flex items-center gap-2 min-w-0">
            <AlertCircle className="w-4 h-4 flex-shrink-0" />
            <span className="truncate">需求已被他人修改，当前页面内容可能已过期。建议刷新页面获取最新内容。</span>
          </div>
          <div className="flex items-center gap-2 flex-shrink-0">
            <button
              onClick={() => setConflictDetected(false)}
              className="text-xs underline"
              style={{ color: 'var(--state-warning)' }}
            >
              稍后
            </button>
            <button
              onClick={() => window.location.reload()}
              className="px-3 py-1 text-xs font-medium transition-colors flex items-center gap-1"
              style={{ background: 'var(--state-warning)', color: '#ffffff', borderRadius: 'calc(var(--radius) * 0.5)' }}
            >
              <RefreshCw className="w-3 h-3" /> 刷新页面
            </button>
          </div>
        </div>
      )}

      {/* v2.33: 任务运行中提示条 —— 有分析任务正在执行，此期间无法进行其他分析操作 */}
      {taskRunningInfo && !conflictDetected && (
        <div className="p-3 text-sm flex items-center gap-2" style={{ background: 'rgba(0, 122, 255, 0.08)', border: '1px solid rgba(0, 122, 255, 0.2)', borderRadius: 'calc(var(--radius) * 0.6)', color: 'var(--brand-700)' }}>
          <Loader2 className="w-4 h-4 animate-spin flex-shrink-0" />
          <span>{taskRunningInfo.message || `正在执行${taskRunningInfo.task_type}任务...`}</span>
          <span className="text-xs ml-1" style={{ color: 'var(--brand-500)' }}>（此期间无法进行其他分析操作）</span>
        </div>
      )}

      {/* 标题区域（始终展示） */}
      <div className="card">
        <div className="flex items-start justify-between">
          <div className="space-y-1">
            <div className="flex items-center gap-2">
              <h1 className="text-xl font-bold" style={{ color: 'var(--foreground)' }}>{reqData.title || detail.title}</h1>
              <span className={priorityBadge(reqData.priority || detail.priority)}>{reqData.priority || detail.priority}</span>
            </div>
            <p className="text-sm" style={{ color: 'var(--foreground-tertiary)', fontFamily: 'var(--font-mono)' }}>{detail.id} · {formatDate(detail.created_at)}</p>
          </div>
        </div>

        {/* 草稿状态提醒横幅 */}
        {detail.status === 'draft' && (
          <div className="mt-4 p-4 flex items-center justify-between" style={{ background: 'var(--state-warning-surface)', border: '1px solid var(--state-warning)', borderRadius: 'calc(var(--radius) * 0.6)' }}>
            <div className="flex items-start gap-3">
              <div className="w-8 h-8 rounded-full flex items-center justify-center flex-shrink-0" style={{ background: 'rgba(255, 149, 0, 0.15)' }}>
                <MessageCircle className="w-4 h-4" style={{ color: 'var(--state-warning)' }} />
              </div>
              <div>
                <p className="text-sm font-semibold" style={{ color: 'var(--state-warning)' }}>需求待澄清</p>
                <p className="text-xs mt-0.5" style={{ color: 'var(--state-warning)' }}>该需求有待回答的澄清问题，完成澄清后才能生成测试用例</p>
              </div>
            </div>
            <button
              onClick={() => navigate(`/requirements/${id}/clarify`)}
              className="flex items-center gap-1.5 px-4 py-2 text-sm font-medium transition-all flex-shrink-0 ml-3"
              style={{ background: 'var(--state-warning)', color: '#ffffff', borderRadius: 'calc(var(--radius) * 0.5)', boxShadow: 'var(--shadow-apple-sm)' }}
            >
              <MessageCircle className="w-4 h-4" /> 去澄清
            </button>
          </div>
        )}

        {/* Tab 导航 */}
        <div className="flex items-center gap-1 mt-5" style={{ borderBottom: '1px solid var(--border)' }}>
          <button
            onClick={() => setActiveTab('srr')}
            className="px-4 py-2.5 text-sm font-medium border-b-2 transition-all duration-150"
            style={activeTab === 'srr'
              ? { borderColor: 'var(--brand-500)', color: 'var(--brand-700)' }
              : { borderColor: 'transparent', color: 'var(--foreground-tertiary)' }}
          >
            <CheckCircle2 className="w-4 h-4 inline-block mr-1.5 -mt-0.5" />
            SRR 解析结果
          </button>
          <button
            onClick={() => { setActiveTab('raw'); setSelectedFileIdx(0) }}
            className="px-4 py-2.5 text-sm font-medium border-b-2 transition-all duration-150"
            style={activeTab === 'raw'
              ? { borderColor: 'var(--brand-500)', color: 'var(--brand-700)' }
              : { borderColor: 'transparent', color: 'var(--foreground-tertiary)' }}
          >
            <FileText className="w-4 h-4 inline-block mr-1.5 -mt-0.5" />
            原始文本
            {detail.raw_text && (
              <span className="ml-1.5 px-1.5 py-0.5 rounded text-[10px]" style={{ background: 'var(--background-muted)', color: 'var(--foreground-tertiary)', fontFamily: 'var(--font-mono)' }}>
                {fileEntries.length > 1
                  ? `${fileEntries.length}个文件`
                  : sourceTypeLabel(detail.source_type)}
              </span>
            )}
          </button>
          <button
            onClick={() => setActiveTab('kb')}
            className="px-4 py-2.5 text-sm font-medium border-b-2 transition-all duration-150"
            style={activeTab === 'kb'
              ? { borderColor: 'var(--brand-500)', color: 'var(--brand-700)' }
              : { borderColor: 'transparent', color: 'var(--foreground-tertiary)' }}
          >
            <Database className="w-4 h-4 inline-block mr-1.5 -mt-0.5" />
            知识库查询
            {(detail.kb_query_logs?.length ?? 0) > 0 && (
              <span className="ml-1.5 px-1.5 py-0.5 rounded text-[10px]" style={{ background: 'var(--background-muted)', color: 'var(--foreground-tertiary)', fontFamily: 'var(--font-mono)' }}>
                {detail.kb_query_logs!.length}条
              </span>
            )}
          </button>
          <button
            onClick={() => setActiveTab('impact')}
            className="px-4 py-2.5 text-sm font-medium border-b-2 transition-all duration-150"
            style={activeTab === 'impact'
              ? { borderColor: 'var(--brand-500)', color: 'var(--brand-700)' }
              : { borderColor: 'transparent', color: 'var(--foreground-tertiary)' }}
          >
            <GitBranch className="w-4 h-4 inline-block mr-1.5 -mt-0.5" />
            影响分析
            {/* v3.4: 存在未确认受影响 FP 时黄色提醒（未确认将阻止需求确认与归档） */}
            {impactUnconfirmedCount > 0 && detail.status !== 'archived' && (
              <span
                title={`有 ${impactUnconfirmedCount} 个受影响功能点待确认，未确认前无法确认需求与归档`}
                className="ml-1.5 px-1.5 py-0.5 rounded text-[10px] font-medium"
                style={{ background: 'var(--state-warning-surface)', color: 'var(--state-warning)', border: '1px solid var(--state-warning)' }}
              >
                {impactUnconfirmedCount}个待确认
              </span>
            )}
          </button>
          <button
            onClick={() => setActiveTab('history')}
            className="px-4 py-2.5 text-sm font-medium border-b-2 transition-all duration-150"
            style={activeTab === 'history'
              ? { borderColor: 'var(--brand-500)', color: 'var(--brand-700)' }
              : { borderColor: 'transparent', color: 'var(--foreground-tertiary)' }}
          >
            <History className="w-4 h-4 inline-block mr-1.5 -mt-0.5" />
            澄清历史
            {clarifyHistory.length > 0 && (
              <span className="ml-1.5 px-1.5 py-0.5 rounded text-[10px]" style={{ background: 'var(--background-muted)', color: 'var(--foreground-tertiary)', fontFamily: 'var(--font-mono)' }}>
                {clarifyHistory.length}条
              </span>
            )}
          </button>
          <button
            onClick={() => setActiveTab('versions')}
            className="px-4 py-2.5 text-sm font-medium border-b-2 transition-all duration-150"
            style={activeTab === 'versions'
              ? { borderColor: 'var(--brand-500)', color: 'var(--brand-700)' }
              : { borderColor: 'transparent', color: 'var(--foreground-tertiary)' }}
          >
            <Layers className="w-4 h-4 inline-block mr-1.5 -mt-0.5" />
            历史版本
          </button>
        </div>

        {/* Tab 内容 */}
        <div className="mt-5">
          {editing ? (
            /* 编辑模式覆盖 Tab 内容 */
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <h2 className="section-title">编辑 SRR JSON</h2>
                <div className="flex items-center gap-2">
                  <button onClick={() => setEditing(false)} className="btn-secondary"><X className="w-4 h-4" /> 取消</button>
                  <button onClick={saveEdit} disabled={saving} className="btn-primary">
                    {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />} 保存
                  </button>
                </div>
              </div>
              {saveError && (
                <div className="p-3 text-sm flex items-start gap-2" style={{ background: 'var(--state-error-surface)', border: '1px solid var(--state-error)', borderRadius: 'calc(var(--radius) * 0.6)', color: 'var(--state-error)' }}>
                  <AlertCircle className="w-4 h-4 mt-0.5 flex-shrink-0" /> {saveError}
                </div>
              )}
              <textarea
                value={editJson}
                onChange={e => setEditJson(e.target.value)}
                className="input-field resize-none font-mono text-xs leading-relaxed"
                rows={25}
                spellCheck={false}
              />
              <p className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>💡 直接修改 JSON 中的任意字段，保存后立即生效。不符合 Schema 的字段会在生成时被校验。</p>
            </div>
          ) : activeTab === 'srr' ? (
            <>
              {/* v3.3（docs/14 二期 A5）：需求质量报表。
                  放在 SRR 内容**之前**——用户先看到"这份需求写得全不全"，
                  再往下看具体字段，符合"先总后分"的阅读顺序。 */}
              <div className="mb-4">
                <RequirementQualityReport reqId={id!} />
              </div>
              {renderSrrContent()}
            </>
          ) : activeTab === 'raw' ? (
            renderRawText()
          ) : activeTab === 'kb' ? (
            renderKbLogs()
          ) : activeTab === 'impact' ? (
            renderImpactAnalysis()
          ) : activeTab === 'history' ? (
            <ClarifyHistoryTab
              history={clarifyHistory}
              loading={historyLoading}
              onNavigateToClarify={() => navigate(`/requirements/${id}/clarify`)}
              status={detail.status}
            />
          ) : activeTab === 'versions' ? (
            <VersionHistoryPanel reqId={String(id)} />
          ) : null}
        </div>
      </div>

      {/* 澄清弹窗 */}
      {showClarify && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4" style={{ background: 'rgba(0, 0, 0, 0.32)', backdropFilter: 'blur(4px)' }} onClick={() => setShowClarify(false)}>
          <div className="max-w-lg w-full p-6 animate-slide-up max-h-[80vh] overflow-y-auto" style={{ background: 'var(--background)', borderRadius: 'calc(var(--radius) * 1.1)', boxShadow: 'var(--shadow-apple-xl)' }} onClick={e => e.stopPropagation()}>
            <h2 className="text-lg font-semibold mb-1" style={{ color: 'var(--foreground)' }}>需求澄清</h2>
            <p className="text-sm mb-4" style={{ color: 'var(--foreground-tertiary)' }}>AI 检测到以下信息需要补充或纠正，请逐一确认或修改：</p>
            {questions.length === 0 ? (
              <div className="p-6 text-center text-sm" style={{ color: 'var(--foreground-tertiary)' }}>未检测到需要澄清的问题，需求质量已达标。</div>
            ) : (
              <div className="space-y-4">
                {(Array.isArray(questions) ? questions : []).map((q) => {
                  const isSelect = q.input_type === 'select' || q.input_type === 'multi_select'
                  const isMulti = q.input_type === 'multi_select'
                  const selOptions = q.options || []
                  const currentVal = answers[q.id] || ''
                  const cardBg: React.CSSProperties = q.priority === 'critical'
                    ? { background: 'var(--state-error-surface)', border: '1px solid var(--state-error)', borderRadius: 'calc(var(--radius) * 0.6)' }
                    : q.priority === 'important'
                      ? { background: 'var(--state-warning-surface)', border: '1px solid var(--state-warning)', borderRadius: 'calc(var(--radius) * 0.6)' }
                      : { background: 'var(--background-muted)', border: '1px solid var(--border)', borderRadius: 'calc(var(--radius) * 0.6)' }
                  const iconColor = q.priority === 'critical'
                    ? 'var(--state-error)'
                    : q.priority === 'important'
                      ? 'var(--state-warning)'
                      : 'var(--brand-500)'
                  return (
                    <div key={q.id} className="p-4" style={cardBg}>
                      <div className="flex items-start gap-2 mb-2">
                        <MessageCircle className="w-4 h-4 mt-0.5 flex-shrink-0" style={{ color: iconColor }} />
                        <div>
                          <p className="text-sm font-medium" style={{ color: 'var(--foreground-secondary)' }}>{q.question}</p>
                          <p className="text-xs mt-0.5" style={{ color: 'var(--foreground-tertiary)' }}>{q.context}</p>
                        </div>
                      </div>

                      {isSelect ? (
                        <div className="mt-2 space-y-1.5">
                          {selOptions.map((opt, oi) => {
                            const isSelected = isMulti
                              ? currentVal.split(',').includes(opt)
                              : currentVal === opt
                            return (
                              <button
                                key={oi}
                                onClick={() => {
                                  if (isMulti) {
                                    const parts = currentVal ? currentVal.split(',').filter(Boolean) : []
                                    const idx = parts.indexOf(opt)
                                    const next = idx >= 0
                                      ? parts.filter(p => p !== opt)
                                      : [...parts, opt]
                                    setAnswers({...answers, [q.id]: next.join(',')})
                                  } else {
                                    setAnswers({...answers, [q.id]: currentVal === opt ? '' : opt})
                                  }
                                }}
                                className="w-full text-left px-3 py-2 text-sm border transition-all"
                                style={{
                                  borderRadius: 'calc(var(--radius) * 0.5)',
                                  background: isSelected ? 'rgba(0, 122, 255, 0.08)' : 'var(--background)',
                                  borderColor: isSelected ? 'var(--brand-300)' : 'var(--border)',
                                  color: isSelected ? 'var(--brand-700)' : 'var(--foreground-secondary)'
                                }}
                              >
                                <span className="inline-flex items-center gap-2">
                                  <span className="w-4 h-4 border-2 flex items-center justify-center" style={{
                                    borderRadius: isMulti ? 'calc(var(--radius) * 0.25)' : '9999px',
                                    borderColor: isSelected ? 'var(--brand-500)' : 'var(--input)',
                                    background: isSelected ? 'var(--brand-500)' : 'transparent'
                                  }}>
                                    {isSelected && <CheckCircle2 className="w-3 h-3" style={{ color: '#ffffff' }} />}
                                  </span>
                                  {opt}
                                </span>
                              </button>
                            )
                          })}
                          <textarea
                            value={currentVal}
                            onChange={e => setAnswers({...answers, [q.id]: e.target.value})}
                            rows={2}
                            className="input-field mt-2 text-sm"
                            placeholder={isMulti ? '选择以上选项或自行补充...' : '选择以上选项或自行填写...'}
                          />
                        </div>
                      ) : (
                        <textarea
                          value={currentVal}
                          onChange={e => setAnswers({...answers, [q.id]: e.target.value})}
                          rows={2}
                          className="input-field mt-2 text-sm"
                          placeholder={q.default_answer || (q.priority === 'critical' ? '必填，请补充' : '选填，如有需要请补充...')}
                        />
                      )}
                    </div>
                  )
                })}
              </div>
            )}
            <div className="flex items-center justify-end gap-3 mt-5">
              <button onClick={() => setShowClarify(false)} className="btn-secondary">暂不处理</button>
              {questions.length > 0 && (
                <button onClick={submitClarify} disabled={submitting} className="btn-primary">
                  {submitting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
                  提交答案
                </button>
              )}
            </div>
            <p className="text-xs mt-4" style={{ color: 'var(--foreground-tertiary)' }}>💡 如需修改 SRR 其他字段，请关闭此弹窗后点击「编辑 SRR」按钮手动修改。</p>
          </div>
        </div>
      )}

      {/* 澄清成功提示 */}
      {clarifyDone && !editing && (
        <div className="p-4 flex items-center gap-3" style={{ background: 'var(--state-success-surface)', border: '1px solid var(--state-success)', borderRadius: 'calc(var(--radius) * 1.1)' }}>
          <CheckCircle2 className="w-5 h-5 flex-shrink-0" style={{ color: 'var(--state-success)' }} />
          <div>
            <p className="text-sm font-medium" style={{ color: 'var(--state-success)' }}>澄清答案已提交</p>
            <p className="text-xs" style={{ color: 'var(--state-success)' }}>答案将在生成用例时自动应用。你也可以点击「编辑 SRR」手动调整。</p>
          </div>
        </div>
      )}

    </div>
  )
}


// ============================================================================
// 澄清历史 Tab 组件
// ============================================================================

function ClarifyHistoryTab({
  history,
  loading,
  onNavigateToClarify,
  status,
}: {
  history: ClarifyHistoryEntry[]
  loading: boolean
  onNavigateToClarify: () => void
  status: string
}) {
  if (loading) {
    return (
      <div className="text-center py-12">
        <Loader2 className="w-8 h-8 mx-auto mb-3 animate-spin" style={{ color: 'var(--brand-400)' }} />
        <p className="font-medium" style={{ color: 'var(--foreground-tertiary)' }}>正在加载澄清历史...</p>
      </div>
    )
  }

  if (history.length === 0) {
    return (
      <div className="text-center py-12">
        <History className="w-12 h-12 mx-auto mb-3" style={{ color: 'var(--foreground-quaternary)' }} />
        <p className="font-medium" style={{ color: 'var(--foreground-tertiary)' }}>暂无澄清历史</p>
        <p className="text-sm mt-1" style={{ color: 'var(--foreground-tertiary)' }}>
          {status === 'draft'
            ? '提交澄清答案后，历史记录将显示在此处'
            : '如需补充信息重新生成需求分析，点击下方按钮'}
        </p>
        {status === 'active' && (
          <button onClick={onNavigateToClarify} className="mt-4 btn-secondary text-sm">
            <RefreshCw className="w-4 h-4" /> 补充重新生成
          </button>
        )}
        {(status === 'draft' || status === 'pending_review') && (
          <button onClick={onNavigateToClarify} className="mt-4 btn-secondary text-sm">
            <RefreshCw className="w-4 h-4" /> 补充重新生成
          </button>
        )}
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {/* 顶部操作栏 */}
      <div className="flex items-center justify-between">
        <p className="text-sm" style={{ color: 'var(--foreground-tertiary)' }}>共 {history.length} 条记录</p>
        {status !== 'archived' && (
          <button onClick={onNavigateToClarify} className="btn-secondary text-sm">
            <RefreshCw className="w-4 h-4" /> 补充重新生成
          </button>
        )}
      </div>

      {/* 时间线 */}
      <div className="space-y-3">
        {history.map((entry, idx) => {
          const isClarify = entry.action === 'clarify'
          const date = new Date(entry.timestamp)
          const timeStr = date.toLocaleString('zh-CN', {
            month: '2-digit', day: '2-digit',
            hour: '2-digit', minute: '2-digit', second: '2-digit',
          })

          return (
            <div key={idx} className="card p-4">
              {/* 头部 */}
              <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2">
                  {isClarify ? (
                    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium" style={{ background: 'rgba(0, 122, 255, 0.08)', color: 'var(--brand-700)' }}>
                      <MessageCircle className="w-3 h-3" /> 人工澄清
                    </span>
                  ) : (
                    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium" style={{ background: 'rgba(88, 86, 214, 0.1)', color: '#5856d6' }}>
                      <RefreshCw className="w-3 h-3" /> 重新生成
                    </span>
                  )}
                  {entry.quality_report && (
                    <span
                      className="text-xs"
                      style={{ color: 'var(--foreground-tertiary)' }}
                      title={entry.quality_report.summary || undefined}
                    >
                      质量分: {Math.round((Number(entry.quality_report.overall_score) || 0) * 100)}%
                    </span>
                  )}
                </div>
                <span className="text-xs font-mono" style={{ color: 'var(--foreground-tertiary)' }}>{timeStr}</span>
              </div>

              {/* 澄清记录：Q&A 列表 */}
              {isClarify && (() => {
                // v2.29: 优先使用 qa_records（结构化，含 question 文本），兜底用 questions+answers
                const qaList = entry.qa_records && entry.qa_records.length > 0
                  ? entry.qa_records.map(qa => ({
                      id: qa.id,
                      field: qa.field || '',
                      question: qa.question || '',
                      priority: qa.priority || '',
                      answer: qa.answer || '',
                    }))
                  : (entry.questions || []).map(q => ({
                      id: q.id,
                      field: q.field || '',
                      question: q.question || '',
                      priority: q.priority || '',
                      answer: entry.answers?.[q.id] || '',
                    }))
                if (qaList.length === 0) return null
                return (
                  <div className="space-y-2 mb-3">
                    {qaList.map((q, qi) => (
                      <div key={qi} className="p-3" style={{ background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.5)' }}>
                        <div className="flex items-start gap-2">
                          <span className="px-1.5 py-0.5 rounded text-[10px] font-medium flex-shrink-0" style={
                            q.priority === 'critical'
                              ? { background: 'var(--state-error-surface)', color: 'var(--state-error)' }
                              : q.priority === 'important'
                                ? { background: 'var(--state-warning-surface)', color: 'var(--state-warning)' }
                                : { background: 'var(--background)', color: 'var(--foreground-tertiary)' }
                          }>
                            {q.priority === 'critical' ? '关键' : q.priority === 'important' ? '重要' : q.priority === 'optional' ? '可选' : q.priority || '普通'}
                          </span>
                          <div className="flex-1 min-w-0">
                            <p className="text-sm font-medium" style={{ color: 'var(--foreground-secondary)' }}>{q.question}</p>
                            {q.answer && (
                              <p className="text-sm mt-1 pl-3" style={{ color: 'var(--foreground-tertiary)', borderLeft: '2px solid var(--border)' }}>
                                {q.answer}
                              </p>
                            )}
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                )
              })()}

              {/* 重新生成记录：标题变化 */}
              {!isClarify && entry.previous_title && entry.new_title && (
                <div className="p-3 mb-3" style={{ background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.5)' }}>
                  <div className="flex items-center gap-2 text-sm">
                    <span className="line-through" style={{ color: 'var(--foreground-tertiary)' }}>{entry.previous_title}</span>
                    <span style={{ color: 'var(--foreground-quaternary)' }}>→</span>
                    <span className="font-medium" style={{ color: 'var(--foreground-secondary)' }}>{entry.new_title}</span>
                  </div>
                </div>
              )}

              {/* 补充说明 */}
              {entry.extra_notes && (
                <div className="p-3 mb-3" style={{ background: 'var(--state-warning-surface)', border: '1px solid var(--state-warning)', borderRadius: 'calc(var(--radius) * 0.5)' }}>
                  <p className="text-xs font-medium mb-1" style={{ color: 'var(--state-warning)' }}>补充说明</p>
                  <p className="text-sm whitespace-pre-wrap" style={{ color: 'var(--state-warning)' }}>{entry.extra_notes}</p>
                </div>
              )}

              {/* KB 自动补全摘要 */}
              {isClarify && entry.auto_augmentations && entry.auto_augmentations.length > 0 && (
                <div className="flex items-center gap-2 text-xs" style={{ color: 'var(--state-success)' }}>
                  <CheckCircle2 className="w-3 h-3" />
                  <span>KB 自动补全 {entry.auto_augmentations.length} 项字段</span>
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
