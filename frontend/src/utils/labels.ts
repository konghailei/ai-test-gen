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

// 验收标准类型（v2.48: 字段已合并到业务规则，保留映射兼容历史数据）
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
  calculation: '计算规则',
  api_behavior: 'API 行为',
  data_record: '数据记录',
  config: '系统配置',
}

// 数据约束字段类型（v2.48: 兼容英文历史数据映射为中文）
const DATA_TYPE_LABELS: Record<string, string> = {
  string: '字符串',
  str: '字符串',
  text: '文本',
  integer: '整数',
  int: '整数',
  long: '长整数',
  bigint: '长整数',
  short: '短整数',
  float: '小数',
  double: '双精度小数',
  decimal: '高精度小数',
  number: '数字',
  boolean: '布尔',
  bool: '布尔',
  date: '日期',
  datetime: '日期时间',
  timestamp: '时间戳',
  time: '时间',
  enum: '枚举',
  array: '数组',
  list: '数组',
  object: '对象',
  dict: '对象',
  json: 'JSON',
  uuid: 'UUID',
  url: 'URL',
  email: '邮箱',
  phone: '电话',
  file: '文件',
  image: '图片',
  binary: '二进制',
  blob: '二进制大对象',
}

/** 获取业务规则类型的中文名，兜底返回原文 */
export function brTypeLabel(type: string): string {
  return BR_TYPE_LABELS[type] || type
}

/** 获取验收标准类型的中文名（v2.48 兼容历史数据） */
export function acTypeLabel(type: string): string {
  return AC_TYPE_LABELS[type] || type
}

/** 获取功能点类型的中文名 */
export function fpTypeLabel(type: string): string {
  return FP_TYPE_LABELS[type] || type
}

/** 获取数据约束字段类型的中文名（v2.48 新增，兼容英文历史数据） */
export function dataTypeLabel(type: string): string {
  if (!type) return ''
  // 若已是中文，直接返回
  if (/[\u4e00-\u9fa5]/.test(type)) return type
  return DATA_TYPE_LABELS[type.toLowerCase()] || type
}

// 测试用例类型
const CASE_TYPE_LABELS: Record<string, string> = {
  functional: '功能',
  boundary: '边界',
  exception: '异常',
  security: '安全',
  performance: '性能',
  regression: '回归',
  integration: '集成',
  ui: '界面',
  api: '接口',
  data: '数据',
}

/** 获取测试用例类型的中文名 */
export function caseTypeLabel(type: string): string {
  return CASE_TYPE_LABELS[type] || type
}
