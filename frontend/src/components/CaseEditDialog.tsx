import { useState, useEffect } from 'react'
import { X, Plus, Trash2, ChevronUp, ChevronDown, Save, Loader2 } from 'lucide-react'
import { testCases as tcApi } from '../api/client'
import type { TestCase } from '../types'
import { caseTypeLabel } from '../utils/labels'

interface Props {
  caseData: TestCase
  onClose: () => void
  onSaved: (updated: TestCase) => void
}

const CASE_TYPES = ['functional', 'boundary', 'exception', 'security', 'performance', 'regression', 'integration', 'ui', 'api', 'data']
const PRIORITIES = ['P0', 'P1', 'P2', 'P3']

export default function CaseEditDialog({ caseData, onClose, onSaved }: Props) {
  // 新增模式：caseData.id 为空字符串表示新建用例，调用 create 而非 update
  const isCreate = !caseData.id
  const [title, setTitle] = useState(caseData.title || '')
  const [type, setType] = useState(caseData.type || 'functional')
  const [priority, setPriority] = useState(caseData.priority || 'P1')
  const [description, setDescription] = useState<string>(typeof caseData.case_json?.description === 'string' ? caseData.case_json.description as string : (caseData.description || ''))
  const [preconditions, setPreconditions] = useState<string[]>(caseData.preconditions || [])
  const [steps, setSteps] = useState<{ step: number; action: string; expected: string }[]>(
    (caseData.steps || []).map((s, i) => ({ step: s.step || i + 1, action: s.action || '', expected: s.expected || '' }))
  )
  const [expected, setExpected] = useState<string>(typeof caseData.case_json?.expected === 'string' ? caseData.case_json.expected as string : '')
  const [assertions, setAssertions] = useState<Array<{ target: string; comparison: string; value: string }>>(
    Array.isArray(caseData.case_json?.assertions) ? (caseData.case_json!.assertions as any[]) : []
  )
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  // ESC 关闭
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  const handleStepChange = (i: number, field: 'action' | 'expected', v: string) => {
    setSteps(prev => prev.map((s, idx) => idx === i ? { ...s, [field]: v } : s))
  }
  const addStep = () => setSteps(prev => [...prev, { step: prev.length + 1, action: '', expected: '' }])
  const removeStep = (i: number) => setSteps(prev => prev.filter((_, idx) => idx !== i).map((s, idx) => ({ ...s, step: idx + 1 })))
  const moveStep = (i: number, dir: -1 | 1) => {
    setSteps(prev => {
      const arr = [...prev]
      const j = i + dir
      if (j < 0 || j >= arr.length) return arr
      ;[arr[i], arr[j]] = [arr[j], arr[i]]
      return arr.map((s, idx) => ({ ...s, step: idx + 1 }))
    })
  }

  const handlePreChange = (i: number, v: string) => {
    setPreconditions(prev => prev.map((p, idx) => idx === i ? v : p))
  }
  const addPre = () => setPreconditions(prev => [...prev, ''])
  const removePre = (i: number) => setPreconditions(prev => prev.filter((_, idx) => idx !== i))

  const handleAssertChange = (i: number, field: 'target' | 'comparison' | 'value', v: string) => {
    setAssertions(prev => prev.map((a, idx) => idx === i ? { ...a, [field]: v } : a))
  }
  const addAssert = () => setAssertions(prev => [...prev, { target: '', comparison: 'equals', value: '' }])
  const removeAssert = (i: number) => setAssertions(prev => prev.filter((_, idx) => idx !== i))

  const handleSave = async () => {
    if (!title.trim()) { setError('用例标题不能为空'); return }
    if (steps.length === 0) { setError('至少需要一个测试步骤'); return }
    if (steps.some(s => !s.action.trim())) { setError('步骤操作不能为空'); return }

    setSaving(true)
    setError('')
    try {
      const payload = {
        title: title.trim(),
        type,
        priority,
        description: description.trim(),
        preconditions: preconditions.filter(p => p.trim()),
        steps: steps.filter(s => s.action.trim()),
        expected,
        assertions: assertions.filter(a => a.target.trim()),
      }
      if (isCreate) {
        // 新增模式：调用 create API，必须传 req_id
        const created = await tcApi.create({
          req_id: caseData.req_id,
          product_id: caseData.product_id || undefined,
          title: payload.title,
          type: payload.type,
          priority: payload.priority,
          description: payload.description,
          preconditions: payload.preconditions,
          steps: payload.steps,
          expected: payload.expected,
          assertions: payload.assertions,
        })
        onSaved({
          ...caseData,
          id: created.id,
          req_id: created.req_id,
          product_id: created.product_id,
          title: created.title,
          type: created.type,
          priority: created.priority,
          status: created.status,
          description: payload.description,
          preconditions: payload.preconditions,
          steps: payload.steps,
          case_json: { ...payload, source: 'manual' },
        })
      } else {
        // 编辑模式：调用 update API
        await tcApi.update(caseData.id, payload)
        onSaved({
          ...caseData,
          title: payload.title,
          type: payload.type,
          priority: payload.priority,
          description: payload.description,
          preconditions: payload.preconditions,
          steps: payload.steps,
          status: 'reviewed',
          case_json: { ...(caseData.case_json || {}), ...payload },
        })
      }
    } catch (e: any) {
      setError(e.message || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4" onClick={onClose}>
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-3xl max-h-[92vh] flex flex-col" onClick={e => e.stopPropagation()}>
        {/* 头部 */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100">
          <div className="flex items-center gap-2">
            <h3 className="text-base font-semibold text-slate-800">{isCreate ? '新增用例' : '编辑用例'}</h3>
            <span className="text-xs font-mono text-slate-400">{caseData.id || '新建'}</span>
            {isCreate && (
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-50 text-emerald-700 border border-emerald-100">关联需求 {caseData.req_id}</span>
            )}
          </div>
          <button onClick={onClose} className="btn-ghost p-1 text-slate-400 hover:text-slate-600"><X className="w-5 h-5" /></button>
        </div>

        {/* 内容 */}
        <div className="flex-1 overflow-y-auto px-6 py-5 space-y-5">
          {/* 基础信息 */}
          <div className="grid grid-cols-12 gap-3">
            <div className="col-span-8">
              <label className="block text-xs font-semibold text-slate-600 mb-1">用例标题 <span className="text-red-500">*</span></label>
              <input value={title} onChange={e => setTitle(e.target.value)}
                className="input-field text-sm" placeholder="请输入用例标题" />
            </div>
            <div className="col-span-2">
              <label className="block text-xs font-semibold text-slate-600 mb-1">类型</label>
              <select value={type} onChange={e => setType(e.target.value)} className="select-field text-sm w-full">
                {CASE_TYPES.map(t => <option key={t} value={t}>{caseTypeLabel(t)}</option>)}
              </select>
            </div>
            <div className="col-span-2">
              <label className="block text-xs font-semibold text-slate-600 mb-1">优先级</label>
              <select value={priority} onChange={e => setPriority(e.target.value)} className="select-field text-sm w-full">
                {PRIORITIES.map(p => <option key={p} value={p}>{p}</option>)}
              </select>
            </div>
          </div>

          {/* 描述 */}
          <div>
            <label className="block text-xs font-semibold text-slate-600 mb-1">用例描述</label>
            <textarea value={description} onChange={e => setDescription(e.target.value)} rows={2}
              className="input-field text-sm resize-none" placeholder="用例的简要描述" />
          </div>

          {/* 前置条件 */}
          <div>
            <div className="flex items-center justify-between mb-1">
              <label className="text-xs font-semibold text-slate-600">前置条件</label>
              <button onClick={addPre} className="btn-ghost text-xs text-primary-600 flex items-center gap-1"><Plus className="w-3 h-3" />添加</button>
            </div>
            <div className="space-y-1.5">
              {preconditions.length === 0 && <p className="text-xs text-slate-400 italic">无前置条件</p>}
              {preconditions.map((p, i) => (
                <div key={i} className="flex items-center gap-2">
                  <span className="text-xs text-slate-400 w-5 shrink-0">{i + 1}.</span>
                  <input value={p} onChange={e => handlePreChange(i, e.target.value)}
                    className="input-field text-sm flex-1" placeholder="前置条件" />
                  <button onClick={() => removePre(i)} className="btn-ghost p-1 text-slate-400 hover:text-red-500"><Trash2 className="w-3.5 h-3.5" /></button>
                </div>
              ))}
            </div>
          </div>

          {/* 测试步骤 */}
          <div>
            <div className="flex items-center justify-between mb-1">
              <label className="text-xs font-semibold text-slate-600">测试步骤 <span className="text-red-500">*</span></label>
              <button onClick={addStep} className="btn-ghost text-xs text-primary-600 flex items-center gap-1"><Plus className="w-3 h-3" />添加步骤</button>
            </div>
            <div className="space-y-2">
              {steps.length === 0 && <p className="text-xs text-red-500 italic">至少需要一个测试步骤</p>}
              {steps.map((s, i) => (
                <div key={i} className="border border-slate-200 rounded-lg p-2.5 bg-slate-50/40">
                  <div className="flex items-center justify-between mb-1.5">
                    <span className="text-xs font-semibold text-slate-500">步骤 {i + 1}</span>
                    <div className="flex items-center gap-0.5">
                      <button onClick={() => moveStep(i, -1)} disabled={i === 0} className="btn-ghost p-1 text-slate-400 hover:text-slate-600 disabled:opacity-30"><ChevronUp className="w-3.5 h-3.5" /></button>
                      <button onClick={() => moveStep(i, 1)} disabled={i === steps.length - 1} className="btn-ghost p-1 text-slate-400 hover:text-slate-600 disabled:opacity-30"><ChevronDown className="w-3.5 h-3.5" /></button>
                      <button onClick={() => removeStep(i)} className="btn-ghost p-1 text-slate-400 hover:text-red-500"><Trash2 className="w-3.5 h-3.5" /></button>
                    </div>
                  </div>
                  <input value={s.action} onChange={e => handleStepChange(i, 'action', e.target.value)}
                    className="input-field text-sm mb-1.5" placeholder="操作（如：用户点击「提交订单」按钮）" />
                  <input value={s.expected} onChange={e => handleStepChange(i, 'expected', e.target.value)}
                    className="input-field text-sm" placeholder="预期结果（如：系统生成订单号，状态为待支付）" />
                </div>
              ))}
            </div>
          </div>

          {/* 整体预期 */}
          <div>
            <label className="block text-xs font-semibold text-slate-600 mb-1">整体预期结果</label>
            <input value={expected} onChange={e => setExpected(e.target.value)}
              className="input-field text-sm" placeholder="可选，整个用例的最终预期" />
          </div>

          {/* 断言 */}
          <div>
            <div className="flex items-center justify-between mb-1">
              <label className="text-xs font-semibold text-slate-600">断言</label>
              <button onClick={addAssert} className="btn-ghost text-xs text-primary-600 flex items-center gap-1"><Plus className="w-3 h-3" />添加断言</button>
            </div>
            <div className="space-y-1.5">
              {assertions.length === 0 && <p className="text-xs text-slate-400 italic">无断言</p>}
              {assertions.map((a, i) => (
                <div key={i} className="grid grid-cols-12 gap-2 items-center">
                  <input value={a.target} onChange={e => handleAssertChange(i, 'target', e.target.value)}
                    className="input-field text-sm col-span-5" placeholder="目标字段（如 order.status）" />
                  <select value={a.comparison} onChange={e => handleAssertChange(i, 'comparison', e.target.value)}
                    className="select-field text-sm col-span-3">
                    <option value="equals">等于</option>
                    <option value="not_equals">不等于</option>
                    <option value="contains">包含</option>
                    <option value="matches">正则匹配</option>
                    <option value="greater_than">大于</option>
                    <option value="less_than">小于</option>
                  </select>
                  <input value={a.value} onChange={e => handleAssertChange(i, 'value', e.target.value)}
                    className="input-field text-sm col-span-3" placeholder="期望值" />
                  <button onClick={() => removeAssert(i)} className="btn-ghost p-1 text-slate-400 hover:text-red-500 col-span-1 justify-self-end"><Trash2 className="w-3.5 h-3.5" /></button>
                </div>
              ))}
            </div>
          </div>

          {error && <div className="text-xs text-red-500 bg-red-50 px-3 py-2 rounded-lg">{error}</div>}
        </div>

        {/* 底部 */}
        <div className="flex items-center justify-end gap-2 px-6 py-3 border-t border-slate-100 bg-slate-50/50">
          <button onClick={onClose} className="btn-secondary text-sm">取消</button>
          <button onClick={handleSave} disabled={saving}
            className="btn-primary text-sm flex items-center gap-2 disabled:opacity-50">
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
            {isCreate ? '创建' : '保存'}
          </button>
        </div>
      </div>
    </div>
  )
}
