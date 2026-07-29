export function formatDate(dateStr: string): string {
  if (!dateStr) return '-'
  try {
    const d = new Date(dateStr)
    return d.toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
  } catch {
    return dateStr
  }
}

export function priorityBadge(p: string): string {
  const map: Record<string, string> = { P0: 'badge-p0', P1: 'badge-p1', P2: 'badge-p2', P3: 'badge-p3' }
  return map[p] || 'badge-p2'
}

export function statusBadge(s: string): string {
  const map: Record<string, string> = {
    active: 'badge-active', approved: 'badge-approved', reviewed: 'badge-reviewed',
    draft: 'badge-draft', rejected: 'badge-rejected',
    analyzing: 'badge-info',
  }
  return map[s] || 'badge-draft'
}

/** 需求状态中文映射（v2.1 新增，v1.6 增加 pending_review / confirmed） */
export const STATUS_LABELS: Record<string, string> = {
  active: '活动中',
  archived: '已归档',
  superseded: '已替代',
  deprecated: '已废弃',
  draft: '草稿',
  analyzing: '分析中',
  pending_review: '待确认',
  confirmed: '确认完成',
}

/** 用例状态中文映射 */
export const CASE_STATUS_LABELS: Record<string, string> = {
  draft: '未审核',
  reviewed: '已审核',
  approved: '已通过',
  rejected: '已驳回',
}

export function confidenceColor(c: number): string {
  if (c >= 0.85) return 'text-emerald-600'
  if (c >= 0.6) return 'text-amber-600'
  return 'text-slate-400'
}

export function cn(...classes: (string | false | undefined | null)[]): string {
  return classes.filter(Boolean).join(' ')
}

/** 标准化 source_type：后端可能存 'md' / 'markdown' / 'docx' / 'txt' / 'multi' */
export function normalizeSourceType(st: string | undefined | null): string {
  const s = (st || 'txt').toLowerCase()
  if (s === 'md' || s === 'markdown') return 'markdown'
  if (s === 'docx') return 'docx'
  if (s === 'multi') return 'multi'
  return 'txt'
}

/** 来源格式中文标签 */
export function sourceTypeLabel(st: string | undefined | null): string {
  const map: Record<string, string> = {
    markdown: 'Markdown',
    md: 'Markdown',
    docx: 'Word',
    multi: '多文件',
    txt: '文本',
  }
  return map[(st || 'txt').toLowerCase()] || st || '文本'
}
