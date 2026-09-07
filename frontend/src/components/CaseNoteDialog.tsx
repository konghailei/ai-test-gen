import { useMemo, useState, useEffect } from 'react'
import { X, Save, AlertTriangle, CheckCircle2, RotateCcw, FileText } from 'lucide-react'
import type { TestCase } from '../types'
import {
  buildCaseNote, parseCaseNote, formatNoteErrors, NOTE_FIELDS, type ParsedNote,
} from '../utils/caseNote'

interface Props {
  caseData: TestCase
  /** 当前备注文本（可能已被改过） */
  noteText: string
  onClose: () => void
  onSaved: (text: string, parsed: ParsedNote) => void
}

/**
 * 用例备注文本编辑弹窗。
 *
 * 备注是 XMind 节点上的说明文字，按固定格式书写后，保存时可反解回用例字段
 * （优先级 / 类型 / 描述 / 前置条件 / 测试步骤）。
 * 输入过程中实时校验，格式有误时逐行提示位置与原因，错误未清零不允许保存。
 */
export default function CaseNoteDialog({ caseData, noteText, onClose, onSaved }: Props) {
  const [text, setText] = useState(noteText)
  const baseline = useMemo(() => buildCaseNote(caseData), [caseData])
  const result = useMemo(() => parseCaseNote(text), [text])
  const dirty = text.trim() !== baseline.trim()

  // ESC 关闭
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  const handleSave = () => {
    if (!result.ok) return
    onSaved(text, result.data)
  }

  const changedFields: string[] = []
  if (result.ok) {
    if (result.data.priority && result.data.priority !== caseData.priority) changedFields.push('优先级')
    if (result.data.type && result.data.type !== caseData.type) changedFields.push('类型')
    if (result.data.description !== undefined && result.data.description !== (caseData.description || '')) changedFields.push('描述')
    if (result.data.preconditions) changedFields.push(`前置条件(${result.data.preconditions.length})`)
    if (result.data.steps) changedFields.push(`测试步骤(${result.data.steps.length})`)
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4" onClick={onClose}>
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-2xl max-h-[92vh] flex flex-col" onClick={e => e.stopPropagation()}>
        {/* 头部 */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100">
          <div className="flex items-center gap-2 min-w-0">
            <FileText className="w-4 h-4 text-primary-500 shrink-0" />
            <h3 className="text-base font-semibold text-slate-800">编辑备注</h3>
            <span className="text-sm text-slate-500 truncate">{caseData.title || '未命名用例'}</span>
            <span className="text-xs font-mono text-slate-400 shrink-0">{caseData.id}</span>
          </div>
          <button onClick={onClose} className="btn-ghost p-1 text-slate-400 hover:text-slate-600">
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* 格式说明 */}
        <div className="px-6 pt-4">
          <p className="text-xs text-slate-500 leading-relaxed">
            按固定格式书写，保存后自动同步到用例。可用字段：
            <span className="font-medium text-slate-700">{NOTE_FIELDS.map(f => `【${f}】`).join(' ')}</span>
            ；步骤写「<span className="font-mono">步骤1: 操作</span>」，预期写「<span className="font-mono">预期: 结果</span>」（须紧跟步骤行）；前置条件写「<span className="font-mono">1. 内容</span>」。
            未书写的字段保持原样，不会清空。
          </p>
        </div>

        {/* 编辑区 */}
        <div className="flex-1 overflow-y-auto px-6 py-4">
          <textarea
            value={text}
            onChange={e => setText(e.target.value)}
            spellCheck={false}
            rows={16}
            className="input-field text-sm w-full font-mono leading-relaxed resize-none"
            placeholder={'【优先级】P1\n【类型】功能\n【前置条件】\n1. 已部署测试环境\n【测试步骤】\n步骤1: 输入账号密码\n  预期: 登录成功'}
          />
        </div>

        {/* 校验结果 */}
        <div className="px-6 pb-4">
          {result.errors.length > 0 ? (
            <div className="rounded-lg border border-red-200 bg-red-50/60 p-3">
              <div className="flex items-center gap-1.5 text-red-600 text-xs font-semibold mb-1.5">
                <AlertTriangle className="w-3.5 h-3.5" />
                格式有误（{result.errors.length} 处），请修正后再保存
              </div>
              <ul className="space-y-1 max-h-40 overflow-y-auto">
                {formatNoteErrors(result.errors).map((msg, i) => (
                  <li key={i} className="text-xs text-red-600 font-mono leading-relaxed">· {msg}</li>
                ))}
              </ul>
            </div>
          ) : (
            <div className="rounded-lg border border-emerald-200 bg-emerald-50/60 p-3">
              <div className="flex items-center gap-1.5 text-emerald-700 text-xs font-semibold">
                <CheckCircle2 className="w-3.5 h-3.5" />
                格式正确
                {dirty && changedFields.length > 0 && (
                  <span className="font-normal text-emerald-600">· 将更新 {changedFields.join('、')}</span>
                )}
                {!dirty && <span className="font-normal text-emerald-600">· 未做修改</span>}
              </div>
            </div>
          )}
        </div>

        {/* 底部 */}
        <div className="flex items-center justify-between gap-2 px-6 py-4 border-t border-slate-100">
          <button
            onClick={() => setText(baseline)}
            disabled={!dirty}
            className="btn-secondary text-xs flex items-center gap-1 disabled:opacity-40"
            title="恢复为用例当前内容"
          >
            <RotateCcw className="w-3.5 h-3.5" />还原
          </button>
          <div className="flex items-center gap-2">
            <button onClick={onClose} className="btn-secondary text-sm">取消</button>
            <button
              onClick={handleSave}
              disabled={!result.ok}
              className="btn-primary text-sm flex items-center gap-1.5 disabled:opacity-40"
              title={result.ok ? '保存到导图节点（点页面「保存」按钮后写入系统）' : '格式有误，无法保存'}
            >
              <Save className="w-4 h-4" />保存到节点
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
