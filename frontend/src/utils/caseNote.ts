/**
 * 用例备注（XMind 节点 note）的序列化与解析。
 *
 * 用途：XMind 编辑页里用例节点的备注可以直接编辑，保存时按固定格式把备注
 * 反解回用例字段（优先级/类型/描述/前置条件/测试步骤），实现「改备注 = 改用例」。
 *
 * 标准格式（buildCaseNote 的输出，也是 parseCaseNote 唯一认可的输入）：
 *
 *   【优先级】P1
 *   【类型】功能
 *   【描述】验证标准登录主流程
 *   【前置条件】
 *   1. 已部署测试环境
 *   2. 用户已注册
 *   【测试步骤】
 *   步骤1：输入账号密码
 *     预期：登录成功
 *   步骤2：点击退出
 *     预期：返回登录页
 *
 * 容错约定：
 *   - 冒号支持中英文（：/:）
 *   - 预期行可缩进，必须紧跟在某条「步骤N」之后
 *   - 【前置条件】与【测试步骤】块可省略；整段为空视为未修改
 */

import type { TestCase } from '../types'
import { DETAIL_LABEL } from './caseType'

/**
 * 用例类型 → 中文标签（XMind 备注用；单一来源 caseType.ts 的 DETAIL_LABEL，
 * 与后端 taxonomy 对齐）。备注空间取细分短标签，归属组由前端徽章/导出呈现。
 */
export const CASE_TYPE_LABELS: Record<string, string> = {
  ...DETAIL_LABEL,
  integration: '集成', // 备注历史形态兼容（taxonomy 展示名为"跨功能点场景"）
}

/** 中文标签 → 用例类型（反查，用于解析备注） */
export const CASE_LABEL_TYPES: Record<string, string> = Object.fromEntries(
  Object.entries(CASE_TYPE_LABELS).map(([k, v]) => [v, k])
)

export const PRIORITIES = ['P0', 'P1', 'P2', 'P3']

export function typeLabel(t: string): string {
  return CASE_TYPE_LABELS[t] || t || '其他'
}

export function labelType(l: string): string {
  return CASE_LABEL_TYPES[l] || l || 'other'
}

/** 备注支持的字段（顺序即标准模板顺序） */
export const NOTE_FIELDS = ['优先级', '类型', '描述', '前置条件', '测试步骤'] as const

export interface ParsedNote {
  priority?: string
  type?: string
  description?: string
  preconditions?: string[]
  steps?: Array<{ step: number; action: string; expected: string }>
}

export interface NoteError {
  line: number
  message: string
}

export interface ParseResult {
  ok: boolean
  data: ParsedNote
  errors: NoteError[]
}

// ---------------------------------------------------------------------------
// 序列化
// ---------------------------------------------------------------------------

/** 用例 → 备注文本（标准格式） */
export function buildCaseNote(c: TestCase): string {
  const lines: string[] = []
  lines.push(`【优先级】${c.priority || 'P1'}`)
  lines.push(`【类型】${typeLabel(c.type)}`)
  if (c.description?.trim()) lines.push(`【描述】${c.description.trim()}`)
  if (c.preconditions?.length) {
    lines.push('【前置条件】')
    c.preconditions.forEach((p, i) => lines.push(`${i + 1}. ${p}`))
  }
  lines.push('【测试步骤】')
  if (c.steps?.length) {
    c.steps.forEach(s => {
      lines.push(`步骤${s.step}: ${s.action}`)
      if (s.expected) lines.push(`  预期: ${s.expected}`)
    })
  }
  return lines.join('\n')
}

/** 空用例的备注模板（新建用例节点时引导用户填写） */
export function emptyNoteTemplate(): string {
  return [
    '【优先级】P1',
    '【类型】功能',
    '【描述】',
    '【前置条件】',
    '1. ',
    '【测试步骤】',
    '步骤1: ',
    '  预期: ',
  ].join('\n')
}

// ---------------------------------------------------------------------------
// 解析
// ---------------------------------------------------------------------------

const FIELD_RE = /^【([^】]+)】\s*(.*)$/
const PRE_RE = /^(\d+)\s*[.、)]\s*(.*)$/
const STEP_RE = /^步骤\s*(\d+)\s*[：:]\s*(.*)$/
const EXPECT_RE = /^预期\s*[：:]\s*(.*)$/

/**
 * 备注文本 → 用例字段。
 * 任何格式问题都会收集到 errors（带行号），ok=false 时不应写回用例。
 */
export function parseCaseNote(text: string): ParseResult {
  const errors: NoteError[] = []
  const data: ParsedNote = {}
  const lines = text.split(/\r?\n/)

  let section: string | null = null        // 当前所在块：前置条件 / 测试步骤
  const seen = new Map<string, number>()   // 字段 → 首次出现的行号
  let preconditions: string[] = []
  let steps: Array<{ step: number; action: string; expected: string }> = []
  let preDeclared = false
  let stepDeclared = false

  lines.forEach((raw, idx) => {
    const lineNo = idx + 1
    const line = raw.trim()
    if (!line) return

    const fm = line.match(FIELD_RE)
    if (fm) {
      const field = fm[1].trim()
      const inline = fm[2].trim()
      if (!(NOTE_FIELDS as readonly string[]).includes(field)) {
        errors.push({
          line: lineNo,
          message: `未知字段「${field}」，可用字段：${NOTE_FIELDS.join(' / ')}`,
        })
        return
      }
      if (seen.has(field)) {
        errors.push({
          line: lineNo,
          message: `字段「${field}」重复定义（第 ${seen.get(field)} 行已出现过）`,
        })
        return
      }
      seen.set(field, lineNo)

      if (field === '优先级') {
        const v = inline
        if (!v) {
          errors.push({ line: lineNo, message: '优先级不能为空，应为 P0 / P1 / P2 / P3' })
        } else if (!PRIORITIES.includes(v)) {
          errors.push({
            line: lineNo,
            message: `优先级「${v}」无效，只能是 ${PRIORITIES.join(' / ')}`,
          })
        } else {
          data.priority = v
        }
        section = null
        return
      }

      if (field === '类型') {
        const v = inline
        if (!v) {
          errors.push({ line: lineNo, message: '类型不能为空' })
        } else if (!CASE_LABEL_TYPES[v]) {
          errors.push({
            line: lineNo,
            message: `未知用例类型「${v}」，可用：${Object.values(CASE_TYPE_LABELS).join(' / ')}`,
          })
        } else {
          data.type = CASE_LABEL_TYPES[v]
        }
        section = null
        return
      }

      if (field === '描述') {
        if (inline) data.description = inline
        section = '描述'
        return
      }

      if (field === '前置条件') {
        preDeclared = true
        if (inline) preconditions.push(inline)
        section = '前置条件'
        return
      }

      // 测试步骤
      stepDeclared = true
      section = '测试步骤'
      return
    }

    // 块外内容
    if (!section) {
      errors.push({
        line: lineNo,
        message: `内容必须写在字段下方（如【描述】或【测试步骤】），当前行：${truncate(line)}`,
      })
      return
    }

    if (section === '描述') {
      data.description = (data.description ? data.description + '\n' : '') + line
      return
    }

    if (section === '前置条件') {
      const pm = line.match(PRE_RE)
      if (!pm) {
        errors.push({
          line: lineNo,
          message: `前置条件应以「1. 内容」开头，当前行：${truncate(line)}`,
        })
        return
      }
      const content = pm[2].trim()
      if (!content) {
        errors.push({ line: lineNo, message: `第 ${pm[1]} 条前置条件内容为空` })
        return
      }
      preconditions.push(content)
      return
    }

    // 测试步骤块
    const sm = line.match(STEP_RE)
    if (sm) {
      const no = Number(sm[1])
      const action = sm[2].trim()
      if (!action) {
        errors.push({ line: lineNo, message: `步骤${no} 的操作内容不能为空` })
        return
      }
      if (no !== steps.length + 1) {
        errors.push({
          line: lineNo,
          message: `步骤编号不连续：应为「步骤${steps.length + 1}」，当前是「步骤${no}」`,
        })
        return
      }
      steps.push({ step: no, action, expected: '' })
      return
    }

    const em = line.match(EXPECT_RE)
    if (em) {
      if (steps.length === 0) {
        errors.push({
          line: lineNo,
          message: `「预期」必须写在某条「步骤N」的下方，不能单独出现`,
        })
        return
      }
      const expected = em[1].trim()
      if (!expected) {
        errors.push({ line: lineNo, message: `步骤${steps.length} 的预期结果为空` })
        return
      }
      if (steps[steps.length - 1].expected) {
        steps[steps.length - 1].expected += '；' + expected
      } else {
        steps[steps.length - 1].expected = expected
      }
      return
    }

    errors.push({
      line: lineNo,
      message: `无法识别的行，应为「步骤N：操作」或「预期：结果」，当前行：${truncate(line)}`,
    })
  })

  // 声明了块却没内容
  if (preDeclared && preconditions.length === 0) {
    const at = seen.get('前置条件') || 0
    errors.push({ line: at, message: '【前置条件】下方没有任何条件，请删除该字段或补充「1. 内容」' })
  }
  if (stepDeclared && steps.length === 0) {
    const at = seen.get('测试步骤') || 0
    errors.push({ line: at, message: '【测试步骤】下方没有任何步骤，请补充「步骤1：操作」或删除该字段' })
  }

  if (preDeclared && preconditions.length) data.preconditions = preconditions
  if (stepDeclared && steps.length) data.steps = steps

  return { ok: errors.length === 0, data, errors }
}

function truncate(s: string, n = 20): string {
  return s.length > n ? s.slice(0, n) + '…' : s
}

/** 错误列表 → 可直接展示的文本（每行带行号） */
export function formatNoteErrors(errors: NoteError[]): string[] {
  return errors.map(e => `第 ${e.line} 行：${e.message}`)
}
