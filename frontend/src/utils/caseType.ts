/**
 * 用例类型分类体系（v3.5.11，docs/04 类型体系定稿，大哥拍板 2026-10-15）。
 *
 * 后端 app/core/case_type_taxonomy.py 的 TS 镜像（同构同序）：
 * 细分 type（LLM 契约 / DB 存量，永不变）→ 展示分组。
 * 禁止各处再写映射表（历史上 5 处各自为政已漂移）。
 *
 * 分组设计：
 * - 功能测试组 functional：functional + boundary（边界值）+ exception（异常）
 *   + exploratory（探索性）——经典归属 ⊂ 功能测试；细分保留供"技术覆盖矩阵"度量。
 * - 场景测试组 scenario：integration 展示更名（内部值不变）——跨功能点业务场景。
 * - 安全 / 性能 / 回归：独立分组。
 * - UI / 接口 / 数据 / 其他：v2 遗留细分，归入同名分组。
 */

/** 细分 type → 分组 key */
export const TYPE_GROUP: Record<string, string> = {
  functional: 'functional',
  boundary: 'functional',
  exception: 'functional',
  exploratory: 'functional',
  integration: 'scenario',
  security: 'security',
  performance: 'performance',
  regression: 'regression',
  ui: 'ui',
  api: 'api',
  data: 'data',
}

/** 分组中文名（展示口径） */
export const GROUP_LABEL: Record<string, string> = {
  functional: '功能测试',
  scenario: '场景测试',
  security: '安全测试',
  performance: '性能测试',
  regression: '回归测试',
  ui: '界面测试',
  api: '接口测试',
  data: '数据测试',
  other: '其他',
}

/** 细分中文名（复合标签的细分部分用） */
export const DETAIL_LABEL: Record<string, string> = {
  functional: '功能',
  boundary: '边界值',
  exception: '异常',
  exploratory: '探索性',
  integration: '跨功能点场景',
  security: '安全',
  performance: '性能',
  regression: '回归',
  ui: 'UI',
  api: '接口',
  data: '数据',
  other: '其他',
}

/** 分组展示顺序 */
export const GROUP_ORDER = [
  'functional', 'scenario', 'security', 'performance',
  'regression', 'ui', 'api', 'data', 'other',
]

/** 每组代表值：type 等于代表值时复合标签直接显示组名 */
const GROUP_REPRESENTATIVE: Record<string, string> = {
  functional: 'functional',
  scenario: 'integration',
  security: 'security',
  performance: 'performance',
  regression: 'regression',
  ui: 'ui',
  api: 'api',
  data: 'data',
  other: 'other',
}

/** 细分 type → 分组 key；未知值归 other */
export function caseTypeGroup(t?: string | null): string {
  const key = (t || '').trim().toLowerCase()
  return TYPE_GROUP[key] || 'other'
}

/** 细分 type → 分组中文名（如 boundary → '功能测试'） */
export function caseTypeGroupLabel(t?: string | null): string {
  return GROUP_LABEL[caseTypeGroup(t)] || '其他'
}

/** 细分 type → 细分中文名（如 boundary → '边界值'） */
export function caseTypeDetailLabel(t?: string | null): string {
  const key = (t || '').trim().toLowerCase()
  return DETAIL_LABEL[key] || key || '其他'
}

/** 复合标签：组内多细分的显示'功能测试·边界值'；代表值直接显示组名。
 *  空值（脏数据缺 type）与代表值同形态：只显示组名，不缀'其他'。 */
export function caseTypeCompositeLabel(t?: string | null): string {
  const g = caseTypeGroup(t)
  const groupLabel = GROUP_LABEL[g] || '其他'
  const key = (t || '').trim().toLowerCase()
  if (!key || key === GROUP_REPRESENTATIVE[g]) return groupLabel
  return `${groupLabel}·${caseTypeDetailLabel(t)}`
}

/** 分组 → 代表细分 type（分组节点下新建用例时的兜底 type） */
export function caseTypeGroupRepresentative(g: string): string {
  return GROUP_REPRESENTATIVE[g] || 'other'
}

/** 分组 → 组内细分 type 列表（按展示顺序） */
export function caseTypeGroupMembers(g: string): string[] {
  return Object.keys(TYPE_GROUP).filter((t) => TYPE_GROUP[t] === g)
}

/** 分组排序键（GROUP_ORDER 下标；未知分组排最后） */
export function caseTypeGroupSortKey(g: string): number {
  const i = GROUP_ORDER.indexOf(g)
  return i === -1 ? GROUP_ORDER.length : i
}
