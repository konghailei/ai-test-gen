import { useState, useRef, useEffect } from 'react'
import {
  MessageSquare, Send, Bot, User, CheckCircle2, Pencil,
  X, Loader2, BookOpen, FileText, TestTube, AlertCircle, History,
  PenSquare, Save, CornerDownRight,
} from 'lucide-react'
import { knowledgeQA } from '../api/client'
import { useProduct } from '../store/ProductContext'
import type { KbQAReference } from '../types'
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

  // Correction
  const [correctingId, setCorrectingId] = useState<string | null>(null)
  const [correctText, setCorrectText] = useState('')
  const [correctNotes, setCorrectNotes] = useState('')
  const [archiveLoading, setArchiveLoading] = useState(false)

  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const abortRef = useRef<AbortController | null>(null)

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const loadHistory = async (page = 1) => {
    setHistoryLoading(true)
    try {
      const r = await knowledgeQA.history(page, 20)
      setHistoryRecords(prev => page === 1 ? r.items : [...prev, ...r.items])
      setHistoryTotal(r.total)
    } catch { }
    setHistoryLoading(false)
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
      body: JSON.stringify({ question: q, product_id: selectedProductId || undefined }),
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

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        const chunk = decoder.decode(value, { stream: true })
        const lines = chunk.split('\n')

        for (const line of lines) {
          if (line.startsWith('event: ')) {
            eventType = line.slice(7).trim()
          } else if (line.startsWith('data: ')) {
            dataLines.push(line.slice(6))
          } else if (line === '' && dataLines.length > 0) {
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
                  m.id === asstId ? { ...m, isStreaming: false, recordId: doneData.id } : m
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
  }

  const submitCorrect = async (msg: ChatMessage) => {
    if (!msg.recordId || !correctText.trim()) return
    try {
      await knowledgeQA.correct(msg.recordId, correctText.trim(), correctNotes.trim())
      setMessages(prev => prev.map(m => m.id === msg.id ? {
        ...m, isCorrected: true, correctedContent: correctText.trim(), correctionNotes: correctNotes.trim(),
      } : m))
      setCorrectingId(null)
    } catch (e: any) { alert('纠错保存失败: ' + (e?.message || '')) }
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
          <h1 className="page-title flex items-center gap-2"><MessageSquare className="w-5 h-5 text-primary-500" /> 知识问答</h1>
          <p className="text-sm text-slate-500 mt-1">基于知识库的智能问答 — 输入问题获取 AI 分析</p>
        </div>
        <button onClick={() => { setShowHistory(true); loadHistory(1) }} className="btn-secondary text-sm">
          <History className="w-4 h-4" /> 问答历史
        </button>
      </div>

      {/* Chat area */}
      <div className="card p-6 min-h-[500px] flex flex-col">
        {/* Messages */}
        <div className="flex-1 space-y-5 mb-4 max-h-[60vh] overflow-y-auto">
          {messages.length === 0 && (
            <div className="flex flex-col items-center justify-center py-16 text-slate-400">
              <MessageSquare className="w-12 h-12 mb-3 opacity-30" />
              <p className="text-sm font-medium">输入问题，基于知识库进行智能问答</p>
              <div className="flex flex-wrap gap-2 mt-6 max-w-lg justify-center">
                {[
                  '这个产品的核心功能是什么？',
                  '登录模块有哪些测试用例？',
                  '系统有哪些关键业务规则？',
                ].map((q, i) => (
                  <button key={i} onClick={() => { setInput(q); inputRef.current?.focus() }}
                    className="px-3 py-1.5 text-xs bg-slate-100 hover:bg-primary-50 text-slate-500 hover:text-primary-600 rounded-lg transition-colors">
                    {q}
                  </button>
                ))}
              </div>
            </div>
          )}

          {messages.map(msg => (
            <div key={msg.id} className={`flex gap-3 ${msg.role === 'user' ? 'justify-end' : ''}`}>
              {msg.role !== 'user' && (
                <div className={`w-7 h-7 rounded-full flex items-center justify-center flex-shrink-0 ${
                  msg.isError ? 'bg-red-100' : 'bg-primary-100'
                }`}>
                  {msg.isError ? <AlertCircle className="w-3.5 h-3.5 text-red-500" /> : <Bot className="w-3.5 h-3.5 text-primary-600" />}
                </div>
              )}

              <div className={`max-w-[80%] ${msg.role === 'user' ? '' : ''}`}>
                {msg.role === 'user' && (
                  <div className="px-4 py-2.5 bg-primary-500 text-white rounded-2xl rounded-br-md text-sm shadow-sm">
                    {msg.content}
                  </div>
                )}

                {(msg.role === 'assistant' || msg.role === 'system') && (
                  <div className={`rounded-2xl rounded-bl-md px-4 py-3 border ${
                    msg.isError ? 'bg-red-50 border-red-100 text-red-700' : 'bg-slate-50 border-slate-100 text-slate-700'
                  }`}>
                    {/* Markdown rendered content */}
                    <div className="text-sm leading-relaxed space-y-2 markdown-body">
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>
                        {msg.isCorrected ? (msg.correctedContent || '') : msg.content || '思考中...'}
                      </ReactMarkdown>
                    </div>

                    {msg.isStreaming && (
                      <span className="inline-block w-2 h-4 bg-primary-400 animate-pulse rounded-sm mt-1" />
                    )}

                    {/* Sources */}
                    {msg.sources && msg.sources.length > 0 && !msg.isStreaming && (
                      <div className="mt-4 pt-3 border-t border-slate-200">
                        <p className="text-xs font-medium text-slate-500 mb-2 flex items-center gap-1">
                          <BookOpen className="w-3 h-3" /> 参考来源
                        </p>
                        <div className="space-y-1">
                          {msg.sources.map((s, i) => (
                            <div key={i} className="flex items-center gap-2 text-xs text-slate-500 py-0.5">
                              {s.type === 'requirement'
                                ? <FileText className="w-3 h-3 text-primary-400 flex-shrink-0" />
                                : <TestTube className="w-3 h-3 text-amber-400 flex-shrink-0" />}
                              <span className="truncate">{s.title}</span>
                              <span className="text-slate-300 flex-shrink-0">{(s.similarity * 100).toFixed(0)}%</span>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}

                    {/* Action bar */}
                    {!msg.isStreaming && msg.role !== 'system' && !msg.isError && (
                      <div className="flex items-center gap-3 mt-3 pt-2 border-t border-slate-200/50">
                        {!msg.isCorrected && msg.recordId && (
                          <button onClick={() => startCorrect(msg)}
                            className="text-[10px] text-slate-400 hover:text-primary-500 transition-colors flex items-center gap-1">
                            <Pencil className="w-3 h-3" /> 纠错
                          </button>
                        )}
                        {msg.isCorrected && (
                          <span className="text-[10px] text-emerald-500 flex items-center gap-1">
                            <CheckCircle2 className="w-3 h-3" /> 已纠错
                          </span>
                        )}
                      </div>
                    )}
                  </div>
                )}
              </div>

              {msg.role === 'user' && (
                <div className="w-7 h-7 rounded-full bg-slate-200 flex items-center justify-center flex-shrink-0">
                  <User className="w-3.5 h-3.5 text-slate-500" />
                </div>
              )}
            </div>
          ))}
          <div ref={messagesEndRef} />
        </div>

        {/* Input */}
        <div className="border-t border-slate-100 pt-4">
          {activeRecordId && (
            <div className="flex items-center gap-2 mb-2 text-xs text-slate-400">
              <CornerDownRight className="w-3 h-3" />
              追问中 · <button onClick={() => { setActiveRecordId(null); setMessages([]) }}
                className="text-primary-500 hover:underline">新话题</button>
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
        <div className="fixed inset-0 z-50 bg-black/30 backdrop-blur-sm flex items-center justify-center p-4" onClick={() => setCorrectingId(null)}>
          <div className="bg-white rounded-2xl shadow-xl max-w-lg w-full p-6 animate-slide-up" onClick={e => e.stopPropagation()}>
            <div className="flex items-center gap-2 mb-4">
              <PenSquare className="w-5 h-5 text-primary-500" />
              <h2 className="text-lg font-semibold text-slate-800">纠错回答</h2>
            </div>
            <textarea value={correctText} onChange={e => setCorrectText(e.target.value)}
              className="w-full input-field text-sm min-h-[150px]" />
            <input value={correctNotes} onChange={e => setCorrectNotes(e.target.value)}
              className="w-full input-field text-sm mt-2" placeholder="纠错说明（可选）" />
            <div className="flex justify-end gap-3 mt-4">
              <button onClick={() => setCorrectingId(null)} className="btn-secondary text-sm">取消</button>
              <button onClick={() => submitCorrect(messages.find(m => m.id === correctingId)!)} disabled={!correctText.trim()}
                className="btn-primary text-sm"><Save className="w-4 h-4" /> 保存纠错</button>
            </div>
          </div>
        </div>
      )}

      {/* History sidebar */}
      {showHistory && (
        <div className="fixed inset-0 z-50 flex justify-end" onClick={() => setShowHistory(false)}>
          <div className="bg-white shadow-2xl w-[420px] h-full flex flex-col" onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between p-4 border-b border-slate-100">
              <h2 className="text-base font-semibold text-slate-800">问答历史</h2>
              <button onClick={() => setShowHistory(false)}><X className="w-5 h-5 text-slate-400" /></button>
            </div>
            <div className="flex-1 overflow-y-auto p-3 space-y-2">
              {historyRecords.map(r => (
                <div key={r.id} className="p-3 bg-slate-50 rounded-xl text-sm">
                  <p className="font-medium text-slate-700">{r.question}</p>
                  <p className="text-xs text-slate-400 mt-1 line-clamp-2">{r.answer}</p>
                  <p className="text-[10px] text-slate-400 mt-2">{r.created_at?.slice(0, 16)}</p>
                </div>
              ))}
              {historyRecords.length === 0 && (
                <p className="text-slate-400 text-center py-12 text-sm">暂无问答记录</p>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
