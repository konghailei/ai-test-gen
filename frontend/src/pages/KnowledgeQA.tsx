import { useState, useRef, useEffect } from 'react'
import { Link } from 'react-router-dom'
import {
  MessageSquare, Send, Bot, User, CheckCircle2, Pencil,
  X, Loader2, BookOpen, FileText, TestTube, AlertCircle, History,
  PenSquare, Save, CornerDownRight, Layers, ClipboardCheck,
} from 'lucide-react'

/**
 * v3.3（N1 治理 B6-5）：知识权威层级 → 展示元数据。
 *
 * 层级定义（与后端 `knowledge_qa.AUTHORITY_BY_TYPE` 对齐）：
 *   L0 需求文档 > L1 功能点说明 > L2 人工核准用例/历史问答 > L3 AI 生成用例
 *
 * 目的：让"这条答案的规则依据够不够硬"一眼可见——尤其是当结论实际
 * 推断自 L3（AI 生成的测试用例）而非需求文档时，用户必须能看出来。
 */
function authorityMeta(s: { type?: string; authority_level?: string }) {
  const level = s.authority_level || (
    s.type === 'requirement' ? 'L0'
      : s.type === 'functional_point' ? 'L1'
        : s.type === 'knowledge_qa' ? 'L2'
          : 'L3'
  )
  switch (level) {
    case 'L0':
      return { icon: FileText, color: 'var(--brand-500)', label: 'L0 需求文档', hint: '业务规则的唯一权威来源' }
    case 'L1':
      return { icon: Layers, color: 'var(--state-success)', label: 'L1 功能点', hint: '需求的结构化分解，次权威' }
    case 'L2':
      return { icon: ClipboardCheck, color: 'var(--brand-400)', label: 'L2 人工核准', hint: '可作行为佐证，不作规则依据' }
    default:
      return { icon: TestTube, color: 'var(--state-warning)', label: 'L3 AI 用例', hint: '仅作参考，不得作为业务规则依据' }
  }
}
import { knowledgeQA, products as prodApi } from '../api/client'
import { useProduct } from '../store/ProductContext'
import type { KbQAReference, Module } from '../types'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

interface ChatMessage {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  sources?: KbQAReference[]
  recordId?: string
  isError?: boolean
  isStreaming?: boolean
  isCorrected?: boolean
  correctedContent?: string
  correctionNotes?: string
  isFollowUp?: boolean
  kbStatus?: { search_ok: boolean; error: string; total: number }
  // v3.4（docs/15 §4.9）：claims 证据链——逐条结论挂来源 ID
  claims?: { claim: string; source_ids: string[] }[]
  answerStatus?: 'ok' | 'failed'
  answerError?: string
}

export default function KnowledgeQA() {
  const { selectedProductId } = useProduct()
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [activeRecordId, setActiveRecordId] = useState<string | null>(null)
  const [showHistory, setShowHistory] = useState(false)
  const [historyRecords, setHistoryRecords] = useState<any[]>([])
  const [historyLoading, setHistoryLoading] = useState(false)
  const [historyTotal, setHistoryTotal] = useState(0)

  // 模块多选（仅在所选模块范围内问答）
  const [selectedModules, setSelectedModules] = useState<string[]>([])
  const [availableModules, setAvailableModules] = useState<Module[]>([])

  // Correction
  const [correctingId, setCorrectingId] = useState<string | null>(null)
  const [correctText, setCorrectText] = useState('')
  const [correctNotes, setCorrectNotes] = useState('')
  const [archiveLoading, setArchiveLoading] = useState(false)
  // v2.8.56: 纠正审核流程字段
  const [correctModule, setCorrectModule] = useState('')
  const [correctTargetType, setCorrectTargetType] = useState<'requirement' | 'functional_point'>('requirement')
  const [correctTargetId, setCorrectTargetId] = useState('')

  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const abortRef = useRef<AbortController | null>(null)

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  // 加载当前产品的模块列表（用于模块多选 chips）
  useEffect(() => {
    setSelectedModules([])  // 切换产品时清空已选模块
    if (!selectedProductId) { setAvailableModules([]); return }
    prodApi.listModules(selectedProductId).then((mods: Module[]) => {
      setAvailableModules(mods || [])
    }).catch(() => setAvailableModules([]))
  }, [selectedProductId])

  const toggleModule = (moduleName: string) => {
    setSelectedModules(prev =>
      prev.includes(moduleName)
        ? prev.filter(m => m !== moduleName)
        : [...prev, moduleName]
    )
  }

  const loadHistory = async (page = 1) => {
    setHistoryLoading(true)
    try {
      const r = await knowledgeQA.history(page, 20)
      setHistoryRecords(prev => page === 1 ? r.items : [...prev, ...r.items])
      setHistoryTotal(r.total)
    } catch { }
    setHistoryLoading(false)
  }

  // 点击历史记录加载完整对话
  const handleHistoryClick = async (recordId: string) => {
    try {
      const record = await knowledgeQA.detail(recordId)
      const conv = record.conversation_history || []
      const msgs: ChatMessage[] = conv.map((m: any, i: number) => ({
        id: `hist-${recordId}-${i}`,
        role: m.role,
        content: m.content,
        recordId,
      }))
      if (msgs.length === 0) {
        // 无 conversation_history，用 question + answer 构建
        msgs.push({ id: `hist-${recordId}-0`, role: 'user', content: record.question, recordId })
        msgs.push({ id: `hist-${recordId}-1`, role: 'assistant', content: record.answer, recordId })
      }
      setMessages(msgs)
      setActiveRecordId(recordId)
      setShowHistory(false)
    } catch { }
  }

  // ========== Streaming Submit ==========
  const handleSubmit = async () => {
    const q = input.trim()
    if (!q || loading) return

    setInput('')
    const userMsg: ChatMessage = { id: `u-${Date.now()}`, role: 'user', content: q, isFollowUp: !!activeRecordId }
    setMessages(prev => [...prev, userMsg])
    setLoading(true)

    if (activeRecordId) {
      try {
        const resp = await knowledgeQA.chat(activeRecordId, q)
        setMessages(prev => [...prev, {
          id: `a-${Date.now()}`, role: 'assistant', content: resp.answer,
          recordId: activeRecordId, isFollowUp: true,
        }])
      } catch (e: any) {
        setMessages(prev => [...prev, {
          id: `e-${Date.now()}`, role: 'system', content: '请求失败: ' + (e?.message || ''),
          isError: true,
        }])
      }
      setLoading(false)
      return
    }

    // New question — SSE streaming
    const asstId = `a-${Date.now()}`
    setMessages(prev => [...prev, { id: asstId, role: 'assistant', content: '', isStreaming: true }])

    const token = localStorage.getItem('aitestgen_token')
    const controller = new AbortController()
    abortRef.current = controller

    fetch('/api/knowledge-qa/ask', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': token ? `Bearer ${token}` : '',
      },
      body: JSON.stringify({
        question: q,
        product_id: selectedProductId || undefined,
        modules: selectedModules.length > 0 ? selectedModules : undefined,
      }),
      signal: controller.signal,
    }).then(async res => {
      if (res.status === 401) {
        localStorage.removeItem('aitestgen_token')
        window.location.href = '/login'
        return
      }
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }))
        throw new Error(err.detail || `请求失败`)
      }
      if (!res.body) throw new Error('浏览器不支持流式读取')

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let eventType = ''
      let dataLines: string[] = []
      let lineBuffer = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        const chunk = decoder.decode(value, { stream: true })
        // 将新 chunk 追加到缓冲区，然后按行分割
        // 最后一段可能不完整（不含换行符），保留在 lineBuffer 中等下次拼接
        const combined = lineBuffer + chunk
        const lines = combined.split('\n')
        lineBuffer = lines.pop() || ''

        for (const line of lines) {
          const trimmed = line.replace(/\r$/, '')
          if (trimmed.startsWith('event: ')) {
            eventType = trimmed.slice(7).trim()
          } else if (trimmed.startsWith('data: ')) {
            dataLines.push(trimmed.slice(6))
          } else if (trimmed === '' && dataLines.length > 0) {
            // Empty line = end of an SSE event
            const payload = dataLines.join('\n')
            dataLines = []

            if (eventType === 'sources') {
              try {
                setMessages(prev => prev.map(m =>
                  m.id === asstId ? { ...m, sources: JSON.parse(payload) } : m
                ))
              } catch {}
            } else if (eventType === 'done') {
              try {
                const doneData = JSON.parse(payload)
                setActiveRecordId(doneData.id)
                setMessages(prev => prev.map(m =>
                  m.id === asstId ? {
                    ...m,
                    isStreaming: false,
                    recordId: doneData.id,
                    kbStatus: doneData.kb_status,
                    claims: doneData.claims || [],
                    answerStatus: doneData.answer_status || 'ok',
                    answerError: doneData.error || '',
                  } : m
                ))
                setLoading(false)
              } catch {}
            } else {
              // chunk (default)
              setMessages(prev => prev.map(m =>
                m.id === asstId ? { ...m, content: m.content + payload } : m
              ))
            }
            eventType = ''
          }
        }
      }
    }).catch(e => {
      if (e.name !== 'AbortError') {
        setMessages(prev => prev.map(m =>
          m.id === asstId ? { ...m, content: '抱歉，请求失败: ' + e.message, isStreaming: false, isError: true } : m
        ))
      }
    }).finally(() => {
      setLoading(false)
    })
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSubmit()
    }
  }

  const startCorrect = (msg: ChatMessage) => {
    setCorrectingId(msg.id)
    setCorrectText(msg.correctedContent || msg.content)
    setCorrectNotes(msg.correctionNotes || '')
    // v2.8.56: 默认模块取当前选中的第一个模块；颗粒度默认 requirement
    setCorrectModule(selectedModules[0] || '')
    setCorrectTargetType('requirement')
    setCorrectTargetId('')
  }

  const submitCorrect = async (msg: ChatMessage) => {
    if (!msg.recordId || !correctText.trim()) return
    try {
      await knowledgeQA.correct(msg.recordId, correctText.trim(), correctNotes.trim(), {
        correction_module: correctModule,
        correction_target_type: correctTargetType,
        correction_target_id: correctTargetId,
      })
      setMessages(prev => prev.map(m => m.id === msg.id ? {
        ...m, isCorrected: true, correctedContent: correctText.trim(), correctionNotes: correctNotes.trim(),
      } : m))
      setCorrectingId(null)
      alert('纠错已提交审核，请在「纠正审核」页面查看审核进度')
    } catch (e: any) { alert('纠错提交失败: ' + (e?.message || '')) }
  }

  const cancelStreaming = () => {
    abortRef.current?.abort()
    setLoading(false)
    setMessages(prev => prev.map(m => m.isStreaming ? { ...m, isStreaming: false } : m))
  }

  // ==========================================================================
  // Render
  // ==========================================================================
  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="page-title flex items-center gap-2"><MessageSquare className="w-5 h-5" style={{ color: 'var(--brand-500)' }} /> 知识问答</h1>
          <p className="text-sm mt-1" style={{ color: 'var(--foreground-tertiary)' }}>基于知识库的智能问答 — 输入问题获取 AI 分析</p>
        </div>
        <div className="flex items-center gap-2">
          <Link to="/corrections" className="btn-secondary text-sm flex items-center gap-1.5">
            <ClipboardCheck className="w-4 h-4" /> 纠正审核
          </Link>
          <button onClick={() => { setShowHistory(true); loadHistory(1) }} className="btn-secondary text-sm">
            <History className="w-4 h-4" /> 问答历史
          </button>
        </div>
      </div>

      {/* 模块多选 chips — 限定问答范围 */}
      {availableModules.length > 0 && (
        <div className="card p-4">
          <div className="flex items-center gap-2 flex-wrap">
            <Layers className="w-4 h-4 flex-shrink-0" style={{ color: 'var(--foreground-quaternary)' }} />
            <span className="text-sm" style={{ color: 'var(--foreground-tertiary)' }}>问答范围：</span>
            {selectedModules.length > 0 && (
              <button
                onClick={() => setSelectedModules([])}
                className="px-2.5 py-0.5 text-xs rounded-full hover:opacity-80"
                style={{ background: 'var(--background-muted)', color: 'var(--foreground-secondary)' }}
              >
                全部模块
              </button>
            )}
            {availableModules.map(m => {
              const selected = selectedModules.includes(m.id)
              return (
                <button
                  key={m.id}
                  onClick={() => toggleModule(m.id)}
                  className="px-2.5 py-0.5 text-xs rounded-full transition-colors"
                  style={selected
                    ? { background: 'var(--brand-500)', color: '#ffffff' }
                    : { background: 'var(--background-muted)', color: 'var(--foreground-secondary)' }
                  }
                >
                  {m.name}
                </button>
              )
            })}
            {selectedModules.length > 0 && (
              <span className="text-xs ml-1" style={{ color: 'var(--brand-500)' }}>
                已选 {selectedModules.length} 个 · 仅在此范围内问答
              </span>
            )}
          </div>
        </div>
      )}

      {/* Chat area */}
      <div className="card p-6 min-h-[500px] flex flex-col">
        {/* Messages */}
        <div className="flex-1 space-y-5 mb-4 max-h-[60vh] overflow-y-auto">
          {messages.length === 0 && (
            <div className="flex flex-col items-center justify-center py-16" style={{ color: 'var(--foreground-quaternary)' }}>
              <MessageSquare className="w-12 h-12 mb-3 opacity-30" />
              <p className="text-sm font-medium">输入问题，基于知识库进行智能问答</p>
              <div className="flex flex-wrap gap-2 mt-6 max-w-lg justify-center">
                {[
                  '这个产品的核心功能是什么？',
                  '登录模块有哪些测试用例？',
                  '系统有哪些关键业务规则？',
                ].map((q, i) => (
                  <button key={i} onClick={() => { setInput(q); inputRef.current?.focus() }}
                    className="px-3 py-1.5 text-xs rounded-lg transition-colors"
                    style={{ background: 'var(--background-muted)', color: 'var(--foreground-tertiary)' }}>
                    {q}
                  </button>
                ))}
              </div>
            </div>
          )}

          {messages.map(msg => (
            <div key={msg.id} className={`flex gap-3 ${msg.role === 'user' ? 'justify-end' : ''}`}>
              {msg.role !== 'user' && (
                <div className="w-7 h-7 rounded-full flex items-center justify-center flex-shrink-0"
                  style={{ background: msg.isError ? 'var(--state-error-surface)' : 'rgba(0, 122, 255, 0.12)' }}>
                  {msg.isError ? <AlertCircle className="w-3.5 h-3.5" style={{ color: 'var(--state-error)' }} /> : <Bot className="w-3.5 h-3.5" style={{ color: 'var(--brand-500)' }} />}
                </div>
              )}

              <div className={`max-w-[80%] ${msg.role === 'user' ? '' : ''}`}>
                {msg.role === 'user' && (
                  <div className="px-4 py-2.5 text-white text-sm shadow-sm"
                    style={{ background: 'var(--brand-500)', borderRadius: 'calc(var(--radius) * 1.1) calc(var(--radius) * 1.1) calc(var(--radius) * 1.1) 0.375rem', boxShadow: 'var(--shadow-apple-sm)' }}>
                    {msg.content}
                  </div>
                )}

                {(msg.role === 'assistant' || msg.role === 'system') && (
                  <div className="px-4 py-3 border text-sm"
                    style={{
                      borderRadius: 'calc(var(--radius) * 1.1) calc(var(--radius) * 1.1) 0.375rem calc(var(--radius) * 1.1)',
                      ...(msg.isError
                        ? { background: 'var(--state-error-surface)', borderColor: 'var(--state-error)', color: 'var(--state-error)' }
                        : { background: 'var(--background-muted)', borderColor: 'var(--border)', color: 'var(--foreground-secondary)' }
                      )
                    }}>
                    {/* Markdown rendered content */}
                    <div className="text-sm leading-relaxed space-y-2 markdown-body">
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>
                        {msg.isCorrected ? (msg.correctedContent || '') : msg.content || '思考中...'}
                      </ReactMarkdown>
                    </div>

                    {msg.isStreaming && (
                      <span className="inline-block w-2 h-4 animate-pulse rounded-sm mt-1" style={{ background: 'var(--brand-400)' }} />
                    )}

                    {/* Sources */}
                    {msg.sources && msg.sources.length > 0 && !msg.isStreaming && (
                      <div className="mt-4 pt-3 border-t" style={{ borderColor: 'var(--border)' }}>
                        <p className="text-xs font-medium mb-2 flex items-center gap-1" style={{ color: 'var(--foreground-tertiary)' }}>
                          <BookOpen className="w-3 h-3" /> 参考来源
                          {/* v3.3（B6-5）：权威层级图例 */}
                          <span className="ml-1 font-normal" style={{ color: 'var(--foreground-quaternary)' }}>
                            （按权威层级：L0 需求 &gt; L1 功能点 &gt; L2 人工核准 &gt; L3 AI 用例）
                          </span>
                        </p>
                        <div className="space-y-1">
                          {msg.sources.map((s, i) => {
                            const a = authorityMeta(s)
                            const Icon = a.icon
                            return (
                              <div key={i} className="flex items-center gap-2 text-xs py-0.5" style={{ color: 'var(--foreground-tertiary)' }}>
                                <Icon className="w-3 h-3 flex-shrink-0" style={{ color: a.color }} />
                                {/* 权威层级徽标：悬停显示该层级的采信规则 */}
                                <span
                                  className="flex-shrink-0 px-1 py-px rounded text-[10px] leading-4 font-medium"
                                  title={a.hint}
                                  style={{ color: a.color, border: `1px solid ${a.color}`, opacity: 0.9 }}
                                >
                                  {a.label}
                                </span>
                                <span className="truncate">{s.title}</span>
                                <span className="flex-shrink-0" style={{ color: 'var(--foreground-quaternary)' }}>{(s.similarity * 100).toFixed(0)}%</span>
                              </div>
                            )
                          })}
                        </div>
                        {/* v3.3（B6-5）：回答级提示——有 L3 且无 L0 时，
                            说明规则并非来自需求文档，而是推断自 AI 生成用例（N1 的循环权威场景）。
                            这是"光看答案看不出来"的关键风险，必须显式提示。 */}
                        {(() => {
                          const levels = msg.sources.map((s) => authorityMeta(s).label.slice(0, 2))
                          const hasL3 = levels.includes('L3')
                          const hasL0 = levels.includes('L0')
                          if (!hasL3 || hasL0) return null
                          return (
                            <div className="mt-2 flex items-start gap-2 text-xs rounded-md px-3 py-2"
                              style={{ color: 'var(--state-warning)', background: 'var(--state-warning-surface)' }}>
                              <AlertCircle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                              <span>
                                本次来源<strong>未包含 L0 需求文档</strong>，规则可能推断自 L3（AI 生成用例）。
                                引用前请核对需求原文，避免把用例中的推断当作既定规则。
                              </span>
                            </div>
                          )
                        })()}
                      </div>
                    )}

                    {/* KB status warning */}
                    {msg.kbStatus && !msg.isStreaming && (
                      !msg.kbStatus.search_ok ? (
                        <div className="mt-3 flex items-start gap-2 text-xs rounded-md px-3 py-2"
                          style={{ color: 'var(--state-warning)', background: 'var(--state-warning-surface)' }}>
                          <AlertCircle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                          <span>
                            知识库检索失败：{msg.kbStatus.error || '未知错误'}。
                            请检查 Milvus 服务是否正常运行。
                          </span>
                        </div>
                      ) : msg.kbStatus.total === 0 && (!msg.sources || msg.sources.length === 0) ? (
                        <div className="mt-3 flex items-start gap-2 text-xs rounded-md px-3 py-2"
                          style={{ color: 'var(--foreground-tertiary)', background: 'var(--background-muted)' }}>
                          <AlertCircle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                          <span>
                            知识库中暂无已归档的需求或测试用例。请先归档需求后再进行问答，以获得更准确的参考。
                          </span>
                        </div>
                      ) : null
                    )}

                    {/* v3.4：LLM 失败降级提示（不参与纠错归档） */}
                    {!msg.isStreaming && msg.answerStatus === 'failed' && (
                      <div className="mt-3 flex items-start gap-2 text-xs rounded-md px-3 py-2"
                        style={{ color: 'var(--state-danger)', background: 'var(--state-danger-surface, rgba(255,59,48,0.08))' }}>
                        <AlertCircle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                        <span>
                          本次回答由 AI 服务异常产生（{msg.answerError || '未知错误'}），不参与纠错归档，建议重新提问。
                        </span>
                      </div>
                    )}

                    {/* v3.4：claims 证据引用——每条关键结论挂来源 */}
                    {!msg.isStreaming && msg.claims && msg.claims.length > 0 && (
                      <div className="mt-3 text-xs rounded-md px-3 py-2"
                        style={{ background: 'var(--background-muted)' }}>
                        <p className="font-semibold mb-1" style={{ color: 'var(--foreground-secondary)' }}>
                          证据引用（逐条结论 → 来源）
                        </p>
                        <ul className="space-y-1">
                          {msg.claims.map((c, i) => (
                            <li key={i} style={{ color: 'var(--foreground-secondary)' }}>
                              <span>{c.claim}</span>
                              <span style={{ color: 'var(--foreground-quaternary)' }}>
                                {' '}← 来源：{c.source_ids.join('、')}
                              </span>
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}

                    {/* Action bar */}
                    {!msg.isStreaming && msg.role !== 'system' && !msg.isError && (
                      <div className="flex items-center gap-3 mt-3 pt-2 border-t" style={{ borderColor: 'rgba(229, 229, 234, 0.5)' }}>
                        {!msg.isCorrected && msg.recordId && (
                          <button onClick={() => startCorrect(msg)}
                            className="text-[10px] transition-colors flex items-center gap-1"
                            style={{ color: 'var(--foreground-quaternary)' }}>
                            <Pencil className="w-3 h-3" /> 纠错
                          </button>
                        )}
                        {msg.isCorrected && (
                          <span className="text-[10px] flex items-center gap-1" style={{ color: 'var(--state-success)' }}>
                            <CheckCircle2 className="w-3 h-3" /> 已纠错
                          </span>
                        )}
                      </div>
                    )}
                  </div>
                )}
              </div>

              {msg.role === 'user' && (
                <div className="w-7 h-7 rounded-full flex items-center justify-center flex-shrink-0"
                  style={{ background: 'var(--background-muted)' }}>
                  <User className="w-3.5 h-3.5" style={{ color: 'var(--foreground-tertiary)' }} />
                </div>
              )}
            </div>
          ))}
          <div ref={messagesEndRef} />
        </div>

        {/* Input */}
        <div className="border-t pt-4" style={{ borderColor: 'var(--border)' }}>
          {activeRecordId && (
            <div className="flex items-center gap-2 mb-2 text-xs" style={{ color: 'var(--foreground-quaternary)' }}>
              <CornerDownRight className="w-3 h-3" />
              追问中 · <button onClick={() => { setActiveRecordId(null); setMessages([]) }}
                className="hover:underline" style={{ color: 'var(--brand-500)' }}>新话题</button>
            </div>
          )}
          <div className="flex items-end gap-2">
            <textarea
              ref={inputRef}
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              rows={2}
              className="flex-1 resize-none input-field text-sm"
              placeholder={activeRecordId ? '继续追问...' : '输入你的问题（Enter 发送，Shift+Enter 换行）'}
              disabled={loading}
            />
            {loading ? (
              <button onClick={cancelStreaming} className="btn-secondary text-sm flex items-center gap-1">
                <Loader2 className="w-4 h-4 animate-spin" /> 停止
              </button>
            ) : (
              <button onClick={handleSubmit} disabled={!input.trim()}
                className="btn-primary flex items-center gap-1.5">
                <Send className="w-4 h-4" /> 发送
              </button>
            )}
          </div>
        </div>
      </div>

      {/* Correction Modal */}
      {correctingId && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4"
          style={{ background: 'rgba(0, 0, 0, 0.32)', backdropFilter: 'blur(4px)' }}
          onClick={() => setCorrectingId(null)}>
          <div className="max-w-lg w-full p-6 animate-slide-up"
            style={{ background: 'var(--background)', borderRadius: 'calc(var(--radius) * 1.1)', boxShadow: 'var(--shadow-apple-xl)' }}
            onClick={e => e.stopPropagation()}>
            <div className="flex items-center gap-2 mb-4">
              <PenSquare className="w-5 h-5" style={{ color: 'var(--brand-500)' }} />
              <h2 className="text-lg font-semibold" style={{ color: 'var(--foreground)' }}>纠错回答</h2>
              <span className="text-xs px-2 py-0.5 rounded ml-auto"
                style={{ background: 'var(--state-warning-surface)', color: 'var(--state-warning)' }}>
                提交后进入审核
              </span>
            </div>
            <textarea value={correctText} onChange={e => setCorrectText(e.target.value)}
              className="w-full input-field text-sm min-h-[150px]" />
            <input value={correctNotes} onChange={e => setCorrectNotes(e.target.value)}
              className="w-full input-field text-sm mt-2" placeholder="纠错说明（可选）" />

            {/* v2.8.56: 颗粒度选择 */}
            <div className="mt-3 grid grid-cols-2 gap-3">
              <div>
                <label className="text-xs mb-1 block" style={{ color: 'var(--foreground-tertiary)' }}>
                  纠正颗粒度
                </label>
                <select
                  value={correctTargetType}
                  onChange={e => setCorrectTargetType(e.target.value as 'requirement' | 'functional_point')}
                  className="w-full input-field text-sm"
                >
                  <option value="requirement">整篇需求</option>
                  <option value="functional_point">单个功能点</option>
                </select>
              </div>
              <div>
                <label className="text-xs mb-1 block" style={{ color: 'var(--foreground-tertiary)' }}>
                  关联模块
                </label>
                <input
                  value={correctModule}
                  onChange={e => setCorrectModule(e.target.value)}
                  className="w-full input-field text-sm"
                  placeholder="模块名（用于按模块筛选审核）"
                  list="module-list-correct"
                />
                <datalist id="module-list-correct">
                  {availableModules.map(m => (
                    <option key={m.id} value={m.name}>{m.name}</option>
                  ))}
                </datalist>
              </div>
            </div>
            {correctTargetType === 'functional_point' && (
              <input
                value={correctTargetId}
                onChange={e => setCorrectTargetId(e.target.value)}
                className="w-full input-field text-sm mt-2"
                placeholder="功能点 ID（FP-xxx，纠正后将替换原 FP 向量）"
              />
            )}

            <div className="flex justify-end gap-3 mt-4">
              <button onClick={() => setCorrectingId(null)} className="btn-secondary text-sm">取消</button>
              <button onClick={() => submitCorrect(messages.find(m => m.id === correctingId)!)} disabled={!correctText.trim()}
                className="btn-primary text-sm"><Save className="w-4 h-4" /> 提交审核</button>
            </div>
          </div>
        </div>
      )}

      {/* History sidebar */}
      {showHistory && (
        <div className="fixed inset-0 z-50 flex justify-end" onClick={() => setShowHistory(false)}>
          <div className="w-[420px] h-full flex flex-col"
            style={{ background: 'var(--background)', boxShadow: 'var(--shadow-apple-xl)' }}
            onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between p-4 border-b" style={{ borderColor: 'var(--border)' }}>
              <h2 className="text-base font-semibold" style={{ color: 'var(--foreground)' }}>问答历史</h2>
              <button onClick={() => setShowHistory(false)}><X className="w-5 h-5" style={{ color: 'var(--foreground-quaternary)' }} /></button>
            </div>
            <div className="flex-1 overflow-y-auto p-3 space-y-2">
              {historyRecords.map(r => (
                <div key={r.id} onClick={() => handleHistoryClick(r.id)}
                  className="p-3 rounded-xl text-sm cursor-pointer transition-colors border border-transparent"
                  style={{ background: 'var(--background-muted)' }}>
                  <p className="font-medium" style={{ color: 'var(--foreground-secondary)' }}>{r.question}</p>
                  <p className="text-xs mt-1 line-clamp-2" style={{ color: 'var(--foreground-quaternary)' }}>{r.answer}</p>
                  <p className="text-[10px] mt-2" style={{ color: 'var(--foreground-quaternary)' }}>{r.created_at?.slice(0, 16)}</p>
                </div>
              ))}
              {historyRecords.length === 0 && (
                <p className="text-center py-12 text-sm" style={{ color: 'var(--foreground-quaternary)' }}>暂无问答记录</p>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
