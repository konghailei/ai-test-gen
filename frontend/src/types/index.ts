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
  priority: string
  context: string
  default_answer?: string
  input_type?: 'text' | 'select' | 'multi_select'
  options?: string[]
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
  created_at: string
}

export interface Module {
  id: string
  project_id: string
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
