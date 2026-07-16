/**
 * SRR / FP 字段类型标签中文映射。
 */

// 业务规则类型
const BR_TYPE_LABELS: Record<string, string> = {
  validation: '校验',
  calculation: '计算',
  workflow: '流程',
  permission: '权限',
  security: '安全',
  session: '会话',
  timeout: '超时',
  rate_limit: '限流',
  notification: '通知',
  data_retention: '数据保留',
  audit: '审计',
  encryption: '加密',
  integration: '集成',
  // LLM 常见变体
  business: '业务',
  constraint: '约束',
  data_integrity: '数据完整性',
  field_validation: '字段校验',
  access_control: '访问控制',
  format: '格式',
  duplication: '去重',
  mandatory: '必填',
  uniqueness: '唯一性',
  relationship: '关联',
  logic: '逻辑',
  prerequisite: '前置条件',
  authorization: '授权',
  authentication: '认证',
  presentation: '展示',
  initialization: '初始化',
  data_format: '数据格式',
  range: '范围',
  enumeration: '枚举',
}

// 验收标准类型
const AC_TYPE_LABELS: Record<string, string> = {
  functional: '功能',
  performance: '性能',
  security: '安全',
  usability: '易用性',
  compatibility: '兼容性',
  reliability: '可靠性',
}

// 功能点类型
const FP_TYPE_LABELS: Record<string, string> = {
  workflow: '业务流程',
  constraint: '约束规则',
  calculation: '计算公式',
  api_behavior: 'API 行为',
  data_record: '数据记录',
  config: '系统配置',
}

/** 获取业务规则类型的中文名，兜底返回原文 */
export function brTypeLabel(type: string): string {
  return BR_TYPE_LABELS[type] || type
}

/** 获取验收标准类型的中文名 */
export function acTypeLabel(type: string): string {
  return AC_TYPE_LABELS[type] || type
}

/** 获取功能点类型的中文名 */
export function fpTypeLabel(type: string): string {
  return FP_TYPE_LABELS[type] || type
}
