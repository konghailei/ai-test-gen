import { useState, useEffect } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { ArrowLeft, Send, Loader2, MessageCircle, AlertCircle, CheckCircle2, FileText } from 'lucide-react'
import { requirements as reqApi } from '../api/client'
import type { ClarifyQuestion } from '../types'

export default function RequirementClarify() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()

  const [loading, setLoading] = useState(true)
  const [questions, setQuestions] = useState<ClarifyQuestion[]>([])
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const [extraNotes, setExtraNotes] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState(false)

  useEffect(() => {
    if (!id) return
    setLoading(true)
    reqApi.getClarifyState(id).then(data => {
      const qs = data?.questions || []
      setQuestions(qs)
      // 预填历史答案
      const prev = data?.previous_answers || {}
      const init: Record<string, string> = {}
      qs.forEach(q => { init[q.id] = prev[q.id] || '' })
      setAnswers(init)
    }).catch(e => {
      setError(e?.message || '加载澄清问题失败')
    }).finally(() => setLoading(false))
  }, [id])

  const handleSubmit = async () => {
    if (!id) return
    setSubmitting(true)
    setError('')
    try {
      await reqApi.submitClarify(id, answers, extraNotes)
      setDone(true)
      setTimeout(() => navigate(`/requirements/${id}/generate`, { replace: true }), 1000)
    } catch (e: any) {
      setError(e?.message || '提交失败')
    } finally {
      setSubmitting(false)
    }
  }

  if (loading) return <div className="space-y-4 max-w-3xl mx-auto pt-8">{[1,2,3].map(i => <div key={i} className="skeleton h-20" />)}</div>

  return (
    <div className="max-w-3xl mx-auto space-y-6 pt-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <button onClick={() => navigate(`/requirements/${id}`)} className="btn-ghost">
          <ArrowLeft className="w-4 h-4" /> 返回需求详情
        </button>
      </div>

      {done ? (
        <div className="bg-gradient-to-r from-emerald-50 to-green-50 border border-emerald-100 rounded-2xl p-8 text-center">
          <CheckCircle2 className="w-12 h-12 text-emerald-500 mx-auto mb-3" />
          <h2 className="text-lg font-semibold text-emerald-800 mb-1">澄清已提交</h2>
          <p className="text-sm text-emerald-600">需求已确认，即将跳转到用例生成页面...</p>
        </div>
      ) : (
        <>
          {/* 页面标题 */}
          <div className="card">
            <div className="flex items-center gap-3 mb-4">
              <MessageCircle className="w-6 h-6 text-primary-500" />
              <div>
                <h1 className="text-xl font-bold text-slate-900">需求澄清</h1>
                <p className="text-sm text-slate-400">补充以下信息可让需求分析更精确</p>
              </div>
            </div>

            {error && (
              <div className="p-3 bg-red-50 border border-red-200 rounded-xl text-sm text-red-700 flex items-start gap-2 mb-4">
                <AlertCircle className="w-4 h-4 mt-0.5 flex-shrink-0" />
                <span>{error}</span>
              </div>
            )}

            {questions.length === 0 ? (
              <div className="text-center py-8 text-slate-400">
                <FileText className="w-10 h-10 mx-auto mb-2 opacity-50" />
                <p>未检测到需要澄清的问题</p>
                <p className="text-xs mt-1">当前需求信息已足够完整</p>
              </div>
            ) : (
              <div className="space-y-4">
                <p className="text-sm text-slate-500">共 {questions.length} 个问题需要确认：</p>
                {questions.map((q) => {
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
                          q.priority === 'important' ? 'text-amber-500' : 'text-primary-500'
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
                              <button key={oi}
                                onClick={() => {
                                  if (isMulti) {
                                    const parts = currentVal ? currentVal.split(',').filter(Boolean) : []
                                    const idx = parts.indexOf(opt)
                                    const next = idx >= 0 ? parts.filter(p => p !== opt) : [...parts, opt]
                                    setAnswers({...answers, [q.id]: next.join(',')})
                                  } else {
                                    setAnswers({...answers, [q.id]: currentVal === opt ? '' : opt})
                                  }
                                }}
                                className={`w-full text-left px-3 py-2 rounded-lg text-sm border transition-all ${
                                  isSelected ? 'bg-primary-50 border-primary-300 text-primary-700' : 'bg-white border-slate-200 text-slate-600 hover:border-slate-300'
                                }`}>
                                <span className="inline-flex items-center gap-2">
                                  <span className={`w-4 h-4 rounded-${isMulti ? 'sm' : 'full'} border-2 flex items-center justify-center ${isSelected ? 'border-primary-500 bg-primary-500' : 'border-slate-300'}`}>
                                    {isSelected && <CheckCircle2 className="w-3 h-3 text-white" />}
                                  </span>
                                  {opt}
                                </span>
                              </button>
                            )
                          })}
                          <textarea value={currentVal} onChange={e => setAnswers({...answers, [q.id]: e.target.value})}
                            rows={2} className="input-field mt-2 text-sm" placeholder={isMulti ? '选择以上选项或自行补充...' : '选择以上选项或自行填写...'} />
                        </div>
                      ) : (
                        <textarea value={currentVal} onChange={e => setAnswers({...answers, [q.id]: e.target.value})}
                          rows={2} className="input-field mt-2 text-sm" placeholder={q.default_answer || (q.priority === 'critical' ? '必填，请补充' : '选填，如有需要请补充...')} />
                      )}
                    </div>
                  )
                })}
              </div>
            )}

            {/* 额外补充说明 */}
            <div className="mt-6 pt-4 border-t border-slate-100">
              <label className="label">额外补充说明</label>
              <textarea value={extraNotes} onChange={e => setExtraNotes(e.target.value)}
                rows={3} className="input-field text-sm" placeholder="如有其他需要补充的信息，请在此填写（可选）" />
              <p className="text-xs text-slate-400 mt-1">补充说明将一并提交给 AI，帮助生成更准确的需求分析</p>
            </div>

            {/* 提交按钮 */}
            <div className="flex items-center justify-end gap-3 mt-6 pt-4 border-t border-slate-100">
              <button onClick={() => navigate(`/requirements/${id}`)} className="btn-secondary">
                暂不处理
              </button>
              <button onClick={handleSubmit} disabled={submitting} className="btn-primary">
                {submitting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
                {submitting ? '提交中...' : '提交并重新分析'}
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  )
}
