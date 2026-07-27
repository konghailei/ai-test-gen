import { useState, useEffect, useRef, useMemo } from 'react'
import type { ReactNode } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import {
  ArrowLeft, Send, Loader2, MessageCircle, AlertCircle, CheckCircle2, FileText,
  ChevronDown, ChevronRight, BookOpen, Snowflake, SkipForward, Wand2, Save,
  RefreshCw, History, FileUp, XCircle,
} from 'lucide-react'
import { requirements as reqApi } from '../api/client'
import type { ClarifyQuestion, KbAugmentation, FieldConflict, ClarifyHistoryEntry, AnalysisTaskInfo } from '../types'

// ============================================================================
// 草稿自动保存工具
// ============================================================================

const draftKey = (id: string) => `clarify_draft_${id}`

interface DraftData {
  answers: Record<string, string>
  skipped: string[]
  extraNotes: string
  conflictResolutions: Record<string, 'existing' | 'kb' | 'custom'>
  timestamp: number
}

function loadDraft(id: string): DraftData | null {
  try {
    const raw = localStorage.getItem(draftKey(id))
    if (!raw) return null
    return JSON.parse(raw) as DraftData
  } catch {
    return null
  }
}

function saveDraft(id: string, data: DraftData) {
  try {
    localStorage.setItem(draftKey(id), JSON.stringify(data))
  } catch {
    // localStorage 满或禁用，静默失败
  }
}

function clearDraft(id: string) {
  try {
    localStorage.removeItem(draftKey(id))
  } catch {
    // ignore
  }
}

// ============================================================================
// KB 补全值格式化
// ============================================================================

/** 将 KB 补全的 value 格式化为可读文本（支持字符串、数组、基线块 dict） */
function formatAugValue(value: any): string {
  if (!value) return ''
  if (typeof value === 'string') return value.length > 1000 ? value.slice(0, 1000) + '...' : value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (Array.isArray(value)) {
    return value.slice(0, 8).map((v, i) => {
      if (typeof v === 'object' && v !== null) {
        return `[${i + 1}] ${v.action || v.rule || v.field || v.trigger || JSON.stringify(v).slice(0, 80)}`
      }
      return `[${i + 1}] ${String(v).slice(0, 80)}`
    }).join('\n')
  }
  if (typeof value === 'object') {
    const lines: string[] = []
    const hp = value.happy_path
    if (hp?.steps && Array.isArray(hp.steps)) {
      lines.push('【正常流程】')
      hp.steps.slice(0, 5).forEach((s: any) => lines.push(`  ${s.order ?? ''}. ${s.action ?? ''} → ${s.expect ?? ''}`))
    }
    const af = value.alternate_flows
    if (Array.isArray(af) && af.length) {
      lines.push('【异常/边界流程】')
      af.slice(0, 3).forEach((a: any) => lines.push(`  · ${a.trigger ?? ''} → ${a.expect ?? ''}`))
    }
    const br = value.business_rules
    if (Array.isArray(br) && br.length) {
      lines.push('【业务规则】')
      br.slice(0, 5).forEach((r: any) => lines.push(`  · ${r.id ?? ''} ${r.rule ?? r.description ?? ''}`))
    }
    const dc = value.data_constraints
    if (Array.isArray(dc) && dc.length) {
      lines.push('【数据约束】')
      dc.slice(0, 5).forEach((d: any) => lines.push(`  · ${d.field ?? ''}: ${d.constraints ?? d.constraint ?? ''}`))
    }
    const pc = value.preconditions
    if (Array.isArray(pc) && pc.length) {
      lines.push('【前置条件】')
      pc.slice(0, 3).forEach((p: any) => lines.push(`  · ${typeof p === 'string' ? p : JSON.stringify(p).slice(0, 80)}`))
    }
    if (lines.length) return lines.join('\n')
    return JSON.stringify(value, null, 2).slice(0, 1000)
  }
  return String(value).slice(0, 500)
}

// ============================================================================
// 主组件
// ============================================================================

export default function RequirementClarify() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()

  const [loading, setLoading] = useState(true)
  const [questions, setQuestions] = useState<ClarifyQuestion[]>([])
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const [skipped, setSkipped] = useState<Set<string>>(new Set())
  const [extraNotes, setExtraNotes] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState(false)

  // KB 补全 / 质量报告 / 冲突
  const [autoAugmentations, setAutoAugmentations] = useState<KbAugmentation[]>([])
  const [qualityReport, setQualityReport] = useState<{ overall_score: number; completeness_score?: number; ambiguity_score?: number; summary?: string } | null>(null)
  const [kbStatus, setKbStatus] = useState<{ kb_empty: boolean; product_first_requirement: boolean } | null>(null)
  const [fieldConflicts, setFieldConflicts] = useState<FieldConflict[]>([])
  const [conflictResolutions, setConflictResolutions] = useState<Record<string, 'existing' | 'kb' | 'custom'>>({})
  const [conflictCustomValues, setConflictCustomValues] = useState<Record<string, string>>({})

  // 折叠状态
  const [showKbPanel, setShowKbPanel] = useState(true)
  const [showCritical, setShowCritical] = useState(true)
  const [showImportant, setShowImportant] = useState(true)

  // 草稿恢复提示
  const [draftPrompt, setDraftPrompt] = useState<DraftData | null>(null)

  // 草稿保存指示
  const [draftSaved, setDraftSaved] = useState(false)
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  // 重新生成模式（status=active 时启用）
  const [regenMode, setRegenMode] = useState(false)
  const [regenNotes, setRegenNotes] = useState('')
  const [regenFiles, setRegenFiles] = useState<File[]>([])
  const [regenerating, setRegenerating] = useState(false)
  const [regenDone, setRegenDone] = useState(false)
  const [history, setHistory] = useState<ClarifyHistoryEntry[]>([])

  // P1 修复：异步任务轮询（与后端 _run_clarify_reparse / _run_regenerate_reparse 配合）
  const taskPollingRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const [taskMessage, setTaskMessage] = useState('')

  // ===== 加载澄清状态 =====
  useEffect(() => {
    if (!id) return
    setLoading(true)
    setError('')
    Promise.all([
      reqApi.getClarifyState(id),
      reqApi.getClarifyHistory(id).catch(() => ({ items: [] as ClarifyHistoryEntry[], total: 0 })),
    ]).then(([data, histData]) => {
      const qs = data?.questions || []
      setQuestions(qs)
      setAutoAugmentations(data?.auto_augmentations || [])
      setQualityReport(data?.quality_report || null)
      setKbStatus(data?.kb_status || null)
      setFieldConflicts(data?.field_conflicts || [])
      setHistory(histData?.items || [])

      // 无澄清问题 → 进入重新生成模式（需求已确认/已澄清）
      if (qs.length === 0) {
        setRegenMode(true)
      }

      const prev = data?.previous_answers || {}
      const init: Record<string, string> = {}
      qs.forEach(q => { init[q.id] = prev[q.id] || '' })
      setAnswers(init)

      // 初始化冲突解决状态
      const initRes: Record<string, 'existing' | 'kb' | 'custom'> = {}
      const initCustom: Record<string, string> = {}
      ;(data?.field_conflicts || []).forEach(c => {
        initRes[c.field] = 'existing'
        initCustom[c.field] = ''
      })
      setConflictResolutions(initRes)
      setConflictCustomValues(initCustom)

      // 检查是否有未提交草稿（仅在有问题时检查）
      if (qs.length > 0) {
        const draft = loadDraft(id)
        if (draft && Object.keys(prev).length === 0) {
          setDraftPrompt(draft)
        }
      }
    }).catch(e => {
      setError(e?.message || '加载澄清问题失败')
    }).finally(() => setLoading(false))
  }, [id])

  // ===== 重新生成（P1 异步任务模式：立即返回 task_id → 轮询 → 完成跳转） =====
  // v2.17: 支持上传新需求文件，以新文件内容为准重新分析
  const handleRegenerate = async () => {
    if (!id) return
    const hasNotes = regenNotes.trim().length > 0
    const hasFiles = regenFiles.length > 0
    if (!hasNotes && !hasFiles) return
    setRegenerating(true)
    setError('')
    setTaskMessage('正在提交重新生成请求...')
    try {
      const res = await reqApi.regenerate(id, regenNotes.trim(), regenFiles.length > 0 ? regenFiles : undefined)
      const taskId = res?.task_id
      if (!taskId) throw new Error('未返回任务 ID')
      setTaskMessage('正在重新生成 SRR...')
      startRegenPolling(taskId)
    } catch (e: any) {
      setError(e?.message || '重新生成失败')
      setRegenerating(false)
    }
  }

  const startRegenPolling = (taskId: string) => {
    if (taskPollingRef.current) clearInterval(taskPollingRef.current)
    taskPollingRef.current = setInterval(async () => {
      try {
        const task: AnalysisTaskInfo = await reqApi.getTask(taskId)
        setTaskMessage(task.message || '正在重新生成...')
        if (task.status === 'completed') {
          if (taskPollingRef.current) { clearInterval(taskPollingRef.current); taskPollingRef.current = null }
          setRegenDone(true)
          setRegenerating(false)
          reqApi.getClarifyHistory(id!).then(data => setHistory(data?.items || [])).catch(() => {})
          setTimeout(() => navigate(`/requirements/${id}`, { replace: true }), 800)
        } else if (task.status === 'failed' || task.status === 'interrupted') {
          if (taskPollingRef.current) { clearInterval(taskPollingRef.current); taskPollingRef.current = null }
          setError(task.error || '重新生成失败')
          setRegenerating(false)
        }
      } catch (e: any) {
        if (taskPollingRef.current) { clearInterval(taskPollingRef.current); taskPollingRef.current = null }
        setError(e?.message || '查询任务状态失败')
        setRegenerating(false)
      }
    }, 1500)
  }

  // ===== 草稿自动保存（5 秒防抖） =====
  useEffect(() => {
    if (!id || loading || questions.length === 0) return
    if (saveTimer.current) clearTimeout(saveTimer.current)
    saveTimer.current = setTimeout(() => {
      const hasContent = Object.values(answers).some(v => v) || extraNotes || skipped.size > 0
      if (hasContent) {
        saveDraft(id, {
          answers,
          skipped: Array.from(skipped),
          extraNotes,
          conflictResolutions,
          timestamp: Date.now(),
        })
        setDraftSaved(true)
        setTimeout(() => setDraftSaved(false), 2000)
      }
    }, 5000)
    return () => {
      if (saveTimer.current) clearTimeout(saveTimer.current)
    }
  }, [id, answers, skipped, extraNotes, conflictResolutions, loading, questions.length])

  // ===== 恢复草稿 =====
  const restoreDraft = () => {
    if (!draftPrompt) return
    setAnswers(draftPrompt.answers || {})
    setSkipped(new Set(draftPrompt.skipped || []))
    setExtraNotes(draftPrompt.extraNotes || '')
    setConflictResolutions(draftPrompt.conflictResolutions || {})
    setDraftPrompt(null)
  }

  const dismissDraft = () => {
    setDraftPrompt(null)
  }

  // ===== 进度计算 =====
  const { answeredCount, skippedCount, totalCount } = useMemo(() => {
    const answered = questions.filter(q => (answers[q.id] || '').trim() !== '').length
    const skip = questions.filter(q => skipped.has(q.id)).length
    return { answeredCount: answered, skippedCount: skip, totalCount: questions.length }
  }, [questions, answers, skipped])

  // ===== 问题分组 =====
  const { criticalQs, importantQs } = useMemo(() => {
    return {
      criticalQs: questions.filter(q => q.priority === 'critical' && !skipped.has(q.id)),
      importantQs: questions.filter(q => q.priority === 'important' && !skipped.has(q.id)),
    }
  }, [questions, skipped])

  // ===== 单题操作 =====
  const setAnswer = (qid: string, value: string) => {
    setAnswers(prev => ({ ...prev, [qid]: value }))
    // 有答案时自动取消跳过
    if (value.trim() && skipped.has(qid)) {
      setSkipped(prev => { const n = new Set(prev); n.delete(qid); return n })
    }
  }

  const toggleSkip = (qid: string) => {
    setSkipped(prev => {
      const n = new Set(prev)
      if (n.has(qid)) {
        n.delete(qid)
      } else {
        n.add(qid)
        setAnswers(prevA => ({ ...prevA, [qid]: '' }))
      }
      return n
    })
  }

  const useDefaultAnswer = (qid: string) => {
    const q = questions.find(x => x.id === qid)
    if (q?.default_answer) {
      setAnswer(qid, q.default_answer)
    }
  }

  // ===== 冲突解决 =====
  const setConflictResolution = (field: string, choice: 'existing' | 'kb' | 'custom') => {
    setConflictResolutions(prev => ({ ...prev, [field]: choice }))
  }

  const setConflictCustom = (field: string, value: string) => {
    setConflictCustomValues(prev => ({ ...prev, [field]: value }))
  }

  // ===== 提交（P1 异步任务模式：立即返回 task_id → 轮询 → 完成跳转） =====
  const handleSubmit = async () => {
    if (!id) return
    // critical 跳过警告
    const skippedCritical = questions.filter(q => q.priority === 'critical' && skipped.has(q.id))
    if (skippedCritical.length > 0) {
      if (!confirm(`有 ${skippedCritical.length} 个关键问题被跳过，可能导致 SRR 不完整。确定继续提交吗？`)) {
        return
      }
    }
    setSubmitting(true)
    setError('')
    setTaskMessage('正在提交澄清答案...')
    try {
      // 将冲突解决结果合并到 answers
      const finalAnswers = { ...answers }
      fieldConflicts.forEach(c => {
        const choice = conflictResolutions[c.field] || 'existing'
        if (choice === 'kb') {
          finalAnswers[`conflict_${c.field}`] = String(c.suggested_value)
        } else if (choice === 'custom') {
          finalAnswers[`conflict_${c.field}`] = conflictCustomValues[c.field] || ''
        } else {
          finalAnswers[`conflict_${c.field}`] = String(c.existing_value)
        }
      })
      const res = await reqApi.submitClarify(id, finalAnswers, extraNotes)
      const taskId = res?.task_id
      if (!taskId) throw new Error('未返回任务 ID')
      clearDraft(id)
      setTaskMessage('正在调用 LLM 重新生成 SRR...')
      startSubmitPolling(taskId)
    } catch (e: any) {
      setError(e?.message || '提交失败')
      setSubmitting(false)
    }
  }

  const startSubmitPolling = (taskId: string) => {
    if (taskPollingRef.current) clearInterval(taskPollingRef.current)
    taskPollingRef.current = setInterval(async () => {
      try {
        const task: AnalysisTaskInfo = await reqApi.getTask(taskId)
        setTaskMessage(task.message || '正在重新生成 SRR...')
        if (task.status === 'completed') {
          if (taskPollingRef.current) { clearInterval(taskPollingRef.current); taskPollingRef.current = null }
          setDone(true)
          setSubmitting(false)
          setTimeout(() => navigate(`/requirements/${id}`, { replace: true }), 800)
        } else if (task.status === 'failed' || task.status === 'interrupted') {
          if (taskPollingRef.current) { clearInterval(taskPollingRef.current); taskPollingRef.current = null }
          setError(task.error || '提交后重新生成失败')
          setSubmitting(false)
        }
      } catch (e: any) {
        if (taskPollingRef.current) { clearInterval(taskPollingRef.current); taskPollingRef.current = null }
        setError(e?.message || '查询任务状态失败')
        setSubmitting(false)
      }
    }, 1500)
  }

  // 组件卸载时清理轮询 timer
  useEffect(() => {
    return () => {
      if (taskPollingRef.current) {
        clearInterval(taskPollingRef.current)
        taskPollingRef.current = null
      }
    }
  }, [])

  if (loading) return <div className="space-y-4 max-w-3xl mx-auto pt-8">{[1,2,3].map(i => <div key={i} className="skeleton h-20" />)}</div>

  const score = qualityReport?.overall_score ?? 0
  const scorePass = score >= 0.75
  const coldStart = kbStatus?.kb_empty || kbStatus?.product_first_requirement

  return (
    <div className="max-w-3xl mx-auto space-y-5 pt-4 pb-24">
      {/* Header */}
      <div className="flex items-center justify-between">
        <button onClick={() => navigate(`/requirements/${id}`)} className="btn-ghost">
          <ArrowLeft className="w-4 h-4" /> 返回需求详情
        </button>
      </div>

      {/* P1 修复：异步任务轮询中的进度提示（submitting/regenerating 时显示 taskMessage） */}
      {((submitting || regenerating) && taskMessage) && (
        <div className="bg-gradient-to-r from-blue-50 to-indigo-50 border border-blue-100 rounded-2xl p-6 text-center">
          <Loader2 className="w-10 h-10 text-blue-500 mx-auto mb-2 animate-spin" />
          <h2 className="text-base font-semibold text-blue-800 mb-1">任务进行中</h2>
          <p className="text-sm text-blue-600">{taskMessage}</p>
        </div>
      )}

      {done ? (
        <div className="bg-gradient-to-r from-emerald-50 to-green-50 border border-emerald-100 rounded-2xl p-8 text-center">
          <CheckCircle2 className="w-12 h-12 text-emerald-500 mx-auto mb-3" />
          <h2 className="text-lg font-semibold text-emerald-800 mb-1">澄清已提交</h2>
          <p className="text-sm text-emerald-600">{taskMessage || '需求已确认，SRR 已重新生成...'}</p>
        </div>
      ) : regenDone ? (
        <div className="bg-gradient-to-r from-purple-50 to-indigo-50 border border-purple-100 rounded-2xl p-8 text-center">
          <CheckCircle2 className="w-12 h-12 text-purple-500 mx-auto mb-3" />
          <h2 className="text-lg font-semibold text-purple-800 mb-1">需求分析已重新生成</h2>
          <p className="text-sm text-purple-600">{taskMessage || '即将返回需求详情...'}</p>
        </div>
      ) : regenMode ? (
        <div className="space-y-5">
          {/* 已确认状态提示 */}
          <div className="bg-gradient-to-r from-emerald-50 to-green-50 border border-emerald-100 rounded-2xl p-5">
            <div className="flex items-center gap-3">
              <CheckCircle2 className="w-6 h-6 text-emerald-500 flex-shrink-0" />
              <div>
                <h2 className="text-base font-semibold text-emerald-800">需求已确认</h2>
                <p className="text-sm text-emerald-600 mt-0.5">
                  该需求已完成分析。如需补充信息，可在下方输入并重新生成需求分析。
                </p>
              </div>
            </div>
          </div>

          {/* 历史记录摘要 */}
          {history.length > 0 && (
            <div className="card p-4">
              <div className="flex items-center gap-2 mb-3">
                <History className="w-4 h-4 text-slate-400" />
                <h3 className="text-sm font-semibold text-slate-700">操作历史 ({history.length} 条)</h3>
              </div>
              <div className="space-y-2">
                {history.slice(0, 3).map((entry, idx) => {
                  const isClarify = entry.action === 'clarify'
                  const date = new Date(entry.timestamp)
                  const timeStr = date.toLocaleString('zh-CN', {
                    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
                  })
                  return (
                    <div key={idx} className="flex items-center gap-2 text-sm">
                      <span className={`px-2 py-0.5 rounded text-xs font-medium ${
                        isClarify ? 'bg-blue-50 text-blue-600' : 'bg-purple-50 text-purple-600'
                      }`}>
                        {isClarify ? '人工澄清' : '重新生成'}
                      </span>
                      {isClarify && entry.questions && (
                        <span className="text-slate-500">{entry.questions.length} 个问题</span>
                      )}
                      {!isClarify && entry.extra_notes && (
                        <span className="text-slate-500 truncate max-w-xs">{entry.extra_notes.slice(0, 40)}...</span>
                      )}
                      <span className="text-xs text-slate-400 ml-auto font-mono">{timeStr}</span>
                    </div>
                  )
                })}
                {history.length > 3 && (
                  <p className="text-xs text-slate-400 text-center pt-1">
                    还有 {history.length - 3} 条更早的记录，请在需求详情页查看
                  </p>
                )}
              </div>
            </div>
          )}

          {/* 重新生成输入区 */}
          <div className="card p-5 space-y-4">
            <div className="flex items-center gap-2">
              <RefreshCw className="w-5 h-5 text-primary-500" />
              <h3 className="text-base font-semibold text-slate-800">重新生成需求分析</h3>
            </div>
            <p className="text-sm text-slate-500">
              可上传补充需求文件或输入补充说明，系统将原始需求与补充内容合并后重新调用 AI 解析。
              <strong className="text-amber-600">补充文件不替代原始文件</strong>，两者内容在详情页均可查看。
              此操作会记录到澄清历史中。
            </p>

            {/* 文件上传区 */}
            <div className="p-4 border-2 border-dashed border-amber-200 rounded-xl hover:border-amber-300 transition-colors bg-amber-50/30">
              <label className="label flex items-center gap-1.5">
                <FileUp className="w-4 h-4 text-amber-500" />
                上传补充需求文件（可选，支持多选 .md / .docx / .txt）
              </label>
              <input
                type="file"
                accept=".md,.docx,.txt,.markdown"
                multiple
                onChange={e => {
                  const selected = Array.from(e.target.files || [])
                  if (selected.length > 0) {
                    setRegenFiles(prev => [...prev, ...selected])
                  }
                  e.target.value = ''
                }}
                disabled={regenerating}
                className="block w-full text-sm text-slate-500 file:mr-4 file:py-2 file:px-4 file:rounded-xl file:border-0 file:text-sm file:font-medium file:bg-amber-50 file:text-amber-700 hover:file:bg-amber-100 disabled:opacity-50"
              />
              <p className="text-xs text-amber-600 mt-1">
                补充文件可纠正原始文件中的错误，但不会替代原始文件内容
              </p>
            </div>

            {/* 已选文件列表 */}
            {regenFiles.length > 0 && (
              <div className="space-y-2">
                <p className="text-xs text-slate-400">已选择 {regenFiles.length} 个文件：</p>
                {regenFiles.map((f, i) => (
                  <div key={i} className="flex items-center justify-between bg-slate-50 p-2.5 rounded-lg">
                    <span className="text-xs text-slate-600 flex items-center gap-1.5">
                      <FileUp className="w-3.5 h-3.5 text-slate-400" />
                      {f.name}
                      <span className="text-slate-400">({(f.size / 1024).toFixed(1)} KB)</span>
                    </span>
                    <button
                      onClick={() => setRegenFiles(prev => prev.filter((_, idx) => idx !== i))}
                      className="text-slate-400 hover:text-red-500 p-1"
                      title="移除"
                      disabled={regenerating}
                    >
                      <XCircle className="w-4 h-4" />
                    </button>
                  </div>
                ))}
              </div>
            )}

            <div className="border-t border-slate-100 pt-3">
              <label className="label">补充说明（可选，与文件至少填写一项）</label>
              <textarea
                value={regenNotes}
                onChange={e => setRegenNotes(e.target.value)}
                rows={4}
                className="input-field resize-none"
                placeholder="请输入补充说明，例如：&#10;- 需要增加 OAuth 第三方登录支持&#10;- 密码长度限制改为 8-64&#10;- 增加 2FA 双因素认证..."
                disabled={regenerating}
              />
            </div>
            {error && (
              <div className="p-3 bg-red-50 border border-red-200 rounded-xl text-sm text-red-700 flex items-start gap-2">
                <AlertCircle className="w-4 h-4 mt-0.5 flex-shrink-0" /> {error}
              </div>
            )}
            <div className="flex items-center justify-end gap-3">
              <button onClick={() => navigate(`/requirements/${id}`)} className="btn-secondary" disabled={regenerating}>
                返回
              </button>
              <button
                onClick={handleRegenerate}
                disabled={regenerating || (!regenNotes.trim() && regenFiles.length === 0)}
                className="btn-primary"
              >
                {regenerating ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
                重新生成需求分析
              </button>
            </div>
          </div>
        </div>
      ) : (
        <>
          {/* ===== 顶部状态栏 ===== */}
          <div className="card p-4 space-y-3">
            <div className="flex items-center gap-3">
              <MessageCircle className="w-6 h-6 text-primary-500" />
              <div className="flex-1">
                <h1 className="text-xl font-bold text-slate-900">需求澄清</h1>
                <p className="text-sm text-slate-400">补充以下信息可让需求分析更精确</p>
              </div>
              {qualityReport && (
                <div className={`px-3 py-1.5 rounded-lg text-sm font-medium ${scorePass ? 'bg-emerald-50 text-emerald-700' : 'bg-amber-50 text-amber-700'}`}>
                  质量评分: {score.toFixed(2)} {scorePass ? '✅' : '❌'}
                </div>
              )}
            </div>

            {/* 冷启动提示 */}
            {coldStart && (
              <div className="flex items-center gap-2 px-3 py-2 bg-sky-50 border border-sky-100 rounded-lg text-sm text-sky-700">
                <Snowflake className="w-4 h-4 flex-shrink-0" />
                <span>冷启动模式：知识库为空或为该产品首个需求，质量阈值已放宽至 0.60，建议人工重点审核</span>
              </div>
            )}

            {/* 进度条 */}
            {questions.length > 0 && (
              <div className="space-y-1">
                <div className="flex items-center justify-between text-xs text-slate-500">
                  <span>进度: {answeredCount}/{totalCount} 已回答{skippedCount > 0 && ` · ${skippedCount} 已跳过`}</span>
                  <span>{Math.round((answeredCount / totalCount) * 100)}%</span>
                </div>
                <div className="h-2 bg-slate-100 rounded-full overflow-hidden">
                  <div
                    className="h-full bg-primary-500 rounded-full transition-all duration-300"
                    style={{ width: `${(answeredCount / totalCount) * 100}%` }}
                  />
                </div>
              </div>
            )}

            {error && (
              <div className="p-3 bg-red-50 border border-red-200 rounded-xl text-sm text-red-700 flex items-start gap-2">
                <AlertCircle className="w-4 h-4 mt-0.5 flex-shrink-0" />
                <span>{error}</span>
              </div>
            )}

            {/* 草稿恢复提示 */}
            {draftPrompt && (
              <div className="p-3 bg-amber-50 border border-amber-200 rounded-xl text-sm text-amber-800 flex items-center justify-between gap-3">
                <div className="flex items-center gap-2">
                  <Save className="w-4 h-4 flex-shrink-0" />
                  <span>检测到未提交草稿（{new Date(draftPrompt.timestamp).toLocaleString()}），是否恢复？</span>
                </div>
                <div className="flex gap-2">
                  <button onClick={restoreDraft} className="px-3 py-1 bg-amber-600 text-white rounded-lg text-xs hover:bg-amber-700">恢复</button>
                  <button onClick={dismissDraft} className="px-3 py-1 bg-white border border-amber-300 text-amber-700 rounded-lg text-xs hover:bg-amber-50">丢弃</button>
                </div>
              </div>
            )}
          </div>

          {/* ===== KB 自动补全展示区 ===== */}
          {autoAugmentations.length > 0 && (
            <div className="card overflow-hidden">
              <button
                onClick={() => setShowKbPanel(!showKbPanel)}
                className="w-full flex items-center gap-2 p-4 hover:bg-slate-50 transition-colors"
              >
                {showKbPanel ? <ChevronDown className="w-4 h-4 text-slate-400" /> : <ChevronRight className="w-4 h-4 text-slate-400" />}
                <BookOpen className="w-4 h-4 text-primary-500" />
                <span className="text-sm font-medium text-slate-700">KB 已自动补全 ({autoAugmentations.length} 项)</span>
                {!showKbPanel && (
                  <span className="text-xs text-slate-400 ml-auto">无需人工回答</span>
                )}
              </button>
              {showKbPanel && (
                <div className="px-4 pb-4 space-y-2 border-t border-slate-100 pt-3">
                  {autoAugmentations.map((aug, i) => {
                    const valueStr = formatAugValue(aug.value)
                    return (
                      <details key={i} className="group border border-slate-100 rounded-lg overflow-hidden">
                        <summary className="cursor-pointer flex items-start gap-2 text-sm p-2.5 hover:bg-slate-50 transition-colors">
                          <CheckCircle2 className="w-4 h-4 text-emerald-500 flex-shrink-0 mt-0.5" />
                          <div className="flex-1">
                            <span className="text-slate-600">
                              <code className="text-xs bg-slate-100 px-1.5 py-0.5 rounded text-primary-600">{aug.field}</code>
                              {' ← '}
                              {aug.source_req_id && <span className="text-slate-500">{aug.source_req_id} </span>}
                              <span className="text-slate-400">(相似度 {(aug.similarity * 100).toFixed(0)}%)</span>
                            </span>
                            {valueStr && (
                              <p className="text-xs text-slate-400 mt-1 line-clamp-2 group-open:line-clamp-none">
                                {valueStr.slice(0, 200)}{valueStr.length > 200 ? '...' : ''}
                              </p>
                            )}
                          </div>
                          <span className="text-slate-300 group-open:hidden text-xs mt-0.5">▸</span>
                          <span className="text-slate-300 hidden group-open:inline text-xs mt-0.5">▾</span>
                        </summary>
                        {valueStr && (
                          <pre className="p-2.5 bg-slate-50 border-t border-slate-100 text-[11px] text-slate-600 leading-relaxed whitespace-pre-wrap overflow-auto max-h-60">{valueStr}</pre>
                        )}
                      </details>
                    )
                  })}
                </div>
              )}
            </div>
          )}

          {/* ===== 字段冲突提示区 ===== */}
          {fieldConflicts.length > 0 && (
            <div className="card p-4 space-y-3 border-amber-200">
              <div className="flex items-center gap-2">
                <AlertCircle className="w-5 h-5 text-amber-500" />
                <h3 className="text-sm font-semibold text-slate-700">检测到 {fieldConflicts.length} 处字段冲突，请确认</h3>
              </div>
              {fieldConflicts.map((c, i) => (
                <div key={i} className="p-3 bg-amber-50/50 rounded-xl space-y-2">
                  <div className="flex items-center gap-2">
                    <code className="text-xs bg-white px-1.5 py-0.5 rounded text-amber-700 border border-amber-200">{c.field}</code>
                    <span className="text-xs text-slate-400">相似度 {(c.kb_similarity * 100).toFixed(0)}%</span>
                  </div>
                  <div className="grid grid-cols-2 gap-2 text-sm">
                    <div className="p-2 bg-white rounded-lg border border-slate-200">
                      <div className="text-xs text-slate-400 mb-1">原文值 ({c.existing_source})</div>
                      <div className="text-slate-700 text-xs break-all">{String(c.existing_value).slice(0, 100)}</div>
                    </div>
                    <div className="p-2 bg-white rounded-lg border border-slate-200">
                      <div className="text-xs text-slate-400 mb-1">KB 建议 ({c.suggested_source})</div>
                      <div className="text-slate-700 text-xs break-all">{String(c.suggested_value).slice(0, 100)}</div>
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    {(['existing', 'kb', 'custom'] as const).map(choice => (
                      <button
                        key={choice}
                        onClick={() => setConflictResolution(c.field, choice)}
                        className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-all ${
                          conflictResolutions[c.field] === choice
                            ? 'bg-primary-500 text-white'
                            : 'bg-white border border-slate-200 text-slate-600 hover:border-slate-300'
                        }`}
                      >
                        {choice === 'existing' ? '采用原文' : choice === 'kb' ? '采用 KB' : '自定义'}
                      </button>
                    ))}
                  </div>
                  {conflictResolutions[c.field] === 'custom' && (
                    <textarea
                      value={conflictCustomValues[c.field] || ''}
                      onChange={e => setConflictCustom(c.field, e.target.value)}
                      rows={2}
                      className="input-field text-sm"
                      placeholder="请输入自定义值..."
                    />
                  )}
                </div>
              ))}
            </div>
          )}

          {/* ===== 人工澄清问题区 ===== */}
          {questions.length === 0 && autoAugmentations.length === 0 ? (
            <div className="card p-8 text-center text-slate-400">
              <FileText className="w-10 h-10 mx-auto mb-2 opacity-50" />
              <p>未检测到需要澄清的问题</p>
              <p className="text-xs mt-1">当前需求信息已足够完整</p>
            </div>
          ) : questions.length === 0 ? (
            <div className="card p-6 text-center text-slate-500">
              <CheckCircle2 className="w-8 h-8 text-emerald-500 mx-auto mb-2" />
              <p className="text-sm">所有缺口已由 KB 自动补全，无需人工澄清</p>
            </div>
          ) : (
            <div className="space-y-4">
              {/* Critical 分组 */}
              {criticalQs.length > 0 && (
                <QuestionGroup
                  title="关键问题"
                  icon="🔴"
                  count={criticalQs.length}
                  expanded={showCritical}
                  onToggle={() => setShowCritical(!showCritical)}
                  accentColor="red"
                >
                  {criticalQs.map(q => (
                    <QuestionCard
                      key={q.id}
                      question={q}
                      answer={answers[q.id] || ''}
                      isSkipped={skipped.has(q.id)}
                      onAnswer={(v) => setAnswer(q.id, v)}
                      onSkip={() => toggleSkip(q.id)}
                      onUseDefault={() => useDefaultAnswer(q.id)}
                      accentColor="red"
                    />
                  ))}
                </QuestionGroup>
              )}

              {/* Important 分组 */}
              {importantQs.length > 0 && (
                <QuestionGroup
                  title="重要问题"
                  icon="🟡"
                  count={importantQs.length}
                  expanded={showImportant}
                  onToggle={() => setShowImportant(!showImportant)}
                  accentColor="amber"
                >
                  {importantQs.map(q => (
                    <QuestionCard
                      key={q.id}
                      question={q}
                      answer={answers[q.id] || ''}
                      isSkipped={skipped.has(q.id)}
                      onAnswer={(v) => setAnswer(q.id, v)}
                      onSkip={() => toggleSkip(q.id)}
                      onUseDefault={() => useDefaultAnswer(q.id)}
                      accentColor="amber"
                    />
                  ))}
                </QuestionGroup>
              )}

              {/* 额外补充说明 */}
              <div className="card p-4 space-y-2">
                <label className="label">额外补充说明</label>
                <textarea
                  value={extraNotes}
                  onChange={e => setExtraNotes(e.target.value)}
                  rows={3}
                  className="input-field text-sm"
                  placeholder="如有其他需要补充的信息，请在此填写（可选）"
                />
                <p className="text-xs text-slate-400">补充说明将一并提交给 AI，帮助生成更准确的需求分析</p>
              </div>
            </div>
          )}

          {/* ===== 底部操作栏（sticky） ===== */}
          {questions.length > 0 && (
            <div className="fixed bottom-0 left-0 right-0 bg-white border-t border-slate-200 px-4 py-3 shadow-lg z-10">
              <div className="max-w-3xl mx-auto flex items-center justify-between gap-4">
                {/* 左：草稿状态 */}
                <div className="flex items-center gap-2 text-xs text-slate-400">
                  {draftSaved ? (
                    <><Save className="w-3.5 h-3.5 text-emerald-500" /><span className="text-emerald-600">草稿已保存</span></>
                  ) : (
                    <><Save className="w-3.5 h-3.5" /><span>草稿自动保存中</span></>
                  )}
                </div>
                {/* 中：进度 */}
                <div className="text-xs text-slate-500 hidden sm:block">
                  {answeredCount}/{totalCount} 已回答{skippedCount > 0 && ` · ${skippedCount} 已跳过`}
                </div>
                {/* 右：操作按钮 */}
                <div className="flex items-center gap-2">
                  <button onClick={() => navigate(`/requirements/${id}`)} className="btn-secondary text-sm">
                    暂不处理
                  </button>
                  <button onClick={handleSubmit} disabled={submitting} className="btn-primary text-sm">
                    {submitting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
                    {submitting ? '提交中...' : '提交并重新分析'}
                  </button>
                </div>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}

// ============================================================================
// 问题分组组件
// ============================================================================

interface QuestionGroupProps {
  title: string
  icon: string
  count: number
  expanded: boolean
  onToggle: () => void
  accentColor: 'red' | 'amber'
  children: ReactNode
}

function QuestionGroup({ title, icon, count, expanded, onToggle, accentColor, children }: QuestionGroupProps) {
  const headerBg = accentColor === 'red' ? 'bg-red-50/50' : 'bg-amber-50/50'
  const headerText = accentColor === 'red' ? 'text-red-700' : 'text-amber-700'
  return (
    <div className={`rounded-2xl border ${accentColor === 'red' ? 'border-red-100' : 'border-amber-100'} overflow-hidden`}>
      <button
        onClick={onToggle}
        className={`w-full flex items-center gap-2 p-3 ${headerBg} hover:bg-opacity-75 transition-colors`}
      >
        {expanded ? <ChevronDown className="w-4 h-4 text-slate-400" /> : <ChevronRight className="w-4 h-4 text-slate-400" />}
        <span className="text-sm font-semibold">{icon}</span>
        <span className={`text-sm font-semibold ${headerText}`}>{title}</span>
        <span className={`text-xs ${headerText} opacity-70`}>({count} 个问题)</span>
      </button>
      {expanded && (
        <div className="p-3 space-y-3">
          {children}
        </div>
      )}
    </div>
  )
}

// ============================================================================
// 单题卡片组件
// ============================================================================

interface QuestionCardProps {
  question: ClarifyQuestion
  answer: string
  isSkipped: boolean
  onAnswer: (value: string) => void
  onSkip: () => void
  onUseDefault: () => void
  accentColor: 'red' | 'amber'
}

function QuestionCard({ question: q, answer, isSkipped, onAnswer, onSkip, onUseDefault, accentColor }: QuestionCardProps) {
  const [showContext, setShowContext] = useState(false)
  const isSelect = q.input_type === 'select' || q.input_type === 'multi_select'
  const isMulti = q.input_type === 'multi_select'
  const selOptions = q.options || []
  const accentBorder = accentColor === 'red' ? 'border-l-red-500' : 'border-l-amber-500'

  return (
    <div className={`relative p-4 bg-white rounded-xl border border-slate-200 border-l-4 ${accentBorder} ${isSkipped ? 'opacity-50' : ''}`}>
      {/* 右上角操作 */}
      <div className="absolute top-3 right-3 flex items-center gap-1">
        {q.default_answer && !isSkipped && (
          <button
            onClick={onUseDefault}
            className="flex items-center gap-1 px-2 py-1 text-xs text-primary-600 hover:bg-primary-50 rounded-lg transition-colors"
            title="使用建议答案"
          >
            <Wand2 className="w-3 h-3" /> 建议答案
          </button>
        )}
        <button
          onClick={onSkip}
          className={`flex items-center gap-1 px-2 py-1 text-xs rounded-lg transition-colors ${
            isSkipped ? 'text-slate-500 bg-slate-100' : 'text-slate-400 hover:bg-slate-100'
          }`}
          title={isSkipped ? '取消跳过' : '跳过此题'}
        >
          <SkipForward className="w-3 h-3" /> {isSkipped ? '取消跳过' : '跳过'}
        </button>
      </div>

      {/* 问题文本 */}
      <div className="mb-2 pr-24">
        <p className="text-sm font-medium text-slate-700">{q.question}</p>
        <button
          onClick={() => setShowContext(!showContext)}
          className="text-xs text-slate-400 hover:text-slate-600 mt-0.5"
        >
          {showContext ? '隐藏说明' : '为什么需要这个？'}
        </button>
        {showContext && (
          <p className="text-xs text-slate-500 mt-1 p-2 bg-slate-50 rounded-lg">{q.context}</p>
        )}
        {/* 分类标签 */}
        {q.category && (
          <span className="inline-block mt-1 px-1.5 py-0.5 text-[10px] bg-slate-100 text-slate-500 rounded">
            {q.category === 'cold_start' ? '❄️ 冷启动' :
             q.category === 'gap' ? '🔍 遗漏检测' :
             q.category === 'ambiguity' ? '❓ 歧义' :
             q.category === 'completeness' ? '📋 完整性' :
             q.category === 'conflict' ? '⚠️ 冲突' :
             q.category === 'impact_analysis' ? '🔬 影响分析' : q.category}
          </span>
        )}
      </div>

      {/* 输入区 */}
      {!isSkipped && (
        <>
          {isSelect ? (
            <div className="mt-2 space-y-1.5">
              {selOptions.map((opt, oi) => {
                const isSelected = isMulti
                  ? answer.split(',').includes(opt)
                  : answer === opt
                return (
                  <button
                    key={oi}
                    onClick={() => {
                      if (isMulti) {
                        const parts = answer ? answer.split(',').filter(Boolean) : []
                        const idx = parts.indexOf(opt)
                        const next = idx >= 0 ? parts.filter(p => p !== opt) : [...parts, opt]
                        onAnswer(next.join(','))
                      } else {
                        onAnswer(answer === opt ? '' : opt)
                      }
                    }}
                    className={`w-full text-left px-3 py-2 rounded-lg text-sm border transition-all ${
                      isSelected ? 'bg-primary-50 border-primary-300 text-primary-700' : 'bg-white border-slate-200 text-slate-600 hover:border-slate-300'
                    }`}
                  >
                    <span className="inline-flex items-center gap-2">
                      <span className={`w-4 h-4 rounded-${isMulti ? 'sm' : 'full'} border-2 flex items-center justify-center ${isSelected ? 'border-primary-500 bg-primary-500' : 'border-slate-300'}`}>
                        {isSelected && <CheckCircle2 className="w-3 h-3 text-white" />}
                      </span>
                      {opt}
                    </span>
                  </button>
                )
              })}
              <textarea
                value={answer}
                onChange={e => onAnswer(e.target.value)}
                rows={2}
                className="input-field mt-2 text-sm"
                placeholder={isMulti ? '选择以上选项或自行补充...' : '选择以上选项或自行填写...'}
              />
            </div>
          ) : (
            <textarea
              value={answer}
              onChange={e => onAnswer(e.target.value)}
              rows={2}
              className="input-field mt-2 text-sm"
              placeholder={q.default_answer || (q.priority === 'critical' ? '必填，请补充' : '选填，如有需要请补充...')}
            />
          )}
        </>
      )}

      {/* 跳过状态提示 */}
      {isSkipped && (
        <div className="text-xs text-slate-400 italic mt-2">此问题已跳过</div>
      )}
    </div>
  )
}
