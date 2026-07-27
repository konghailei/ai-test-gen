import { useState } from 'react'
import { Edit3, Save, X } from 'lucide-react'
import ArrayEditor from './ArrayEditor'

// ---- 类型选项 ----
const BR_TYPE_OPTIONS = [
  { value: 'validation', label: '校验' }, { value: 'calculation', label: '计算' },
  { value: 'workflow', label: '流程' }, { value: 'permission', label: '权限' },
  { value: 'security', label: '安全' }, { value: 'session', label: '会话' },
  { value: 'timeout', label: '超时' }, { value: 'rate_limit', label: '限流' },
  { value: 'notification', label: '通知' }, { value: 'data_retention', label: '数据保留' },
  { value: 'audit', label: '审计' }, { value: 'encryption', label: '加密' },
  { value: 'integration', label: '集成' }, { value: 'business', label: '业务' },
  { value: 'constraint', label: '约束' }, { value: 'data_integrity', label: '数据完整性' },
  { value: 'field_validation', label: '字段校验' }, { value: 'access_control', label: '访问控制' },
  { value: 'format', label: '格式' }, { value: 'mandatory', label: '必填' },
  { value: 'uniqueness', label: '唯一性' }, { value: 'authorization', label: '授权' },
  { value: 'authentication', label: '认证' }, { value: 'prerequisite', label: '前置条件' },
  { value: 'range', label: '范围' }, { value: 'enumeration', label: '枚举' },
  { value: 'data_format', label: '数据格式' }, { value: 'relationship', label: '关联' },
  { value: 'logic', label: '逻辑' }, { value: 'duplication', label: '去重' },
  { value: 'presentation', label: '展示' }, { value: 'initialization', label: '初始化' },
]
const AC_TYPE_OPTIONS = [
  { value: 'functional', label: '功能' }, { value: 'performance', label: '性能' },
  { value: 'security', label: '安全' }, { value: 'usability', label: '易用性' },
  { value: 'compatibility', label: '兼容性' }, { value: 'reliability', label: '可靠性' },
]
const FIELD_TYPE_OPTIONS = [
  { value: 'string', label: '字符串' }, { value: 'number', label: '数字' },
  { value: 'boolean', label: '布尔' }, { value: 'date', label: '日期' },
  { value: 'datetime', label: '日期时间' }, { value: 'email', label: '邮箱' },
  { value: 'url', label: 'URL' }, { value: 'array', label: '数组' },
  { value: 'object', label: '对象' },
]

// v2.22: 功能点子 SRR 新增字段的下拉选项
const TEST_POINT_CATEGORY_OPTIONS = [
  { value: 'functional', label: '功能' }, { value: 'boundary', label: '边界' },
  { value: 'exception', label: '异常' }, { value: 'security', label: '安全' },
  { value: 'performance', label: '性能' },
]
const ASSUMPTION_TYPE_OPTIONS = [
  { value: 'assumption', label: '假设' },
  { value: 'constraint', label: '约束' },
  { value: 'out_of_scope', label: '范围外' },
]

/**
 * 通用 SRR 字段编辑器。
 *
 * - 展示模式：显示标签 + 内容 + 编辑按钮（始终可见）
 * - 编辑模式：标签 + textarea + 保存/取消按钮
 *
 * @param label       标签文本（如「功能概述」）
 * @param fieldPath   字段路径，点号分隔
 * @param value       当前值
 * @param valueType   值类型：'string' | 'string[]' | 'object[]' | 'json'
 * @param onSave      保存回调 (fieldPath, newValue) => Promise<void>
 * @param render      展示模式内容渲染函数 (value) => JSX
 * @param placeholder 编辑模式占位符
 * @param disabled    是否禁用编辑（归档状态）
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
  render: (value: unknown) => React.ReactNode
  valueType?: 'string' | 'string[]' | 'object[]' | 'json'
  placeholder?: string
  disabled?: boolean
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [draftArr, setDraftArr] = useState<any[]>([])
  const [saving, setSaving] = useState(false)

  const isArrayEdit = valueType === 'object[]'
  const startEdit = () => {
    if (isArrayEdit && Array.isArray(value)) {
      setDraftArr(structuredClone(value))
    } else {
      setDraft(serializeValue(fieldPath, value, valueType))
    }
    setEditing(true)
  }
  const cancelEdit = () => { setEditing(false) }

  const commitEdit = async () => {
    setSaving(true)
    try {
      const result = isArrayEdit ? draftArr : parseValue(fieldPath, draft, valueType)
      await onSave(fieldPath, result)
      setEditing(false)
    } catch { } finally { setSaving(false) }
  }

  // ---- 编辑模式 ----
  if (editing) {
    return (
      <div>
        <div className="flex items-center justify-between">
          <h3 className="label">{label}</h3>
          <div className="flex gap-1">
            <button onClick={commitEdit} disabled={saving}
              className="p-1.5 bg-primary-500 text-white rounded-lg hover:bg-primary-600 disabled:opacity-50" title="保存">
              <Save className="w-3.5 h-3.5" />
            </button>
            <button onClick={cancelEdit}
              className="p-1.5 bg-slate-100 text-slate-500 rounded-lg hover:bg-slate-200" title="取消">
              <X className="w-3.5 h-3.5" />
            </button>
          </div>
        </div>
        <div className="mt-1">
          {isArrayEdit ? (
            <ArrayEditor
              columns={getColumns(fieldPath)}
              items={draftArr}
              onChange={setDraftArr}
            />
          ) : (
            <textarea
              value={draft}
              onChange={e => setDraft(e.target.value)}
              className="input-field text-sm w-full resize-none"
              rows={Math.max(3, Math.min(12, draft.split('\n').length))}
              placeholder={placeholder || getPlaceholder(fieldPath, valueType)}
              autoFocus
            />
          )}
        </div>
      </div>
    )
  }

  // ---- 展示模式 ----
  return (
    <div className="group">
      <div className="flex items-center justify-between mb-1">
        <h3 className="label">{label}</h3>
        {!disabled && (
          <button onClick={startEdit}
            className="flex items-center gap-1 px-2 py-0.5 text-[11px] text-slate-400 border border-slate-200 rounded-md
                       hover:text-primary-600 hover:border-primary-300 hover:bg-primary-50 transition-colors">
            <Edit3 className="w-3 h-3" />
            编辑
          </button>
        )}
      </div>
      {render(value)}
    </div>
  )
}

// ---- ArrayEditor 列配置 ----

function getColumns(fieldPath: string): any[] {
  const base = fieldPath.split('.').pop() || fieldPath
  if (base === 'steps' || fieldPath.includes('steps')) {
    return [
      { key: 'order', label: '序号', type: 'number', flex: 0.4 },
      { key: 'action', label: '操作步骤', placeholder: '用户输入账号密码', flex: 1.5 },
      { key: 'expect', label: '预期结果', placeholder: '系统校验身份', flex: 1.5 },
    ]
  }
  if (base === 'alternate_flows' || fieldPath.includes('alternate_flows')) {
    return [
      { key: 'name', label: '名称', placeholder: '登录失败', flex: 1 },
      { key: 'trigger', label: '触发条件', placeholder: '密码错误', flex: 1.2 },
      { key: 'expect', label: '预期结果', placeholder: '提示重试', flex: 1.2 },
    ]
  }
  if (base === 'boundary_conditions') {
    return [
      { key: 'field', label: '字段', placeholder: '用户名', flex: 0.8 },
      { key: 'condition', label: '条件', placeholder: '为空', flex: 1 },
      { key: 'expect', label: '预期', placeholder: '提示必填', flex: 1.2 },
    ]
  }
  if (base === 'business_rules') {
    return [
      { key: 'id', label: 'ID', placeholder: 'BR-001', flex: 0.5 },
      { key: 'type', label: '类型', type: 'select', options: BR_TYPE_OPTIONS, flex: 0.6 },
      { key: 'rule', label: '规则描述', placeholder: '密码长度不少于8位', flex: 2 },
    ]
  }
  if (base === 'data_constraints') {
    return [
      { key: 'field', label: '字段', placeholder: 'username', flex: 0.8 },
      { key: 'type', label: '类型', type: 'select', options: FIELD_TYPE_OPTIONS, flex: 0.7 },
      { key: 'required', label: '必填', type: 'checkbox', flex: 0.35 },
      { key: 'constraints', label: '约束', placeholder: '2-20字符', flex: 1.2 },
    ]
  }
  if (base === 'acceptance_criteria') {
    return [
      { key: 'id', label: 'ID', placeholder: 'AC-001', flex: 0.5 },
      { key: 'type', label: '类型', type: 'select', options: AC_TYPE_OPTIONS, flex: 0.6 },
      { key: 'description', label: '验收描述', placeholder: '用户可正常登录系统', flex: 2 },
    ]
  }
  // v2.22: 功能点子 SRR 新增字段
  if (base === 'test_points_hint') {
    return [
      { key: 'category', label: '类别', type: 'select', options: TEST_POINT_CATEGORY_OPTIONS, flex: 0.7 },
      { key: 'point', label: '测试点', placeholder: '正确账号+正确密码→登录成功', flex: 2.5 },
    ]
  }
  if (base === 'ui_rules') {
    return [
      { key: 'element', label: '元素', placeholder: '密码输入框', flex: 0.8 },
      { key: 'behavior', label: '行为', placeholder: '默认脱敏显示', flex: 1.5 },
      { key: 'condition', label: '条件', placeholder: '始终', flex: 1 },
    ]
  }
  if (base === 'data_model') {
    return [
      { key: 'entity_name', label: '实体名', placeholder: 'User', flex: 0.8 },
      { key: 'description', label: '描述', placeholder: '用户实体', flex: 1.2 },
      { key: 'fields', label: '字段', placeholder: 'id, name, email', flex: 1.5 },
      { key: 'relationships', label: '关系', placeholder: 'has_many: Order', flex: 1.2 },
    ]
  }
  if (base === 'assumptions') {
    return [
      { key: 'content', label: '内容', placeholder: '假设用户账号体系已存在', flex: 2.5 },
      { key: 'type', label: '类型', type: 'select', options: ASSUMPTION_TYPE_OPTIONS, flex: 0.8 },
    ]
  }
  return []  // fallback
}

// ---- 默认占位符 ----

function getPlaceholder(fieldPath: string, valueType: string): string {
  if (valueType === 'string') return '输入文本...'
  if (valueType === 'string[]') return '一行一条'
  const base = fieldPath.split('.').pop() || fieldPath
  switch (base) {
    case 'steps': return '操作描述 → 预期结果\n用户输入账号 → 校验格式\n校验通过 → 跳转首页'
    case 'alternate_flows': return '名称|触发条件 → 预期结果\n登录失败|密码错误 → 提示重试'
    case 'boundary_conditions': return '字段|条件 → 预期\n用户名|为空 → 提示必填'
    case 'business_rules': return 'ID|类型|规则\nBR-001|校验|密码长度≥8位'
    case 'data_constraints': return '字段|类型|必填|约束\nusername|string|必填|2-20字符'
    case 'acceptance_criteria': return 'ID|类型|描述\nAC-001|功能|用户可正常登录'
    case 'test_points_hint': return '类别|测试点\nfunctional|正确账号+正确密码→登录成功\nboundary|密码长度边界（6位/5位）'
    case 'ui_rules': return '元素|行为|条件\n密码输入框|默认脱敏显示|始终'
    case 'data_model': return '实体名|描述|字段|关系\nUser|用户实体|id,name,email|has_many: Order'
    case 'assumptions': return '内容|类型\n假设用户账号体系已存在|assumption'
    default: return '每行一条记录，用 | 和 → 分隔字段'
  }
}

// ---- 序列化/反序列化工具 ----

/** 序列化：将 JS 值转为可编辑的文本（非 JSON，用户友好）。 */
function serializeValue(fieldPath: string, val: unknown, type: string): string {
  if (val == null) return ''
  switch (type) {
    case 'string':
      return String(val)
    case 'string[]':
      return (Array.isArray(val) ? val : []).join('\n')
    case 'object[]':
      if (!Array.isArray(val) || val.length === 0) return ''
      return serializeObjectArray(fieldPath, val as any[])
    case 'json':
      return JSON.stringify(val, null, 2)
    default:
      return JSON.stringify(val, null, 2)
  }
}

/** 反序列化：将编辑文本还原为 JS 值。 */
function parseValue(fieldPath: string, text: string, type: string): unknown {
  switch (type) {
    case 'string':
      return text
    case 'string[]':
      return text.split('\n').map(s => s.trim()).filter(Boolean)
    case 'object[]':
      return text.trim() ? parseObjectArray(fieldPath, text) : []
    case 'json': {
      const parsed = JSON.parse(text)
      return parsed
    }
    default:
      return text
  }
}

// ---- object[] 模板格式序列化/反序列化 ----

/** 根据字段路径选择模板格式，将对象数组转为一行一条的可读文本。 */
function serializeObjectArray(fieldPath: string, items: any[]): string {
  const base = fieldPath.split('.').pop() || fieldPath

  // steps: order → action → expect
  if (base === 'steps' || fieldPath.includes('steps')) {
    return items.map((s: any) =>
      `${s.order ? s.order + '. ' : ''}${s.action || ''} → ${s.expect || ''}`
    ).join('\n')
  }
  // alternate_flows: name|trigger → expect
  if (base === 'alternate_flows' || fieldPath.includes('alternate_flows')) {
    return items.map((f: any) =>
      `${f.name || ''}|${f.trigger || ''} → ${f.expect || ''}`
    ).join('\n')
  }
  // boundary_conditions: field|condition → expect
  if (base === 'boundary_conditions') {
    return items.map((b: any) =>
      `${b.field || ''}|${b.condition || ''} → ${b.expect || ''}`
    ).join('\n')
  }
  // business_rules: id|type|rule
  if (base === 'business_rules') {
    return items.map((r: any) =>
      typeof r === 'string' ? r
        : `${r.id || ''}|${r.type || ''}|${r.rule || ''}`
    ).join('\n')
  }
  // data_constraints: field|type|必填/可选|constraints
  if (base === 'data_constraints') {
    return items.map((d: any) =>
      `${d.field || ''}|${d.type || ''}|${d.required ? '必填' : '可选'}|${d.constraints || ''}`
    ).join('\n')
  }
  // acceptance_criteria: id|type|description
  if (base === 'acceptance_criteria') {
    return items.map((a: any) =>
      typeof a === 'string' ? a
        : `${a.id || ''}|${a.type || ''}|${a.description || ''}`
    ).join('\n')
  }
  // v2.22: test_points_hint: category|point
  if (base === 'test_points_hint') {
    return items.map((t: any) =>
      `${t.category || ''}|${t.point || ''}`
    ).join('\n')
  }
  // v2.22: ui_rules: element|behavior|condition
  if (base === 'ui_rules') {
    return items.map((u: any) =>
      `${u.element || ''}|${u.behavior || ''}|${u.condition || ''}`
    ).join('\n')
  }
  // v2.22: data_model: entity_name|description|fields|relationships
  if (base === 'data_model') {
    return items.map((d: any) =>
      `${d.entity_name || ''}|${d.description || ''}|${d.fields || (Array.isArray(d.fields) ? d.fields.join(',') : '')}|${d.relationships || (Array.isArray(d.relationships) ? d.relationships.join(',') : '')}`
    ).join('\n')
  }
  // v2.22: assumptions: content|type
  if (base === 'assumptions') {
    return items.map((a: any) =>
      `${a.content || ''}|${a.type || ''}`
    ).join('\n')
  }
  // fallback: JSON for unknown object arrays
  return JSON.stringify(items, null, 2)
}

/** 解析模板文本为对象数组。 */
function parseObjectArray(fieldPath: string, text: string): any[] {
  const base = fieldPath.split('.').pop() || fieldPath
  const lines = text.split('\n').map(s => s.trim()).filter(Boolean)

  // steps: "1. 操作 → 预期" or "操作 → 预期"
  if (base === 'steps' || fieldPath.includes('steps')) {
    return lines.map((line, i) => {
      const parts = line.split('→').map(s => s.trim())
      const actionPart = parts[0] || ''
      // Extract order if present (e.g. "1. 用户操作")
      const orderMatch = actionPart.match(/^(\d+)\.?\s*(.*)/)
      const action = orderMatch ? orderMatch[2] : actionPart
      const order = orderMatch ? parseInt(orderMatch[1]) : i + 1
      return { order, action, expect: parts[1] || '' }
    })
  }
  // alternate_flows: name|trigger → expect
  if (base === 'alternate_flows' || fieldPath.includes('alternate_flows')) {
    return lines.map((line, i) => {
      const arrowParts = line.split('→').map(s => s.trim())
      const leftParts = (arrowParts[0] || '').split('|').map(s => s.trim())
      return { name: leftParts[0] || '', trigger: leftParts[1] || '', expect: arrowParts[1] || '' }
    })
  }
  // boundary_conditions: field|condition → expect
  if (base === 'boundary_conditions') {
    return lines.map((line) => {
      const arrowParts = line.split('→').map(s => s.trim())
      const leftParts = (arrowParts[0] || '').split('|').map(s => s.trim())
      return { field: leftParts[0] || '', condition: leftParts[1] || '', expect: arrowParts[1] || '' }
    })
  }
  // business_rules: id|type|rule
  if (base === 'business_rules') {
    return lines.map((line) => {
      const parts = line.split('|').map(s => s.trim())
      return { id: parts[0] || '', type: parts[1] || '', rule: parts[2] || parts[0] }
    })
  }
  // data_constraints: field|type|必填/可选|constraints
  if (base === 'data_constraints') {
    return lines.map((line) => {
      const parts = line.split('|').map(s => s.trim())
      return { field: parts[0] || '', type: parts[1] || '', required: parts[2] === '必填', constraints: parts[3] || '' }
    })
  }
  // acceptance_criteria: id|type|description
  if (base === 'acceptance_criteria') {
    return lines.map((line) => {
      const parts = line.split('|').map(s => s.trim())
      return { id: parts[0] || '', type: parts[1] || '', description: parts[2] || parts[0] }
    })
  }
  // v2.22: test_points_hint: category|point
  if (base === 'test_points_hint') {
    return lines.map((line) => {
      const parts = line.split('|').map(s => s.trim())
      return { category: parts[0] || '', point: parts[1] || '' }
    })
  }
  // v2.22: ui_rules: element|behavior|condition
  if (base === 'ui_rules') {
    return lines.map((line) => {
      const parts = line.split('|').map(s => s.trim())
      return { element: parts[0] || '', behavior: parts[1] || '', condition: parts[2] || '' }
    })
  }
  // v2.22: data_model: entity_name|description|fields|relationships
  if (base === 'data_model') {
    return lines.map((line) => {
      const parts = line.split('|').map(s => s.trim())
      return {
        entity_name: parts[0] || '',
        description: parts[1] || '',
        fields: parts[2] || '',
        relationships: parts[3] || '',
      }
    })
  }
  // v2.22: assumptions: content|type
  if (base === 'assumptions') {
    return lines.map((line) => {
      const parts = line.split('|').map(s => s.trim())
      return { content: parts[0] || '', type: parts[1] || 'assumption' }
    })
  }
  // fallback: try JSON
  try { return JSON.parse(text) } catch { return [] }
}
