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
  results: KbQueryResult[]
  error?: string | null
  timestamp: string
}

export interface KbQueryResult {
  id: string
  title?: string
  name?: string
  summary?: string
  type?: string
  category?: string
  similarity: number
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
 * v2.35: 移除 affected_requirements（不再分析受影响的需求） */
export interface FPImpactDescription {
  fp_id: string
  fp_name: string
  fp_type?: string
  module_id?: string
  req_id?: string
  affected_fps: {
    fp_id: string
    fp_name: string
    relation_type: string
    relation_cn?: string
    req_id?: string
    req_title?: string
    depth: number
    description?: string
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
