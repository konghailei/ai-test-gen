/* API 数据类型定义 */

export interface Requirement {
  id: string
  title: string
  product_id: string
  priority: string
  status: string
  source_type?: string
  modules?: string[]
  created_at: string
  go_live_date?: string
}

export interface RequirementDetail extends Requirement {
  module: string
  modules: string[]
  version: number
  srr_json: Record<string, unknown>
  raw_text: string
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
  category?: 'completeness' | 'ambiguity' | 'gap' | 'conflict' | 'cold_start'
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
  extra_notes?: string
  auto_augmentations?: KbAugmentation[]
  field_conflicts?: FieldConflict[]
  quality_report?: { overall_score: number; summary?: string }
  previous_title?: string
  new_title?: string
}

export interface TestCase {
  id: string
  req_id: string
  title: string
  type: string
  priority: string
  status: string
  confidence: number
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
  few_shot_context?: Record<string, unknown>
  kb_flowback?: Record<string, unknown>
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
}

// ============================================================================
// v1.4 功能点（FunctionalPoint）
// ============================================================================
export interface FPBusinessPoint {
  name: string
  summary: string
  suggested_module_id?: string
}

export interface FPItem {
  id: string
  name: string
  fp_type: string
  module_id: string | null
  priority: string
  srr_json: Record<string, any>
  atomicity_check: Record<string, boolean>
  test_hints: string[]
  vectorization_status: string | null
  status: string  // pending_review / approved / archived
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
}

export interface FPNameSuggestion {
  id: string
  name: string
  req_id: string
}

/** v3.0: FP 影响功能说明（存于 SRR impact_analysis 数组） */
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
  affected_requirements: {
    id: string
    title: string
    matched_modules?: string[]
  }[]
  description: string
  test_focus: string
  generated_at: string
  is_auto_generated: boolean
}
