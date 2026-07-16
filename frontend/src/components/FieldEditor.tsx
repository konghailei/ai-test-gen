import { useState } from 'react'
import { Edit3, Save, X } from 'lucide-react'

/**
 * 通用 SRR 字段编辑器 — hover 显示编辑图标，点击进入内联编辑。
 *
 * @param label       标签文本（如「功能概述」）
 * @param fieldPath   字段路径，点号分隔（如 'requirement.summary' | 'flows.happy_path.steps'）
 * @param value       当前值（字符串/对象/数组）
 * @param valueType   值类型：'string' 直接编辑 | 'object[]' JSON 编辑 | 可扩展
 * @param onSave      保存回调 (fieldPath, newValue) => Promise<void>
 * @param render      展示模式渲染函数 (value, setEditMode) => JSX
 * @param placeholder 编辑模式占位符
 * @param disabled    是否禁用编辑（如归档状态）
 */
export default function FieldEditor({
  label,
  fieldPath,
  value,
  onSave,
  render,
  valueType = 'string',
  placeholder = '',
  disabled = false,
}: {
  label: string
  fieldPath: string
  value: unknown
  onSave: (path: string, val: unknown) => Promise<void>
  render: (value: unknown, toggle: () => void) => React.ReactNode
  valueType?: 'string' | 'string[]' | 'object[]' | 'json'
  placeholder?: string
  disabled?: boolean
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)

  const startEdit = () => {
    const text = serializeValue(value, valueType)
    setDraft(text)
    setEditing(true)
  }

  const cancelEdit = () => {
    setEditing(false)
    setDraft('')
  }

  const commitEdit = async () => {
    setSaving(true)
    try {
      const parsed = parseValue(draft, valueType)
      await onSave(fieldPath, parsed)
      setEditing(false)
    } catch {
      // 解析失败不保存，保留编辑状态让用户修正
    } finally {
      setSaving(false)
    }
  }

  if (editing) {
    return (
      <div>
        <h3 className="label">{label}</h3>
        <div className="flex items-start gap-2 mt-1">
          <textarea
            value={draft}
            onChange={e => setDraft(e.target.value)}
            className="input-field text-sm flex-1 resize-none font-mono"
            rows={Math.max(3, draft.split('\n').length)}
            placeholder={placeholder}
            autoFocus
          />
          <div className="flex flex-col gap-1">
            <button onClick={commitEdit} disabled={saving}
              className="p-1.5 bg-primary-500 text-white rounded-lg hover:bg-primary-600 disabled:opacity-50">
              <Save className="w-3.5 h-3.5" />
            </button>
            <button onClick={cancelEdit}
              className="p-1.5 bg-slate-100 text-slate-500 rounded-lg hover:bg-slate-200">
              <X className="w-3.5 h-3.5" />
            </button>
          </div>
        </div>
      </div>
    )
  }

  return <div className="group">{render(value, startEdit)}</div>
}

// ---- 序列化/反序列化工具 ----

function serializeValue(val: unknown, type: string): string {
  if (val == null) return ''
  switch (type) {
    case 'string':
      return String(val)
    case 'string[]':
      return (Array.isArray(val) ? val : []).join('\n')
    case 'object[]':
      if (!Array.isArray(val)) return '[]'
      // 去掉 id/order 等推导字段，只保留核心内容
      return JSON.stringify(
        val.map((item: any) => {
          const { order, ...rest } = item
          return rest
        }),
        null, 2,
      )
    case 'json':
      return JSON.stringify(val, null, 2)
    default:
      return JSON.stringify(val, null, 2)
  }
}

function parseValue(text: string, type: string): unknown {
  switch (type) {
    case 'string':
      return text
    case 'string[]':
      return text.split('\n').map(s => s.trim()).filter(Boolean)
    case 'object[]':
    case 'json': {
      const parsed = JSON.parse(text)
      if (type === 'object[]' && Array.isArray(parsed)) {
        // 补回 order 字段
        return parsed.map((item: any, i: number) => ({ ...item, order: i + 1 }))
      }
      return parsed
    }
    default:
      return text
  }
}
