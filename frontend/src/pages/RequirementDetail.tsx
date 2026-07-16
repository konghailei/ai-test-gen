import { useState, useEffect, useCallback, memo } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { ArrowLeft, Play, MessageCircle, Send, Loader2, FileText, AlertCircle, CheckCircle2, Edit3, Save, X, FileCode, FileType, Eye, Database, Search, BookOpen, Archive, GitBranch, Share2, History, RefreshCw, Layers, Clock } from 'lucide-react'
import { requirements as reqApi, products as prodApi } from '../api/client'
import type { RequirementDetail, ClarifyQuestion, ClarifyHistoryEntry, Product } from '../types'
import { formatDate, priorityBadge, STATUS_LABELS, normalizeSourceType, sourceTypeLabel } from '../utils'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeRaw from 'rehype-raw'
import FunctionalPoints from '../components/FunctionalPoints'
import FPSRRViewer from '../components/FPSRRViewer'
import FieldEditor from '../components/FieldEditor'

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
        <div className="flex items-center justify-between px-4 py-1.5 bg-slate-200/80 rounded-t-lg border-b border-slate-300 text-xs text-slate-500 font-mono">
          <span>{match[1]}</span>
        </div>
      )}
      <pre className={`${match ? 'rounded-t-none' : 'rounded-lg'} p-4 bg-slate-50 border border-slate-200 overflow-x-auto text-sm leading-relaxed ${match ? '' : 'rounded-lg'}`}>
        <code className={className || ''} {...props}>{code}</code>
      </pre>
    </div>
  )
}

/** 表格渲染（带斑马纹和悬浮高亮） */
function Table({ children, ...props }: any) {
  return (
    <div className="overflow-x-auto my-4 rounded-xl border border-slate-200">
      <table className="w-full text-sm divide-y divide-slate-200" {...props}>
        {children}
      </table>
    </div>
  )
}

// ============================================================================
// 归档弹窗（React.memo 隔离，避免页面重渲染卡顿）
// ============================================================================

const ArchiveModal = memo(function ArchiveModal({
  reqId,
  onClose,
  onArchived,
}: {
  reqId: string
  onClose: () => void
  onArchived: () => void
}) {
  const [archiveDate, setArchiveDate] = useState(new Date().toISOString().slice(0, 10))
  const [archiving, setArchiving] = useState(false)

  const handleArchive = async () => {
    if (!archiveDate) { alert('请选择上线日期'); return }
    setArchiving(true)
    try {
      await reqApi.archive(reqId, archiveDate)
      onArchived()
    } catch (e: any) {
      alert('归档失败: ' + e.message)
    } finally {
      setArchiving(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 bg-black/30 backdrop-blur-sm flex items-center justify-center p-4" onClick={() => !archiving && onClose()}>
      <div className="bg-white rounded-2xl shadow-xl max-w-md w-full p-6 animate-slide-up" onClick={e => e.stopPropagation()}>
        <div className="flex items-center gap-3 mb-4">
          <div className="w-10 h-10 rounded-xl bg-amber-50 flex items-center justify-center">
            <Archive className="w-5 h-5 text-amber-600" />
          </div>
          <div>
            <h2 className="text-lg font-semibold text-slate-800">归档需求</h2>
            <p className="text-xs text-slate-400">归档后数据将导入知识库，不可再编辑</p>
          </div>
        </div>
        <div className="space-y-3">
          <div>
            <label className="label">上线日期 <span className="text-red-500">*</span></label>
            <input
              type="date"
              value={archiveDate}
              onChange={e => setArchiveDate(e.target.value)}
              className="input-field text-sm"
              autoFocus
            />
          </div>
          <div className="p-3 bg-blue-50 rounded-lg text-xs text-blue-700 flex items-start gap-2">
            <AlertCircle className="w-4 h-4 mt-0.5 flex-shrink-0" />
            <span>上线日期用于知识库检索时区分最新需求。同一功能检索出多个需求时，以靠后日期为准。</span>
          </div>
        </div>
        <div className="flex justify-end gap-3 mt-5">
          <button onClick={onClose} className="btn-secondary" disabled={archiving}>
            <X className="w-4 h-4" /> 取消
          </button>
          <button onClick={handleArchive} disabled={!archiveDate || archiving} className="btn-primary">
            {archiving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Archive className="w-4 h-4" />} 确认归档
          </button>
        </div>
      </div>
    </div>
  )
})

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

  // 归档 — 日期和保存状态由 memo 组件内部管理，父组件仅控制弹窗显隐
  const [showArchiveModal, setShowArchiveModal] = useState(false)
  // 确认生成最终SRR
  const [finalizing, setFinalizing] = useState(false)

  // Tab 切换：'srr' | 'raw' | 'kb' | 'impact' | 'history' | 'fp'
  const [activeTab, setActiveTab] = useState<'srr' | 'raw' | 'kb' | 'impact' | 'history'>('srr')

  // 澄清历史
  const [clarifyHistory, setClarifyHistory] = useState<ClarifyHistoryEntry[]>([])
  const [historyLoading, setHistoryLoading] = useState(false)

  // 影响分析
  const [impactLoading, setImpactLoading] = useState(false)
  const [impactData, setImpactData] = useState<{
    req_title: string
    direct_modules: { id: string; name: string }[]
    indirect_modules: { id: string; name: string; reason: string; depth: number }[]
    affected_requirements: { id: string; title: string; priority: string; modules: string[] }[]
  } | null>(null)

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
    await reqApi.update(id, srr)
    setReq(prev => prev ? { ...prev, srr_json: srr } : prev)
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

  // 产品列表（用于名称查找）
  const [products, setProducts] = useState<Product[]>([])

  // 多文件选择（原始文本 tab 内）
  const [selectedFileIdx, setSelectedFileIdx] = useState(0)

  useEffect(() => {
    if (!id) return
    reqApi.get(id).then(setReq).catch(() => navigate('/requirements')).finally(() => setLoading(false))
  }, [id])

  useEffect(() => { prodApi.list().then(list => setProducts(list || [])) }, [])

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
      const result = await reqApi.impactAnalysis(id)
      setImpactData(result)
    } catch (e: any) {
      alert('影响分析失败: ' + (e?.message || '未知错误'))
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
      await reqApi.update(id, parsed)
      const updated = await reqApi.get(id)
      setReq(updated)
      setEditing(false)
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

  // 归档成功回调：重新加载需求详情
  const handleArchived = useCallback(async () => {
    setShowArchiveModal(false)
    if (id) {
      try {
        const updated = await reqApi.get(id)
        setReq(updated)
      } catch {}
    }
  }, [id])

  const handleFinalize = async () => {
    if (!id) return
    setFinalizing(true)
    try {
      await reqApi.finalize(id)
      const updated = await reqApi.get(id)
      setReq(updated)
    } catch (e: any) {
      alert('生成最终SRR失败: ' + e.message)
    } finally {
      setFinalizing(false)
    }
  }

  if (loading) return <div className="space-y-4">{[1,2,3].map(i => <div key={i} className="skeleton h-16" />)}</div>
  if (!req) return null

  // 非空引用（供闭包函数使用，TS 类型收窄）
  const detail = req
  const srr = detail.srr_json as any
  const reqData = srr?.requirement || {}
  const flows = srr?.flows || {}

  /** 解析原始文本为文件条目列表（支持多文件合并格式 */
  function parseFileEntries(): { filename: string; source_type: string; text: string }[] {
    if (!detail.raw_text) return []
    const isMulti = normalizeSourceType(detail.source_type) === 'multi'
    if (!isMulti) {
      const st = normalizeSourceType(detail.source_type)
      return [{
        filename: `source.${st}`,
        source_type: st,
        text: detail.raw_text,
      }]
    }
    const entries: { filename: string; source_type: string; text: string }[] = []
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
        <div className="grid grid-cols-3 gap-4 p-4 bg-slate-50/50 rounded-xl">
          <div><span className="text-xs text-slate-400 block">产品</span><span className="text-sm font-medium text-slate-700">{(products.find(p => p.id === (reqData.product_id || detail.product_id))?.name) || reqData.product_id || detail.product_id}</span></div>
          <div><span className="text-xs text-slate-400 block">模块</span><span className="text-sm font-medium text-slate-700">
            {detail.modules && detail.modules.length > 0
              ? detail.modules.map((m: string) => moduleMap[m] || m).join('、')
              : '-'}
          </span></div>
          <div><span className="text-xs text-slate-400 block">状态</span><span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium ${
            detail.status === 'active' ? 'bg-emerald-50 text-emerald-700' :
            detail.status === 'draft' ? 'bg-yellow-50 text-yellow-700' :
            detail.status === 'archived' ? 'bg-slate-100 text-slate-600' :
            'bg-slate-100 text-slate-500'
          }`}>{STATUS_LABELS[detail.status] || detail.status}</span></div>
        </div>

        {/* v1.5.2: FP 审核（元信息下方、SRR 详情上方） */}
        {detail.status !== 'archived' && (
          <div className="border border-slate-200 rounded-xl p-4 bg-white">
            <h3 className="text-sm font-semibold text-slate-700 mb-3 flex items-center gap-2">
              <Layers className="w-4 h-4 text-primary-500" />
              功能点（FP）审核
            </h3>
            <FPSRRViewer reqId={id!} productId={detail.product_id} status={detail.status} />
          </div>
        )}

        {/* 功能概述 */}
        {reqData.summary && (
          <FieldEditor
            label="功能概述"
            fieldPath="requirement.summary"
            value={reqData.summary}
            valueType="string"
            onSave={saveSrrField}
            disabled={detail.status === 'archived'}
            render={(v, toggle) => (
              <p className="text-sm text-slate-600 leading-relaxed">
                {String(v)}
                <button onClick={toggle}
                  className="ml-1 opacity-0 group-hover:opacity-100 text-slate-400 hover:text-primary-500 align-top">
                  <Edit3 className="w-3 h-3 inline" />
                </button>
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
          render={(v, toggle) => {
            const list = Array.isArray(v) ? v : []
            return (
              <>
                {list.length > 0 ? (
                  <ul className="list-disc list-inside text-sm text-slate-600 space-y-1">
                    {list.map((p: string, i: number) => <li key={i}>{p}</li>)}
                  </ul>
                ) : <p className="text-xs text-slate-400 italic">点击编辑添加前置条件</p>}
                <button onClick={toggle}
                  className="ml-1 opacity-0 group-hover:opacity-100 text-slate-400 hover:text-primary-500 align-top">
                  <Edit3 className="w-3 h-3 inline" />
                </button>
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
          render={(v, toggle) => {
            const steps = Array.isArray(v) ? v : []
            if (!steps.length) return <p className="text-xs text-slate-400 italic"><button onClick={toggle} className="text-primary-500 hover:underline">点击添加正常流程步骤</button></p>
            return (
              <div className="space-y-2">
                {steps.map((s: any, i: number) => (
                  <div key={i} className="flex gap-3 p-3 bg-primary-50/50 rounded-xl">
                    <span className="w-6 h-6 rounded-full bg-primary-100 text-primary-600 text-xs font-bold flex items-center justify-center flex-shrink-0 mt-0.5">{s.order || i + 1}</span>
                    <div className="flex-1 min-w-0">
                      <p className="text-sm text-slate-700">{s.action}</p>
                      <p className="text-xs text-slate-400 mt-0.5">→ {s.expect}</p>
                    </div>
                  </div>
                ))}
                <button onClick={toggle}
                  className="opacity-0 hover:opacity-100 text-xs text-primary-500">
                  <Edit3 className="w-3 h-3 inline mr-1" />编辑步骤
                </button>
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
          render={(v, toggle) => {
            const afs = Array.isArray(v) ? v : []
            if (!afs.length) return <p className="text-xs text-slate-400 italic"><button onClick={toggle} className="text-primary-500 hover:underline">点击添加异常流程</button></p>
            return (
              <div className="space-y-2">
                {afs.map((f: any, i: number) => (
                  <div key={i} className="flex gap-3 p-3 bg-amber-50/50 rounded-xl">
                    <AlertCircle className="w-5 h-5 text-amber-500 flex-shrink-0 mt-0.5" />
                    <div>
                      <p className="text-sm font-medium text-slate-700">{f.name}</p>
                      <p className="text-xs text-slate-500">触发: {f.trigger} → {f.expect}</p>
                    </div>
                  </div>
                ))}
                <button onClick={toggle}
                  className="opacity-0 hover:opacity-100 text-xs text-primary-500">
                  <Edit3 className="w-3 h-3 inline mr-1" />编辑异常分支
                </button>
              </div>
            )
          }}
        />

        {/* 边界条件 */}
        <FieldEditor
          label="边界条件"
          fieldPath="boundary_conditions"
          value={srr.boundary_conditions || []}
          valueType="object[]"
          placeholder={'[{"field":"字段名","condition":"边界条件","expect":"预期结果"}]'}
          onSave={saveSrrField}
          disabled={detail.status === 'archived'}
          render={(v, toggle) => {
            const bcs = Array.isArray(v) ? v : []
            if (!bcs.length) return <p className="text-xs text-slate-400 italic"><button onClick={toggle} className="text-primary-500 hover:underline">点击添加边界条件</button></p>
            return (
              <div className="grid grid-cols-2 gap-2">
                {bcs.map((b: any, i: number) => (
                  <div key={i} className="p-3 bg-slate-50 rounded-xl text-sm">
                    <span className="font-medium text-slate-700">{b.field}: </span>
                    <span className="text-slate-500">{b.condition}</span>
                    <p className="text-xs text-slate-400 mt-0.5">→ {b.expect}</p>
                  </div>
                ))}
                <button onClick={toggle} className="opacity-0 hover:opacity-100 text-xs text-primary-500 col-span-2">
                  <Edit3 className="w-3 h-3 inline mr-1" />编辑边界条件
                </button>
              </div>
            )
          }}
        />

        {/* 业务规则 */}
        <FieldEditor
          label="业务规则"
          fieldPath="business_rules"
          value={srr.business_rules || []}
          valueType="object[]"
          placeholder={'[{"id":"BR-001","rule":"规则描述","type":"validation"}]'}
          onSave={saveSrrField}
          disabled={detail.status === 'archived'}
          render={(v, toggle) => {
            const rules = Array.isArray(v) ? v : []
            if (!rules.length) return <p className="text-xs text-slate-400 italic"><button onClick={toggle} className="text-primary-500 hover:underline">点击添加业务规则</button></p>
            return (
              <div className="space-y-1.5">
                {rules.map((r: any, i: number) => (
                  <div key={i} className="flex items-center gap-2 text-sm text-slate-600">
                    <CheckCircle2 className="w-3.5 h-3.5 text-emerald-500 flex-shrink-0" />
                    <span>{typeof r === 'string' ? r : `${r.id}: ${r.rule}`}</span>
                    {typeof r !== 'string' && r.type && <span className="text-[10px] px-1 bg-slate-100 rounded text-slate-500">{r.type}</span>}
                  </div>
                ))}
                <button onClick={toggle} className="opacity-0 hover:opacity-100 text-xs text-primary-500">
                  <Edit3 className="w-3 h-3 inline mr-1" />编辑业务规则
                </button>
              </div>
            )
          }}
        />

        {/* 数据约束 */}
        <FieldEditor
          label="数据约束"
          fieldPath="data_constraints"
          value={srr.data_constraints || []}
          valueType="object[]"
          placeholder={'[{"field":"字段","type":"string","required":true,"constraints":"约束","example":"示例"}]'}
          onSave={saveSrrField}
          disabled={detail.status === 'archived'}
          render={(v, toggle) => {
            const dcs = Array.isArray(v) ? v : []
            if (!dcs.length) return <p className="text-xs text-slate-400 italic"><button onClick={toggle} className="text-primary-500 hover:underline">点击添加数据约束</button></p>
            return (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead><tr className="bg-slate-50"><th className="px-3 py-2 text-left text-xs text-slate-500">字段</th><th className="px-3 py-2 text-left text-xs text-slate-500">类型</th><th className="px-3 py-2 text-left text-xs text-slate-500">必填</th><th className="px-3 py-2 text-left text-xs text-slate-500">约束</th></tr></thead>
                  <tbody>
                    {dcs.map((d: any, i: number) => (
                      <tr key={i} className="border-t border-slate-100"><td className="px-3 py-2 font-medium">{d.field}</td><td className="px-3 py-2 text-slate-500">{d.type}</td><td className="px-3 py-2">{d.required ? '✅' : ''}</td><td className="px-3 py-2 text-slate-500">{d.constraints}</td></tr>
                    ))}
                  </tbody>
                </table>
                <button onClick={toggle} className="opacity-0 hover:opacity-100 text-xs text-primary-500 mt-1">
                  <Edit3 className="w-3 h-3 inline mr-1" />编辑数据约束
                </button>
              </div>
            )
          }}
        />

        {/* 验收标准 */}
        <FieldEditor
          label="验收标准"
          fieldPath="acceptance_criteria"
          value={srr.acceptance_criteria || []}
          valueType="object[]"
          placeholder={'[{"id":"AC-001","description":"验收描述","type":"functional"}]'}
          onSave={saveSrrField}
          disabled={detail.status === 'archived'}
          render={(v, toggle) => {
            const acs = Array.isArray(v) ? v : []
            if (!acs.length) return <p className="text-xs text-slate-400 italic"><button onClick={toggle} className="text-primary-500 hover:underline">点击添加验收标准</button></p>
            return (
              <div className="space-y-1.5">
                {acs.map((ac: any, i: number) => (
                  <div key={i} className="flex items-center gap-2 text-sm text-slate-600 pl-3 border-l-2 border-emerald-200">
                    <Clock className="w-3.5 h-3.5 text-emerald-500 flex-shrink-0" />
                    <span>{typeof ac === 'string' ? ac : `${ac.id}: ${ac.description}`}</span>
                    {typeof ac !== 'string' && ac.type && <span className="text-[10px] px-1 bg-emerald-50 rounded text-emerald-600">{ac.type}</span>}
                  </div>
                ))}
                <button onClick={toggle} className="opacity-0 hover:opacity-100 text-xs text-primary-500">
                  <Edit3 className="w-3 h-3 inline mr-1" />编辑验收标准
                </button>
              </div>
            )
          }}
        />
      </div>
    )
  }

  // ==========================================================================
  // 渲染：原始文本（Markdown / Word / 纯文本，支持多文件切换）
  // ==========================================================================
  function renderRawText() {
    if (!detail.raw_text) {
      return (
        <div className="text-center py-12">
          <FileText className="w-12 h-12 mx-auto mb-3 text-slate-300" />
          <p className="text-slate-500">暂无原始文本内容</p>
        </div>
      )
    }

    const entry = fileEntries[selectedFileIdx]
    if (!entry) {
      return (
        <div className="text-center py-12">
          <FileText className="w-12 h-12 mx-auto mb-3 text-slate-300" />
          <p className="text-slate-500">未找到文件内容</p>
        </div>
      )
    }

    const isMarkdown = entry.source_type === 'markdown'
    const isDocx = entry.source_type === 'docx'

    return (
      <div>
        {/* 多文件切换药片 */}
        {fileEntries.length > 1 && (
          <div className="flex items-center gap-2 mb-4 flex-wrap">
            {fileEntries.map((f, i) => (
              <button
                key={i}
                onClick={() => { setSelectedFileIdx(i) }}
                className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium border transition-all duration-150 ${
                  i === selectedFileIdx
                    ? 'bg-primary-50 border-primary-300 text-primary-700 shadow-sm'
                    : 'bg-white border-slate-200 text-slate-500 hover:border-slate-300 hover:text-slate-700'
                }`}
              >
                {f.source_type === 'markdown' ? <FileCode className="w-3.5 h-3.5" /> :
                 f.source_type === 'docx' ? <FileType className="w-3.5 h-3.5" /> :
                 <FileText className="w-3.5 h-3.5" />}
                {f.filename}
              </button>
            ))}
          </div>
        )}

        {/* 当前文件来源标识 */}
        <div className="flex items-center gap-2 mb-4 px-1">
          {isMarkdown ? (
            <span className="inline-flex items-center gap-1.5 px-3 py-1 bg-blue-50 text-blue-700 rounded-full text-xs font-medium">
              <FileCode className="w-3.5 h-3.5" /> Markdown
            </span>
          ) : isDocx ? (
            <span className="inline-flex items-center gap-1.5 px-3 py-1 bg-orange-50 text-orange-700 rounded-full text-xs font-medium">
              <FileType className="w-3.5 h-3.5" /> Word 文档
            </span>
          ) : (
            <span className="inline-flex items-center gap-1.5 px-3 py-1 bg-slate-100 text-slate-600 rounded-full text-xs font-medium">
              <FileText className="w-3.5 h-3.5" /> 纯文本
            </span>
          )}
          <span className="text-xs text-slate-400">
            {entry.text.length} 字符 · {entry.text.split('\n').length} 行
          </span>
          {fileEntries.length > 1 && (
            <span className="text-xs text-slate-400">
              · 文件 {selectedFileIdx + 1}/{fileEntries.length}
            </span>
          )}
        </div>

        {/* Markdown 渲染（带完整格式支持） */}
        {isMarkdown ? (
          <div className="prose prose-sm max-w-none
            prose-headings:text-slate-800 prose-headings:font-semibold
            prose-h1:text-xl prose-h1:border-b prose-h1:border-slate-200 prose-h1:pb-2 prose-h1:mb-4
            prose-h2:text-lg prose-h2:border-b prose-h2:border-slate-100 prose-h2:pb-1 prose-h2:mb-3
            prose-a:text-primary-600 prose-a:no-underline hover:prose-a:underline
            prose-code:bg-slate-100 prose-code:px-1.5 prose-code:py-0.5 prose-code:rounded-md prose-code:text-xs prose-code:font-mono prose-code:text-slate-800
            prose-pre:bg-slate-50 prose-pre:border prose-pre:border-slate-200 prose-pre:rounded-lg
            prose-pre:p-0 prose-pre:shadow-sm
            prose-img:rounded-xl prose-img:border prose-img:border-slate-200
            prose-table:text-sm prose-table:border-collapse
            prose-th:bg-slate-50 prose-th:px-4 prose-th:py-2.5 prose-th:text-left prose-th:text-xs prose-th:text-slate-500 prose-th:font-semibold prose-th:border-b prose-th:border-slate-200
            prose-td:px-4 prose-td:py-2.5 prose-td:border-b prose-td:border-slate-100
            prose-tr:even:bg-slate-50/50
            prose-blockquote:border-l-4 prose-blockquote:border-primary-300 prose-blockquote:bg-primary-50/50 prose-blockquote:py-1 prose-blockquote:px-4 prose-blockquote:rounded-r-lg prose-blockquote:not-italic
            prose-ul:list-disc prose-ul:pl-5
            prose-ol:list-decimal prose-ol:pl-5
            prose-li:my-0.5
            prose-hr:border-slate-200
            prose-strong:text-slate-800
            prose-em:text-slate-700
          ">
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              rehypePlugins={[rehypeRaw]}
              components={{
                code({ className, children, ...props }) {
                  const isInline = !className
                  if (isInline) {
                    return <code className="bg-slate-100 px-1.5 py-0.5 rounded-md text-xs font-mono text-slate-800" {...props}>{children}</code>
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
          /* Word / 纯文本 — 带行号格式展示 */
          <div className="bg-white border border-slate-200 rounded-xl overflow-hidden shadow-sm">
            <div className="flex text-sm leading-relaxed">
              {/* 行号侧栏 */}
              <div className="select-none py-4 pl-3 pr-2 text-right text-xs text-slate-300 bg-slate-50 border-r border-slate-200 min-w-[3rem] font-mono">
                {entry.text.split('\n').map((_, i) => (
                  <div key={i}>{i + 1}</div>
                ))}
              </div>
              {/* 文本内容 */}
              <div className="flex-1 py-4 px-4 whitespace-pre-wrap text-sm text-slate-700 leading-relaxed font-sans">
                {entry.text}
              </div>
            </div>
          </div>
        )}
      </div>
    )
  }

  // ==========================================================================
  // 渲染：知识库查询记录
  // ==========================================================================
  function renderKbLogs() {
    const logs = detail.kb_query_logs || []

    if (logs.length === 0) {
      return (
        <div className="text-center py-12">
          <Database className="w-12 h-12 mx-auto mb-3 text-slate-300" />
          <p className="text-slate-500 font-medium">无需调用知识库</p>
          <p className="text-sm text-slate-400 mt-1">当前需求分析阶段未触发知识库查询</p>
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
    }

    return (
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <p className="text-xs text-slate-400">共 {logs.length} 条查询记录</p>
        </div>

        {logs.map((log, i) => {
          const Icon = typeIcon[log.type] || Database
          const hasResults = (log.result_count ?? 0) > 0 && !log.error
          const isQA = log.type === 'kb_query' || log.type === 'kb_unresolved'

          return (
            <div key={i} className={`border rounded-xl overflow-hidden ${
              isQA
                ? log.type === 'kb_query'
                  ? 'border-emerald-200 bg-emerald-50/30'
                  : 'border-amber-200 bg-amber-50/30'
                : 'border-slate-200'
            }`}>
              {/* 查询头部 */}
              <div className={`flex items-center justify-between px-4 py-3 border-b ${
                isQA ? 'border-emerald-100' : 'border-slate-100'
              } ${isQA ? '' : 'bg-slate-50/80'}`}>
                <div className="flex items-center gap-2">
                  <Icon className={`w-4 h-4 ${
                    log.type === 'kb_unresolved' ? 'text-amber-500' : 'text-primary-500'
                  }`} />
                  <span className="text-sm font-medium text-slate-700">{log.label}</span>
                  {isQA && (
                    <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                      log.type === 'kb_query'
                        ? 'bg-emerald-100 text-emerald-700'
                        : 'bg-amber-100 text-amber-700'
                    }`}>
                      {log.type === 'kb_query' ? `✓ KB 已解答` : `✗ 需人工澄清`}
                    </span>
                  )}
                  {!isQA && (
                    <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                      hasResults
                        ? 'bg-emerald-50 text-emerald-700'
                        : 'bg-slate-100 text-slate-500'
                    }`}>
                      {hasResults ? `找到 ${log.result_count} 条` : '无匹配结果'}
                    </span>
                  )}
                </div>
                <span className="text-xs text-slate-400">{log.timestamp}</span>
              </div>

              {/* Q&A 内容 */}
              {isQA && (
                <div className="px-4 py-3 bg-white space-y-3">
                  {/* 问题 */}
                  <div>
                    <span className="text-xs text-primary-500 font-medium">❓ 向知识库提问：</span>
                    <p className="text-sm text-slate-800 mt-0.5 leading-relaxed font-medium">{log.query}</p>
                  </div>
                  {/* 答案 */}
                  {log.answer && (
                    <div className="pl-3 border-l-2 border-emerald-300">
                      <span className="text-xs text-emerald-600 font-medium">💡 知识库回答：</span>
                      <p className="text-sm text-slate-600 mt-0.5 leading-relaxed whitespace-pre-line">{log.answer}</p>
                    </div>
                  )}
                  {/* 来源 */}
                  {log.sources && log.sources.length > 0 && (
                    <div className="pt-1 space-y-2">
                      <span className="text-xs text-slate-400">参考来源（{log.sources.length}条）：</span>
                      {log.sources.map((s: any, si: number) => {
                        const valueStr = formatSourceValue(s.value)
                        return (
                          <details key={si} className="group">
                            <summary className="cursor-pointer text-[11px] bg-slate-100 text-slate-500 px-2 py-0.5 rounded-full inline-flex items-center gap-1 hover:bg-slate-200 transition-colors">
                              {s.id} · 相似度 {s.similarity}
                              <span className="text-slate-300 group-open:hidden">▸</span>
                              <span className="text-slate-300 hidden group-open:inline">▾</span>
                            </summary>
                            {valueStr && (
                              <pre className="mt-2 p-2.5 bg-slate-50 border border-slate-100 rounded-lg text-[11px] text-slate-600 leading-relaxed whitespace-pre-wrap overflow-auto max-h-48">{valueStr}</pre>
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
              <div className="px-4 py-3 bg-white">
                <div className="mb-2">
                  <span className="text-xs text-slate-400 font-medium">查询意图：</span>
                  <p className="text-sm text-slate-700 mt-0.5 leading-relaxed">{log.query || '—'}</p>
                </div>
                {log.query_detail && (
                  <div className="mt-1.5 pl-3 border-l-2 border-slate-200">
                    <p className="text-xs text-slate-400 leading-relaxed">{log.query_detail}</p>
                  </div>
                )}

                {/* 错误信息 */}
                {log.error && (
                  <div className="p-2.5 bg-red-50 border border-red-100 rounded-lg text-xs text-red-600 mb-2">
                    <AlertCircle className="w-3 h-3 inline-block mr-1 -mt-0.5" />
                    {log.error}
                  </div>
                )}

                {/* 匹配结果列表 */}
                {hasResults && log.results && log.results.length > 0 && (
                  <div>
                    <span className="text-xs text-slate-400 font-medium">匹配结果：</span>
                    <div className="mt-1.5 space-y-1.5">
                      {log.results.map((r, j) => (
                        <div key={j} className="flex items-center justify-between p-2.5 bg-slate-50 rounded-lg">
                          <div className="min-w-0 flex-1">
                            <p className="text-xs font-medium text-slate-700 truncate">
                              {r.title || r.name || r.id}
                            </p>
                            {r.summary && (
                              <p className="text-xs text-slate-400 truncate mt-0.5">{r.summary}</p>
                            )}
                            {r.category && (
                              <span className="text-[10px] text-slate-400 mt-0.5 inline-block">{r.category}</span>
                            )}
                          </div>
                          <div className="ml-3 flex-shrink-0">
                            <span className={`text-xs font-mono px-2 py-0.5 rounded-full ${
                              r.similarity >= 0.8 ? 'bg-emerald-50 text-emerald-700' :
                              r.similarity >= 0.6 ? 'bg-amber-50 text-amber-700' :
                              'bg-slate-100 text-slate-500'
                            }`}>
                              {(r.similarity * 100).toFixed(0)}%
                            </span>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* 无结果 */}
                {!hasResults && !log.error && (
                  <div className="p-3 bg-slate-50 rounded-lg text-center">
                    <p className="text-xs text-slate-400">知识库中未找到匹配内容</p>
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
          <Loader2 className="w-8 h-8 mx-auto mb-3 text-primary-400 animate-spin" />
          <p className="text-slate-500 font-medium">正在分析影响范围...</p>
          <p className="text-sm text-slate-400 mt-1">基于模块依赖图谱 BFS 遍历</p>
        </div>
      )
    }

    if (!impactData) {
      return (
        <div className="text-center py-12">
          <GitBranch className="w-12 h-12 mx-auto mb-3 text-slate-300" />
          <p className="text-slate-500 font-medium">影响分析加载失败</p>
          <button onClick={loadImpact} className="mt-3 btn-primary text-sm">
            <Loader2 className="w-4 h-4" /> 重新分析
          </button>
        </div>
      )
    }

    if (impactData.direct_modules.length === 0 && impactData.indirect_modules.length === 0) {
      return (
        <div className="text-center py-12">
          <Share2 className="w-12 h-12 mx-auto mb-3 text-slate-300" />
          <p className="text-slate-500 font-medium">该需求未关联模块</p>
          <p className="text-sm text-slate-400 mt-1">关联模块后可以分析对其他模块的影响范围</p>
        </div>
      )
    }

    return (
      <div className="space-y-5">
        {/* 直接涉及的模块 */}
        {impactData.direct_modules.length > 0 && (
          <div>
            <h3 className="text-sm font-semibold text-slate-700 mb-2">直接涉及的模块</h3>
            <div className="flex flex-wrap gap-2">
              {impactData.direct_modules.map(m => (
                <span key={m.id} className="px-3 py-1.5 bg-primary-50 text-primary-700 rounded-lg text-sm font-medium border border-primary-200">
                  {m.name}
                </span>
              ))}
            </div>
          </div>
        )}

        {/* 间接影响的模块（BFS 遍历结果） */}
        {impactData.indirect_modules.length > 0 && (
          <div>
            <h3 className="text-sm font-semibold text-slate-700 mb-2">
              间接影响的模块
              <span className="text-xs font-normal text-slate-400 ml-2">（依赖图 BFS {impactData.indirect_modules.some(m => m.depth > 1) ? '多跳' : '单跳'}传播）</span>
            </h3>
            <div className="space-y-2">
              {impactData.indirect_modules.map((m, i) => (
                <div key={i} className="flex items-center gap-3 px-4 py-2.5 bg-amber-50 rounded-xl border border-amber-200">
                  <div className="w-2 h-2 rounded-full bg-amber-400 flex-shrink-0" />
                  <div className="flex-1 min-w-0">
                    <span className="text-sm font-medium text-amber-800">{m.name}</span>
                    <span className="text-xs text-amber-600 ml-2">{m.reason}（{m.depth}跳）</span>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* 受影响的其他需求 */}
        {impactData.affected_requirements.length > 0 ? (
          <div>
            <h3 className="text-sm font-semibold text-slate-700 mb-2">
              受影响的需求
              <span className="text-xs font-normal text-slate-400 ml-2">（{impactData.affected_requirements.length} 个）</span>
            </h3>
            <div className="space-y-2">
              {impactData.affected_requirements.map(r => (
                <div key={r.id} className="flex items-center justify-between px-4 py-3 bg-white rounded-xl border border-slate-200 hover:border-primary-300 cursor-pointer transition-colors"
                  onClick={() => navigate(`/requirements/${r.id}`)}>
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium text-slate-700 truncate">{r.title}</p>
                    <div className="flex items-center gap-2 mt-1">
                      <span className="text-xs px-1.5 py-0.5 rounded bg-slate-100 text-slate-500">{r.priority}</span>
                      <span className="text-xs text-slate-400">{r.id}</span>
                    </div>
                  </div>
                  <ArrowLeft className="w-4 h-4 text-slate-300 rotate-180" />
                </div>
              ))}
            </div>
          </div>
        ) : (
          <div className="text-center py-6">
            <p className="text-sm text-slate-400">没有其他需求受影响</p>
          </div>
        )}

        {/* 重新分析按钮 */}
        <div className="flex justify-center pt-2">
          <button onClick={loadImpact} disabled={impactLoading} className="btn-ghost text-xs">
            <Loader2 className={`w-3.5 h-3.5 ${impactLoading ? 'animate-spin' : ''}`} /> 重新分析
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-6 max-w-5xl">
      {/* 返回 + 操作按钮 */}
      <div className="flex items-center justify-between">
        <button onClick={() => navigate('/requirements')} className="btn-ghost">
          <ArrowLeft className="w-4 h-4" /> 返回列表
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
              {/* active → 可补充信息重新生成 */}
              {detail.status === 'active' && (
                <button onClick={() => navigate(`/requirements/${id}/clarify`)} className="btn-secondary">
                  <RefreshCw className="w-4 h-4" /> 补充重新生成
                </button>
              )}
              {detail.status !== 'archived' && (
                <button onClick={startEditing} className="btn-secondary">
                  <Edit3 className="w-4 h-4" /> 编辑 SRR
                </button>
              )}
              {detail.status === 'active' && (
                <button onClick={() => setShowArchiveModal(true)} className="btn-secondary">
                  <Archive className="w-4 h-4" />
                  归档
                </button>
              )}
              {detail.status === 'active' && (
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
        <div className="p-3 bg-red-50 border border-red-200 rounded-xl text-sm text-red-700 flex items-start gap-2">
          <AlertCircle className="w-4 h-4 mt-0.5 flex-shrink-0" />
          <span>{clarifyError}</span>
        </div>
      )}

      {/* 标题区域（始终展示） */}
      <div className="card">
        <div className="flex items-start justify-between">
          <div className="space-y-1">
            <div className="flex items-center gap-2">
              <h1 className="text-xl font-bold text-slate-900">{reqData.title || detail.title}</h1>
              <span className={priorityBadge(reqData.priority || detail.priority)}>{reqData.priority || detail.priority}</span>
            </div>
            <p className="text-sm text-slate-400 font-mono">{detail.id} · {formatDate(detail.created_at)}</p>
          </div>
        </div>

        {/* 草稿状态提醒横幅 */}
        {detail.status === 'draft' && (
          <div className="mt-4 p-4 bg-gradient-to-r from-yellow-50 to-amber-50 border border-yellow-200 rounded-xl flex items-center justify-between">
            <div className="flex items-start gap-3">
              <div className="w-8 h-8 rounded-full bg-yellow-100 flex items-center justify-center flex-shrink-0">
                <MessageCircle className="w-4 h-4 text-yellow-600" />
              </div>
              <div>
                <p className="text-sm font-semibold text-yellow-800">需求待澄清</p>
                <p className="text-xs text-yellow-700 mt-0.5">该需求有待回答的澄清问题，完成澄清后才能生成测试用例</p>
              </div>
            </div>
            <button
              onClick={() => navigate(`/requirements/${id}/clarify`)}
              className="flex items-center gap-1.5 px-4 py-2 bg-yellow-500 text-white rounded-lg text-sm font-medium hover:bg-yellow-600 transition-all shadow-sm flex-shrink-0 ml-3"
            >
              <MessageCircle className="w-4 h-4" /> 去澄清
            </button>
          </div>
        )}

        {/* Tab 导航 */}
        <div className="flex items-center gap-1 mt-5 border-b border-slate-200">
          <button
            onClick={() => setActiveTab('srr')}
            className={`px-4 py-2.5 text-sm font-medium border-b-2 transition-all duration-150 ${
              activeTab === 'srr'
                ? 'border-primary-500 text-primary-700'
                : 'border-transparent text-slate-500 hover:text-slate-700 hover:border-slate-300'
            }`}
          >
            <CheckCircle2 className="w-4 h-4 inline-block mr-1.5 -mt-0.5" />
            SRR 解析结果
          </button>
          <button
            onClick={() => { setActiveTab('raw'); setSelectedFileIdx(0) }}
            className={`px-4 py-2.5 text-sm font-medium border-b-2 transition-all duration-150 ${
              activeTab === 'raw'
                ? 'border-primary-500 text-primary-700'
                : 'border-transparent text-slate-500 hover:text-slate-700 hover:border-slate-300'
            }`}
          >
            <FileText className="w-4 h-4 inline-block mr-1.5 -mt-0.5" />
            原始文本
            {detail.raw_text && (
              <span className="ml-1.5 px-1.5 py-0.5 bg-slate-100 text-slate-500 rounded text-[10px] font-mono">
                {fileEntries.length > 1
                  ? `${fileEntries.length}个文件`
                  : sourceTypeLabel(detail.source_type)}
              </span>
            )}
          </button>
          <button
            onClick={() => setActiveTab('kb')}
            className={`px-4 py-2.5 text-sm font-medium border-b-2 transition-all duration-150 ${
              activeTab === 'kb'
                ? 'border-primary-500 text-primary-700'
                : 'border-transparent text-slate-500 hover:text-slate-700 hover:border-slate-300'
            }`}
          >
            <Database className="w-4 h-4 inline-block mr-1.5 -mt-0.5" />
            知识库查询
            {(detail.kb_query_logs?.length ?? 0) > 0 && (
              <span className="ml-1.5 px-1.5 py-0.5 bg-slate-100 text-slate-500 rounded text-[10px] font-mono">
                {detail.kb_query_logs!.length}条
              </span>
            )}
          </button>
          <button
            onClick={() => setActiveTab('impact')}
            className={`px-4 py-2.5 text-sm font-medium border-b-2 transition-all duration-150 ${
              activeTab === 'impact'
                ? 'border-primary-500 text-primary-700'
                : 'border-transparent text-slate-500 hover:text-slate-700 hover:border-slate-300'
            }`}
          >
            <GitBranch className="w-4 h-4 inline-block mr-1.5 -mt-0.5" />
            影响分析
          </button>
          <button
            onClick={() => setActiveTab('history')}
            className={`px-4 py-2.5 text-sm font-medium border-b-2 transition-all duration-150 ${
              activeTab === 'history'
                ? 'border-primary-500 text-primary-700'
                : 'border-transparent text-slate-500 hover:text-slate-700 hover:border-slate-300'
            }`}
          >
            <History className="w-4 h-4 inline-block mr-1.5 -mt-0.5" />
            澄清历史
            {clarifyHistory.length > 0 && (
              <span className="ml-1.5 px-1.5 py-0.5 bg-slate-100 text-slate-500 rounded text-[10px] font-mono">
                {clarifyHistory.length}条
              </span>
            )}
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
                <div className="p-3 bg-red-50 border border-red-200 rounded-xl text-sm text-red-700 flex items-start gap-2">
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
              <p className="text-xs text-slate-400">💡 直接修改 JSON 中的任意字段，保存后立即生效。不符合 Schema 的字段会在生成时被校验。</p>
            </div>
          ) : activeTab === 'srr' ? (
            renderSrrContent()
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
          ) : null}
        </div>
      </div>

      {/* 澄清弹窗 */}
      {showClarify && (
        <div className="fixed inset-0 z-50 bg-black/30 backdrop-blur-sm flex items-center justify-center p-4" onClick={() => setShowClarify(false)}>
          <div className="bg-white rounded-2xl shadow-xl max-w-lg w-full p-6 animate-slide-up max-h-[80vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
            <h2 className="text-lg font-semibold text-slate-800 mb-1">需求澄清</h2>
            <p className="text-sm text-slate-400 mb-4">AI 检测到以下信息需要补充或纠正，请逐一确认或修改：</p>
            {questions.length === 0 ? (
              <div className="p-6 text-center text-sm text-slate-400">未检测到需要澄清的问题，需求质量已达标。</div>
            ) : (
              <div className="space-y-4">
                {(Array.isArray(questions) ? questions : []).map((q) => {
                  const isSelect = q.input_type === 'select' || q.input_type === 'multi_select'
                  const isMulti = q.input_type === 'multi_select'
                  const selOptions = q.options || []
                  const currentVal = answers[q.id] || ''
                  return (
                    <div key={q.id} className={`p-4 rounded-xl border ${
                      q.priority === 'critical' ? 'border-red-200 bg-red-50/30' :
                      q.priority === 'important' ? 'border-amber-200 bg-amber-50/30' :
                      'bg-slate-50 border-slate-100'
                    }`}>
                      <div className="flex items-start gap-2 mb-2">
                        <MessageCircle className={`w-4 h-4 mt-0.5 flex-shrink-0 ${
                          q.priority === 'critical' ? 'text-red-500' :
                          q.priority === 'important' ? 'text-amber-500' :
                          'text-primary-500'
                        }`} />
                        <div>
                          <p className="text-sm font-medium text-slate-700">{q.question}</p>
                          <p className="text-xs text-slate-400 mt-0.5">{q.context}</p>
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
                                className={`w-full text-left px-3 py-2 rounded-lg text-sm border transition-all ${
                                  isSelected
                                    ? 'bg-primary-50 border-primary-300 text-primary-700'
                                    : 'bg-white border-slate-200 text-slate-600 hover:border-slate-300'
                                }`}
                              >
                                <span className="inline-flex items-center gap-2">
                                  <span className={`w-4 h-4 rounded-${isMulti ? 'sm' : 'full'} border-2 flex items-center justify-center ${
                                    isSelected ? 'border-primary-500 bg-primary-500' : 'border-slate-300'
                                  }`}>
                                    {isSelected && <CheckCircle2 className="w-3 h-3 text-white" />}
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
            <p className="text-xs text-slate-400 mt-4">💡 如需修改 SRR 其他字段，请关闭此弹窗后点击「编辑 SRR」按钮手动修改。</p>
          </div>
        </div>
      )}

      {/* 澄清成功提示 */}
      {clarifyDone && !editing && (
        <div className="bg-gradient-to-r from-emerald-50 to-green-50 border border-emerald-100 rounded-2xl p-4 flex items-center gap-3">
          <CheckCircle2 className="w-5 h-5 text-emerald-500 flex-shrink-0" />
          <div>
            <p className="text-sm font-medium text-emerald-800">澄清答案已提交</p>
            <p className="text-xs text-emerald-600">答案将在生成用例时自动应用。你也可以点击「编辑 SRR」手动调整。</p>
          </div>
        </div>
      )}

      {/* 归档弹窗（React.memo 隔离） */}
      {showArchiveModal && id && (
        <ArchiveModal
          reqId={id}
          onClose={() => setShowArchiveModal(false)}
          onArchived={handleArchived}
        />
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
        <Loader2 className="w-8 h-8 mx-auto mb-3 text-primary-400 animate-spin" />
        <p className="text-slate-500 font-medium">正在加载澄清历史...</p>
      </div>
    )
  }

  if (history.length === 0) {
    return (
      <div className="text-center py-12">
        <History className="w-12 h-12 mx-auto mb-3 text-slate-300" />
        <p className="text-slate-500 font-medium">暂无澄清历史</p>
        <p className="text-sm text-slate-400 mt-1">
          {status === 'draft'
            ? '提交澄清答案后，历史记录将显示在此处'
            : '如需补充信息重新生成需求分析，点击下方按钮'}
        </p>
        {status === 'active' && (
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
        <p className="text-sm text-slate-500">共 {history.length} 条记录</p>
        {status === 'active' && (
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
                    <span className="inline-flex items-center gap-1 px-2 py-0.5 bg-blue-50 text-blue-600 rounded text-xs font-medium">
                      <MessageCircle className="w-3 h-3" /> 人工澄清
                    </span>
                  ) : (
                    <span className="inline-flex items-center gap-1 px-2 py-0.5 bg-purple-50 text-purple-600 rounded text-xs font-medium">
                      <RefreshCw className="w-3 h-3" /> 重新生成
                    </span>
                  )}
                  {entry.quality_report && (
                    <span className="text-xs text-slate-400">
                      质量分: {(entry.quality_report.overall_score * 100).toFixed(0)}%
                    </span>
                  )}
                </div>
                <span className="text-xs text-slate-400 font-mono">{timeStr}</span>
              </div>

              {/* 澄清记录：Q&A 列表 */}
              {isClarify && entry.questions && entry.questions.length > 0 && (
                <div className="space-y-2 mb-3">
                  {entry.questions.map((q, qi) => {
                    const answer = entry.answers?.[q.id] || ''
                    return (
                      <div key={qi} className="bg-slate-50 rounded-lg p-3">
                        <div className="flex items-start gap-2">
                          <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium flex-shrink-0 ${
                            q.priority === 'critical' ? 'bg-red-100 text-red-600' :
                            q.priority === 'important' ? 'bg-amber-100 text-amber-600' :
                            'bg-slate-100 text-slate-500'
                          }`}>
                            {q.priority === 'critical' ? '关键' : q.priority === 'important' ? '重要' : q.priority === 'optional' ? '可选' : q.priority}
                          </span>
                          <div className="flex-1 min-w-0">
                            <p className="text-sm text-slate-700 font-medium">{q.question}</p>
                            {answer && (
                              <p className="text-sm text-slate-600 mt-1 pl-3 border-l-2 border-slate-200">
                                {answer}
                              </p>
                            )}
                          </div>
                        </div>
                      </div>
                    )
                  })}
                </div>
              )}

              {/* 重新生成记录：标题变化 */}
              {!isClarify && entry.previous_title && entry.new_title && (
                <div className="bg-slate-50 rounded-lg p-3 mb-3">
                  <div className="flex items-center gap-2 text-sm">
                    <span className="text-slate-500 line-through">{entry.previous_title}</span>
                    <span className="text-slate-400">→</span>
                    <span className="text-slate-700 font-medium">{entry.new_title}</span>
                  </div>
                </div>
              )}

              {/* 补充说明 */}
              {entry.extra_notes && (
                <div className="bg-amber-50 border border-amber-100 rounded-lg p-3 mb-3">
                  <p className="text-xs text-amber-600 font-medium mb-1">补充说明</p>
                  <p className="text-sm text-amber-800 whitespace-pre-wrap">{entry.extra_notes}</p>
                </div>
              )}

              {/* KB 自动补全摘要 */}
              {isClarify && entry.auto_augmentations && entry.auto_augmentations.length > 0 && (
                <div className="flex items-center gap-2 text-xs text-emerald-600">
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
