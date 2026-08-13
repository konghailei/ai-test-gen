/* API 数据类型定义 */

export interface Requirement {
  id: string
  title: string
  product_id: string
  project_id?: string
  priority: string
  status: string
  source_type?: string
  modules?: string[]
  created_at: string
  go_live_date?: string
}

export interface RawImage {
  index: number
  data_url: string
  content_type: string
}

/** v2.33: 多人协作冲突检测——需求 revision 轮询响应 */
export interface RequirementRevision {
  revision: number
  updated_at: string
  status: string
  task_running: boolean
  task_type: string
  task_message: string
}

export interface RequirementDetail extends Requirement {
  module: string
  modules: string[]
  version: number
  srr_json: Record<string, unknown>
  raw_text: string
  raw_images?: RawImage[]  // v2.18: docx 原始图片（base64 data_url），供前端展示对比
  supplementary_text?: string  // v2.17: 补充文件内容（重新分析时上传的补充需求文件）
  source_type: string
  questions_count?: number
  kb_query_logs?: KbQueryLog[]
}

export interface KbQueryLog {
  type: string
  label: string
  query: string
  query_detail?: string
  answer?: string
  sources?: KbQueryResult[]
  answered?: boolean
  auto_filled?: boolean
  result_count: number
  results?: KbQueryResult[]
  error?: string | null
  timestamp: string
  duration_ms?: number
  status?: string
}

export interface KbQueryResult {
  id: string
  title?: string
  name?: string
  summary?: string
  type?: string
  category?: string
  similarity: number
  // v2.8.1: 后端 sources 实际写入的扩展字段
  req_id?: string
  field?: string
  value?: any
  document_full?: string
  go_live_date?: string
  project_version?: string
}

export interface ClarifyQuestion {
  id: string
  field: string
  question: string
  priority: 'critical' | 'important' | 'optional'
  category?: 'completeness' | 'ambiguity' | 'gap' | 'conflict' | 'cold_start' | 'impact_analysis'
  context: string
  default_answer?: string
  input_type?: 'text' | 'select' | 'multi_select'
  options?: string[]
  source?: string  // cold_start_xxx 等
  _llm_refine_missing?: boolean  // v2.8.6: LLM 未返回精炼结果标记
  // v2.8.66: 回答状态相关字段（后端getClarifyState接口会注入）
  answered?: boolean         // 是否已回答（用户有答案）
  status?: string            // 状态：answered / unresolved
  resolved?: boolean         // 是否已解决（已解决问题不在当前澄清页显示，仅历史可见）
  answer?: string            // 历史答案文本（如用户已提交过）
}

export interface KbAugmentation {
  field: string
  value: any
  similarity: number
  source_req_id?: string
  _is_baseline?: boolean
}

export interface FieldConflict {
  field: string
  existing_value: any
  existing_source: string
  suggested_value: any
  suggested_source: string
  kb_similarity: number
  kb_source_req_id?: string
  resolution: 'pending_human' | 'resolved'
}

export interface ClarifyState {
  questions: ClarifyQuestion[]
  previous_answers: Record<string, string>
  quality_report: {
    overall_score: number
    completeness_score?: number
    ambiguity_score?: number
    summary?: string
  }
  kb_status: { kb_empty: boolean; product_first_requirement: boolean }
  auto_augmentations: KbAugmentation[]
  field_conflicts?: FieldConflict[]
}

export interface ClarifyHistoryEntry {
  timestamp: string
  action: 'clarify' | 'regenerate'
  questions?: ClarifyQuestion[]
  answers?: Record<string, string>
  /** v2.29: 结构化 Q&A 记录（含 question 文本和 answer），优先于 questions+answers 展示 */
  qa_records?: Array<{
    id: string
    field?: string
    question?: string
    priority?: string
    answer?: string
  }>
  extra_notes?: string
  auto_augmentations?: KbAugmentation[]
  field_conflicts?: FieldConflict[]
  quality_report?: { overall_score: number; summary?: string }
  previous_title?: string
  new_title?: string
}

/** v2.0 需求历史版本摘要（列表） */
export interface ReqVersionSummary {
  version_index: number
  snapshot_at: string
  title: string
  trigger: string
  extra_notes: string
}

/** v2.0 需求历史版本详情（含完整 SRR 快照） */
export interface ReqVersionDetail {
  version_index: number
  snapshot_at: string
  title: string
  trigger: string
  extra_notes: string
  srr: Record<string, unknown>
}

export interface TestCase {
  id: string
  req_id: string
  req_title?: string
  project_name?: string
  product_id?: string
  title: string
  type: string
  priority: string
  status: string
  confidence: number
  functional_point: string
  description: string
  preconditions: string[]
  steps: { step: number; action: string; expected: string }[]
  created_at: string
  case_json?: Record<string, unknown>
}

export interface TestPattern {
  id: number
  name: string
  category: string
  pattern_json: Record<string, unknown>
  usage_count: number
  success_rate: number
}

export interface KnowledgeStats {
  total_requirements: number
  total_test_cases: number
  total_patterns: number
  total_batches: number
  kb_empty: boolean
}

export interface SSEEvent {
  stage: string
  message: string
  progress: number
  req_id?: string
  channel?: string
  cases?: TestCase[]
  review_report?: ReviewReport
  impact_result?: Record<string, unknown>
  gate_result?: Record<string, unknown>
  clarify_result?: Record<string, unknown>
  kb_flowback?: Record<string, unknown>
  // v2.0：一致性检查与 FP 覆盖率
  consistency_check?: {
    is_inconsistent?: boolean
    count_diff_pct?: number
    type_jaccard?: number
    semantic_similarity?: number
    reasons?: string[]
  }
  fp_coverage?: {
    total_fps?: number
    covered_fps?: number
    coverage_rate?: number
    uncovered?: Array<{ id: string; name?: string }>
  }
  duration_ms?: number
  [key: string]: unknown
}

export interface ReviewReport {
  total_cases: number
  passed_cases: number
  blocked_cases: number
  avg_confidence: number
  should_supplement: boolean
  issues?: ReviewIssue[]
  coverage_summary?: Record<string, number>
  supplement_hints?: string[]
}

export interface ReviewIssue {
  case_id: string
  dimension: string
  severity: string
  message: string
  suggestion: string
}

export interface ApiResponse<T = unknown> {
  success: boolean
  message: string
  data: T
}

export interface PaginatedData<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

export interface LLMProviderItem {
  id: number
  provider: string
  provider_label: string
  api_key: string
  base_url: string
  model: string
  is_primary: boolean
  /** v2.36: 思考模式开关（仅对支持思考的模型生效） */
  enable_thinking?: boolean
  /** v2.37: 自定义思考参数（JSON 字符串，为空时使用内置规则） */
  thinking_params?: string
}

export interface LLMConfig {
  providers: LLMProviderItem[]
  builtin_providers: Record<string, { label: string; base_url: string; model: string }>
}

export interface HealthStatus {
  version: string
  llm_provider: string
  llm_configured: boolean
  kb_empty: boolean
  cold_start_mode: boolean
  db_type?: string
  vector_store?: string
  vector_db?: {
    connected: boolean
    model_ready: boolean
    collections: Record<string, number>
    error: string
  }
}

export interface KbFlowback {
  auto_approved: number
  pending_review: number
  rejected: number
  errors: number
}

export interface Product {
  id: string
  name: string
  description: string
  visible_to_user_ids: number[]
  created_at: string
}

export interface Project {
  id: string
  product_id: string
  name: string
  description: string
  version: string
  // v2.45 新增 / v2.46 三状态机
  status?: string  // active / pending_archive / archived
  go_live_date?: string
  archived_at?: string
  archived_by?: string
  created_at: string
}

export interface Module {
  id: string
  project_id: string
  product_id?: string
  name: string
  description: string
  created_at: string
}

export interface GraphNode {
  id: string
  name: string
  product_id: string
  position?: { x: number; y: number }
}

export interface GraphEdge {
  source: string
  target: string
  relation_type: string
  strength: string
}

export interface DependencyGraph {
  nodes: GraphNode[]
  edges: GraphEdge[]
}

export interface ProjectImpact {
  project: { id: string; name: string; version: string }
  direct_modules: { id: string; name: string }[]
  indirect_modules: { module_id: string; name: string; relation_type: string; depth: number }[]
  direct_requirements: { id: string; title: string; modules: string[] }[]
  regression_requirements: { id: string; title: string; matched_modules: string[] }[]
}

export interface KbEntry {
  id: string
  title: string
  type: 'requirement' | 'test_case'
  product_id: string
  modules?: string
  priority?: string
  summary?: string
  archived_at?: string
  go_live_date?: string
  vectorized?: boolean
  vectorization_status?: string | null  // null=未尝试 / processing=向量化中 / completed=已完成 / failed=失败
  vectorization_error?: string | null
  created_at?: string
}

/** v2.14: 异步分析任务信息（任务管理 + 进度轮询） */
export interface PhaseTiming {
  start: string      // ISO 时间戳
  end?: string       // ISO 时间戳（阶段结束时填充）
  duration_ms?: number  // 阶段耗时（毫秒）
}

export interface AnalysisTaskInfo {
  id: string
  task_type: string  // upload_requirement / submit_clarify / regenerate / analyze_impact
  req_id: string | null
  req_title?: string
  product_id: string | null
  status: string  // pending / running / completed / failed / interrupted
  phase: string  // parse / kb_search / clarify / regenerate / impact / done
  progress: number  // 0-100
  message: string
  result_json: Record<string, unknown>
  error: string
  phase_timings?: Record<string, PhaseTiming>  // v2.23: 阶段耗时追踪
  server_now?: string  // v2.36: 服务器当前时间（UTC ISO），用于计算耗时避免客户端时钟偏差
  created_at: string
  updated_at: string
  completed_at: string | null
}

export interface KbDiagnostics {
  vector_db: {
    connected: boolean
    model_ready: boolean
    collections: Record<string, { num_entities: number; description?: string; error?: string }>
    error: string
  }
  architecture: {
    relational_db: string
    vector_db: string
    object_storage: string
    coordination: string
  }
  warnings?: string[]
}

export interface KbModelStatus {
  ready: boolean
  path: string
  device: string
  error: string
}

// ============================================================
// 知识问答
// ============================================================

export interface KbQAAskRequest {
  question: string
}

export interface KbQAAskResponse {
  id: string
  question: string
  answer: string
  reference_sources: KbQAReference[]
}

export interface KbQAReference {
  id: string
  title: string
  type: string
  similarity: number
  summary: string
}

export interface KbQARecord {
  id: string
  question: string
  answer: string
  corrected_answer: string | null
  correction_notes: string
  archived: boolean
  archived_type: string
  archived_id: string
  reference_sources: KbQAReference[]
  conversation_history?: Array<{ role: string; content: string }>
  created_at: string
  updated_at: string
  // v2.8.56: 审核流程字段
  correction_status?: 'none' | 'pending_review' | 'approved' | 'rejected'
  correction_module?: string
  correction_target_type?: 'requirement' | 'functional_point'
  correction_target_id?: string
  reviewed_by?: string
  reviewed_at?: string | null
  review_notes?: string
  product_id?: string
}

export interface KbQACorrectRequest {
  id: string
  corrected_answer: string
  correction_notes: string
}

export interface KbQAArchiveRequest {
  id: string
  archive_type: 'requirement' | 'test_case'
  title: string
  content: string
}

export interface PromptItem {
  id: string
  module: string
  name: string
  value: string
  default_value: string
  description: string
  updated_at: string
  updated_by?: string  // v2.3: 最后修改人
}

/** v2.3: 提示词修改历史记录 */
export interface PromptHistoryItem {
  id: number
  prompt_id: string
  module: string
  name: string
  old_value: string
  new_value: string
  changed_by: string
  changed_at: string
  change_summary: string
}

// ============================================================================
// v2.22: 日志系统（LLM 调用日志 + 用户操作日志）
// ============================================================================
export interface LLMCallLogItem {
  id: number
  agent_name: string
  task_id: string | null
  req_id: string | null
  provider: string
  model: string
  temperature: number
  duration_ms: number
  token_usage: Record<string, number> | null
  success: boolean
  error_message: string | null
  created_at: string
}

export interface LLMCallLogDetail extends LLMCallLogItem {
  system_prompt: string
  user_prompt: string
  response: string
  parsed_json: Record<string, unknown> | null
}

export interface OperationLogItem {
  id: number
  user_id: number
  username: string
  action: string
  target_type: string
  target_id: string
  detail: Record<string, unknown> | string | null
  ip_address: string | null
  success: boolean
  error_message: string | null
  created_at: string
}

// ============================================================================
// v1.4 功能点（FunctionalPoint）
// ============================================================================
export interface FPItem {
  id: string
  name: string
  fp_type: string
  module_id: string | null
  modules: string[]  // v2.2: 多模块关联
  priority: string
  srr_json: Record<string, any>
  atomicity_check: Record<string, boolean>
  test_hints: string[]
  vectorization_status: string | null
  status: string  // pending_review / approved / archived
  needs_module?: boolean  // v2.3: 是否未分配模块（用于前端提示）
  // v2.45: 依赖图谱节点详情字段（归档时从 srr_json 同步）
  summary?: string  // FP 实现内容概括描述
  relations_summary?: string  // 与其他 FP 关系概括描述
  created_at: string
}

export interface FPDependencyItem {
  id: number
  source_fp_id: string
  source_name: string
  target_fp_id: string
  target_name: string
  relation_type: string
  description: string
  review_status?: string  // v2.3: pending / approved / rejected
  reviewed_by?: string
  reviewed_at?: string
  created_by?: string
  created_at?: string
  // v2.45: 版本管理字段（项目归档时冻结为历史版本）
  is_active?: boolean  // 是否启用（归档时 False，恢复活动时 True）
  version_pk?: number | null  // 所属版本快照ID
}

/** v2.3: FP 依赖关系变动历史记录 */
export interface FPDependencyHistoryItem {
  id: number
  dependency_id: number | null
  source_fp_id: string
  source_name: string
  target_fp_id: string
  target_name: string
  relation_type: string
  description: string
  action: string  // created / approved / rejected / modified / deleted
  old_review_status: string
  new_review_status: string
  changed_by: string
  change_reason: string
  related_req_id: string | null
  created_at: string
}

/** v2.3: 可复用的已归档功能点 */
export interface FPReusableItem {
  id: string
  name: string
  fp_type: string
  priority: string
  module_id: string | null
  modules: string[]
  module_names: string[]
  source_req_id: string
  source_req_title: string
  test_hints: string[]
  archived_at: string
}

export interface FPNameSuggestion {
  id: string
  name: string
  req_id: string
}

/** v3.0: FP 影响功能说明（存于 SRR impact_analysis 数组）
 * v2.35: 移除 affected_requirements（不再分析受影响的需求）
 * v3.1: 新增 is_affected 和 confirmed_at（用户确认受影响后写入），
 *       以及 impact_description、source_req_id（来自 confirm_impact_analysis 端点） */
export interface FPImpactDescription {
  fp_id: string
  fp_name: string
  fp_type?: string
  module_id?: string
  req_id?: string
  source_req_id?: string
  is_affected?: boolean
  confirmed_at?: string
  impact_description?: string
  affected_fps: {
    fp_id: string
    fp_name: string
    relation_type: string
    relation_cn?: string
    req_id?: string
    req_title?: string
    depth: number
    description?: string
    direction?: 'incoming' | 'outgoing'  // v3.1: incoming=依赖此FP(下游)，outgoing=被依赖(上游)
  }[]
  affected_modules: {
    module_id: string
    module_name: string
    reason: string
    depth: number
  }[]
  description: string
  test_focus: string
  generated_at: string
  is_auto_generated: boolean
}

/** 需求级影响分析结果（GET /:req_id/impact 返回结构） */
export interface RequirementImpactResult {
  requirement: { id: string; title: string; modules: string[]; fp_count: number }
  module_impact: {
    direct: { id: string; name: string }[]
    indirect: { id: string; name: string; reason: string; depth: number }[]
  }
  affected_fps: {
    fp_id: string
    fp_name: string
    fp_status: string
    module_id: string
    module_name: string
    impact_reason: string
    depth: number
    source_fp_name: string
    kb_context: string
    existing_cases_count: number
  }[]
  total_affected: number
}

/** confirm_impact_analysis 接口请求 body
 *
 * 修复 H5（取消确认死锁）：
 * - confirmed_fp_ids：用户勾选的"受影响"FP（生成回归用例）
 * - unconfirmed_fp_ids：用户确认"不受影响"的 FP（不算回归，但要打 confirmed_at 让守卫放行）
 * - removed_fp_ids：从 impact_analysis 中完全移除的 FP（候选错误、已删除 FP 等）
 */
export interface ImpactConfirmPayload {
  confirmed_fp_ids?: string[]
  unconfirmed_fp_ids?: string[]
  removed_fp_ids?: string[]
  edited_descriptions?: Record<string, string>
}

/** confirm_impact_analysis 接口响应 data */
export interface ImpactConfirmResult {
  confirmed_count: number
  unconfirmed_count: number
  removed_count: number
  impact_analysis: FPImpactDescription[]
}

// v2.45 依赖版本管理
export interface DependencyVersion {
  id: number
  version_id: string
  project_id: string
  project_name: string
  project_version: string
  go_live_date: string
  snapshot_at: string
  is_active: boolean
  archived_by: string
  requirement_count: number
  fp_count: number
  dependency_count: number
  change_summary: string
}

export interface DependencyVersionItem {
  id: number
  source_fp_id: string
  source_fp_name: string
  target_fp_id: string
  target_fp_name: string
  relation_type: string
  description: string
  review_status: string
  source_req_id: string
  source_req_title: string
  // v2.45: target 侧需求字段，支持跨需求双向追溯
  target_req_id: string
  target_req_title: string
}

export interface DependencyVersionDetail extends DependencyVersion {
  items: DependencyVersionItem[]
}

export interface VersionDiff {
  version_1: { id: number; version_id: string; snapshot_at: string }
  version_2: { id: number; version_id: string; snapshot_at: string }
  added: Array<{ source_fp_name: string; target_fp_name: string; relation_type: string; description: string }>
  removed: Array<{ source_fp_name: string; target_fp_name: string; relation_type: string; description: string }>
  modified: Array<{ source_fp_name: string; target_fp_name: string; relation_type: string; old_description: string; new_description: string; old_review_status: string; new_review_status: string }>
  summary: { added_count: number; removed_count: number; modified_count: number; unchanged_count: number }
}

export interface KbProject {
  id: string
  name: string
  version: string
  description: string
  status: string
  go_live_date: string
  archived_at: string
  // v2.45: 归档操作人（与后端 list_kb_projects 返回字段对齐）
  archived_by: string
  requirement_count: number
  test_case_count: number
}
