/**
 * API 客户端 — 封装所有后端 API 调用，自动附带认证令牌
 */
import type {
  ApiResponse, Requirement, RequirementDetail, TestCase, TestPattern,
  KnowledgeStats, ClarifyQuestion, PaginatedData, LLMConfig, HealthStatus, KbFlowback, KbEntry, KbQAAskResponse, KbQAReference, KbQARecord,
  KbAugmentation, FieldConflict, ClarifyHistoryEntry, KbDiagnostics, KbModelStatus, AnalysisTaskInfo,
  FPBusinessPoint, FPItem, FPDependencyItem, FPNameSuggestion,
} from '../types'

const BASE = '/api'

/** 获取当前认证令牌（从 localStorage） */
function getToken(): string | null {
  return localStorage.getItem('aitestgen_token')
}

/** 退出登录：清除 token 并跳转到登录页 */
function forceLogout(): void {
  localStorage.removeItem('aitestgen_token')
  localStorage.removeItem('aitestgen_user')
  // 不在登录页才跳转，避免循环
  if (!window.location.pathname.startsWith('/login')) {
    window.location.href = '/login'
  }
}

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {}
  // DELETE 请求不需要 Content-Type
  if (!options?.method || options.method !== 'DELETE') {
    headers['Content-Type'] = 'application/json'
  }
  const token = getToken()
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }
  const res = await fetch(`${BASE}${url}`, {
    headers: { ...headers, ...options?.headers as Record<string, string> },
    ...options,
  })
  // Token 过期或未授权 → 强制退出
  if (res.status === 401) {
    forceLogout()
    throw new Error('登录已过期，请重新登录')
  }
  const json = await res.json()
  // FastAPI HTTPException 返回格式：{"detail": "..."}
  if (!res.ok && json.detail) {
    throw new Error(json.detail)
  }
  // 标准 ApiResponse 格式：{"success": false, "message": "..."}
  if (json.success === false) {
    throw new Error(json.message || '请求失败')
  }
  return json.data as T
}

async function uploadFile<T = ApiResponse>(url: string, file: File, extra: Record<string, string>, timeoutMs = 300000): Promise<T> {
  const form = new FormData()
  form.append('file', file)
  Object.entries(extra).forEach(([k, v]) => form.append(k, v))
  const headers: Record<string, string> = {}
  const token = getToken()
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }
  
  try {
    const res = await fetch(`${BASE}${url}`, { 
      method: 'POST', 
      body: form, 
      headers,
    })
    if (res.status === 401) { forceLogout(); throw new Error('登录已过期，请重新登录') }
    const json = await res.json()
    // FastAPI 错误格式：{detail: "..."}，统一转为 {success: false, message: detail}
    if (!res.ok && json.detail) {
      throw new Error(json.detail)
    }
    // 标准格式：{success: false, message: "..."}
    if (json.success === false) {
      throw new Error(json.message || '请求失败')
    }
    return json as T
  } catch (err: any) {
    throw err
  }
}

// ============================================================
// 健康检查
// ============================================================
export const health = () => request<HealthStatus>('/health')

// ============================================================
// 需求管理
// ============================================================
export const requirements = {
  list: (page = 1, pageSize = 20, productId?: string, projectId?: string, filters?: { status?: string; priority?: string; search?: string }) => {
    const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) })
    if (productId) params.set('product_id', productId)
    if (projectId) params.set('project_id', projectId)
    if (filters?.status) params.set('status', filters.status)
    if (filters?.priority) params.set('priority', filters.priority)
    if (filters?.search) params.set('search', filters.search)
    return request<PaginatedData<Requirement>>(`/requirements?${params}`)
  },
  get: (id: string) => request<RequirementDetail>(`/requirements/${id}`),
  update: (id: string, srrJson: Record<string, unknown>, rawText?: string) =>
    request(`/requirements/${id}`, {
      method: 'PUT',
      body: JSON.stringify({ srr_json: srrJson, raw_text: rawText }),
    }),
  upload: (raw: string, meta?: Record<string, string>) =>
    request<{ req_id: string; srr: Record<string, unknown> }>('/requirements/upload', {
      method: 'POST',
      body: JSON.stringify({ raw_text: raw, ...meta }),
    }),
    uploadFile: (file: File, productId = 'PROD-DEFAULT', title = '') =>
        uploadFile('/requirements/upload/file', file, { product_id: productId, title }),
    uploadMultiFiles: (files: File[], productId = 'PROD-DEFAULT', title = '', projectId = '', modules = '') => {
        const form = new FormData()
        files.forEach(f => form.append('files', f))
        form.append('product_id', productId)
        if (title) form.append('title', title)
        if (projectId) form.append('project_id', projectId)
        if (modules) form.append('modules', modules)
        const headers: Record<string, string> = {}
        const token = localStorage.getItem('aitestgen_token')
        if (token) headers['Authorization'] = `Bearer ${token}`
        
        
        return fetch(`${BASE}/requirements/upload/multi`, {
            method: 'POST',
            body: form,
            headers,
        }).then(async res => {
            if (res.status === 401) { forceLogout(); throw new Error('登录已过期，请重新登录') }
            const json = await res.json()
            if (!res.ok && json.detail) throw new Error(json.detail)
            if (json.success === false) throw new Error(json.message || '请求失败')
            return json
        })
    },
  /** 影响分析：基于模块依赖图谱分析需求变更的影响范围 */
  impactAnalysis: (id: string) =>
    request<{
      req_id: string
      req_title: string
      direct_modules: { id: string; name: string }[]
      indirect_modules: { id: string; name: string; reason: string; depth: number }[]
      affected_requirements: { id: string; title: string; priority: string; modules: string[] }[]
    }>(`/requirements/${id}/impact`),

  // 后端返回 { req_id, status, questions[], quality_report, kb_status, auto_augmentations }
  // 此处解包出 questions 数组，避免调用方误把整个对象当数组使用
  clarifyQuestions: async (id: string): Promise<ClarifyQuestion[]> => {
    const res = await request<{ questions: ClarifyQuestion[] }>(
      `/requirements/${id}/clarify/questions`
    )
    return Array.isArray(res?.questions) ? res.questions : []
  },
  /** v2.3: 获取完整澄清状态（含历史问题 + 前次答案 + KB 补全 + 字段冲突） */
  getClarifyState: async (id: string) => {
    return request<{
      questions: ClarifyQuestion[]
      previous_answers: Record<string, string>
      status: string
      quality_report: { overall_score: number; completeness_score?: number; ambiguity_score?: number; summary?: string }
      kb_status: { kb_empty: boolean; product_first_requirement: boolean }
      auto_augmentations: KbAugmentation[]
      field_conflicts: FieldConflict[]
    }>(`/requirements/${id}/clarify/questions`)
  },
  submitClarify: (id: string, answers: Record<string, string>, extraNotes = '') =>
    request(`/requirements/${id}/clarify`, {
      method: 'POST',
      body: JSON.stringify({ answers, extra_notes: extraNotes }),
    }),
  delete: (id: string) =>
    request<{ deleted: boolean; status: string; message: string }>(`/requirements/${id}`, {
      method: 'DELETE',
    }),
  deletePreview: (id: string) =>
    request<{
      title: string; status: string;
      dependencies: {
        test_cases: number; generation_batches: number; req_dependencies: number;
        analysis_tasks: number; traceability: number; functional_points: number;
        superseded_refs: number;
      }
    }>(`/requirements/${id}/delete-preview`),
  archive: (id: string, goLiveDate?: string) =>
    request<{ req_id: string; status: string; kb_task_queued?: boolean }>(`/requirements/${id}/archive`, {
      method: 'POST',
      body: JSON.stringify({ go_live_date: goLiveDate || null }),
    }),
  /** 修改已归档需求的上线时间 */
  updateGoLiveDate: (id: string, goLiveDate: string) =>
    request<{ req_id: string; go_live_date: string; milvus_updated: boolean }>(`/requirements/${id}/go-live-date`, {
      method: 'PATCH',
      body: JSON.stringify({ go_live_date: goLiveDate }),
    }),
  /** v2.7: KB 已解答 → 确认生成最终 SRR */
  finalize: (id: string) =>
    request<{ req_id: string; status: string }>(`/requirements/${id}/finalize`, {
      method: 'POST',
    }),
  /** 获取澄清与重新生成历史 */
  getClarifyHistory: (id: string) =>
    request<{ items: ClarifyHistoryEntry[]; total: number }>(`/requirements/${id}/clarify/history`),
  /** 补充信息后重新生成需求分析（status=active 也可调用） */
  regenerate: (id: string, extraNotes: string) =>
    request<{ req_id: string; srr: Record<string, unknown>; status: string; previous_title: string; new_title: string }>(
      `/requirements/${id}/regenerate`,
      { method: 'POST', body: JSON.stringify({ extra_notes: extraNotes }) },
    ),
  /** 运行影响分析（基于需求关联模块的依赖图谱 BFS 遍历） */
  analyzeImpact: (reqId: string) =>
    request<{
      req_id: string
      req_title: string
      direct_modules: { id: string; name: string }[]
      indirect_modules: { id: string; name: string; reason: string; depth: number }[]
      affected_requirements: { id: string; title: string; priority: string; modules: string[] }[]
    }>(`/requirements/${reqId}/impact`),

  /** v2.14: 查询分析任务列表（任务管理） */
  listTasks: (params?: { status?: string; task_type?: string; product_id?: string; limit?: number; offset?: number }) => {
    const query = new URLSearchParams()
    if (params?.status) query.append('status', params.status)
    if (params?.task_type) query.append('task_type', params.task_type)
    if (params?.product_id) query.append('product_id', params.product_id)
    if (params?.limit) query.append('limit', String(params.limit))
    if (params?.offset) query.append('offset', String(params.offset))
    const qs = query.toString()
    return request<{ items: AnalysisTaskInfo[]; total: number }>(`/requirements/tasks${qs ? '?' + qs : ''}`)
  },
  /** v2.14: 查询单个任务状态（轮询进度） */
  getTask: (taskId: string) =>
    request<AnalysisTaskInfo>(`/requirements/tasks/${taskId}`),
  /** v2.14: 按需求 ID 查询关联的最新任务 */
  getTaskByReq: (reqId: string) =>
    request<AnalysisTaskInfo | null>(`/requirements/${reqId}/task`),
  /** v1.4: 取消分析任务 */
  cancelTask: (taskId: string) =>
    request<AnalysisTaskInfo>(`/requirements/tasks/${taskId}/cancel`, { method: 'POST' }),
  /** v1.4 功能点 */
  fpExtractBP: (reqId: string) =>
    request<{ business_points: FPBusinessPoint[] }>(`/requirements/${reqId}/fp/extract-bp`, { method: 'POST' }),
  fpSplit: (reqId: string, bpNames: string[]) =>
    request<{ functional_points: { id: string; name: string; fp_type: string; module_id: string | null }[]; total: number }>(
      `/requirements/${reqId}/fp/split`, { method: 'POST', body: JSON.stringify({ req_id: reqId, bp_names: bpNames }) }
    ),
  fpList: (reqId: string) =>
    request<{ items: FPItem[]; total: number }>(`/requirements/${reqId}/fp`),
  fpDependencies: (reqId: string) =>
    request<{ dependencies: FPDependencyItem[] }>(`/requirements/${reqId}/fp/dependencies`),
  fpAddDependency: (reqId: string, sourceFpId: string, targetFpId: string, relationType: string, description = '') =>
    request<{ id: number }>(`/requirements/${reqId}/fp/dependencies`, {
      method: 'POST',
      body: JSON.stringify({ source_fp_id: sourceFpId, target_fp_id: targetFpId, relation_type: relationType, description }),
    }),
  fpRemoveDependency: (reqId: string, depId: number) =>
    request(`/requirements/${reqId}/fp/dependencies/${depId}`, { method: 'DELETE' }),
  fpUpdate: (reqId: string, fpId: string, updates: { name?: string; module_id?: string; fp_type?: string }) =>
    request<{ success: boolean }>(`/requirements/${reqId}/fp/${fpId}`, {
      method: 'PATCH', body: JSON.stringify(updates),
    }),
  fpSearchNames: (reqId: string, q: string, productId?: string) => {
    const p = new URLSearchParams({ q })
    if (productId) p.append('product_id', productId)
    return request<{ names: FPNameSuggestion[] }>(`/requirements/${reqId}/fp/search-names?${p}`)
  },
  fpDelete: (reqId: string, fpId: string) =>
    request<{ success: boolean }>(`/requirements/${reqId}/fp/${fpId}`, { method: 'DELETE' }),
  fpApprove: (reqId: string, fpId: string) =>
    request<{ message: string }>(`/requirements/${reqId}/fp/${fpId}/approve`, { method: 'PATCH' }),
  fpRegenerate: (reqId: string, fpId: string, hint?: string) =>
    request<{ name: string; fp_type: string; srr_json: any; test_hints: any[] }>(
      `/requirements/${reqId}/fp/${fpId}/regenerate`,
      { method: 'PATCH', body: JSON.stringify({ hint: hint || '' }) },
    ),
  fpMerge: (reqId: string, fpIds: string[], mergedName?: string) =>
    request<{ id: string; name: string; fp_type: string }>(
      `/requirements/${reqId}/fp/merge`,
      { method: 'POST', body: JSON.stringify({ fp_ids: fpIds, merged_name: mergedName || '' }) },
    ),
}

// ============================================================
// 用例生成（SSE）
// ============================================================
export function generateCases(
  reqId: string,
  onEvent: (event: Record<string, unknown>) => void,
  onError?: (err: string) => void,
  onDone?: () => void,
): AbortController {
  const controller = new AbortController()

  fetch(`${BASE}/generate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ req_id: reqId }),
    signal: controller.signal,
  }).then(async (res) => {
    const reader = res.body?.getReader()
    if (!reader) return
    const decoder = new TextDecoder()
    let buffer = ''

    while (true) {
      const { done, value } = await reader.read()
      if (done) { onDone?.(); break }

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const line of lines) {
        if (line.startsWith('data: ')) {
          try {
            const event = JSON.parse(line.slice(6))
            onEvent(event)
          } catch { /* ignore parse errors */ }
        }
      }
    }
  }).catch((err) => {
    if (err.name !== 'AbortError') onError?.(err.message)
  })

  return controller
}

// ============================================================
// 用例管理
// ============================================================
export const testCases = {
  list: (reqId?: string, page = 1, pageSize = 20, productId?: string) => {
    const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) })
    if (reqId) params.set('req_id', reqId)
    if (productId) params.set('product_id', productId)
    return request<PaginatedData<TestCase>>(`/testcases?${params}`)
  },
  get: (id: string) => request<TestCase>(`/testcases/${id}`),
  update: (id: string, data: Record<string, unknown>) =>
    request(`/testcases/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  delete: (id: string) => request(`/testcases/${id}`, { method: 'DELETE' }),
  feedback: (id: string, accepted: boolean, comments = '') =>
    request(`/testcases/${id}/feedback`, {
      method: 'POST',
      body: JSON.stringify({ accepted, comments }),
    }),
}

// ============================================================
// 知识库
// ============================================================
export const knowledge = {
  stats: (productId?: string) => {
    const params = productId ? `?product_id=${productId}` : ''
    return request<KnowledgeStats>(`/knowledge/stats${params}`)
  },
  patterns: (category?: string, page = 1, pageSize = 20) => {
    const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) })
    if (category) params.set('category', category)
    return request<PaginatedData<TestPattern>>(`/knowledge/patterns?${params}`)
  },
  listEntries: (productId?: string, type?: string, filters?: { search?: string; vectorized?: string; modules?: string[] }) => {
    const params = new URLSearchParams()
    if (productId) params.set('product_id', productId)
    if (type) params.set('type', type)
    if (filters?.search) params.set('search', filters.search)
    if (filters?.vectorized) params.set('vectorized', filters.vectorized)
    if (filters?.modules?.length) params.set('modules', filters.modules.join(','))
    return request<{ items: KbEntry[]; total: number }>(`/knowledge/entries?${params}`)
  },
  deleteEntry: (entryId: string, entryType: string) =>
    request<{ entry_id: string; deleted: boolean; db_deleted?: boolean; kb_deleted?: boolean; kb_error?: string }>(`/knowledge/entries/${entryId}?entry_type=${entryType}`, {
      method: 'DELETE',
    }),
  diagnostics: () => request<KbDiagnostics>('/knowledge/diagnostics'),
  modelStatus: () => request<KbModelStatus>('/knowledge/model-status'),
  revectorize: (entryId: string) =>
    request<{ req_id: string; queued: boolean; message?: string }>(`/knowledge/entries/${entryId}/revectorize`, { method: 'POST' }),
  revectorizeMissing: () =>
    request<{ total_archived: number; missing: number; queued: number; failed: number; message?: string }>('/knowledge/revectorize-missing', { method: 'POST' }),
  /** 获取知识库专属 LLM 配置 */
  getKbLlm: () => request<{ provider: string; providers: Array<{ provider: string; provider_label: string; model: string; is_primary: boolean }> }>('/knowledge/kb-llm'),
  /** 设置知识库专属 LLM 供应商 */
  setKbLlm: (provider: string) =>
    request<{ provider: string }>('/knowledge/kb-llm', {
      method: 'PUT',
      body: JSON.stringify({ provider }),
    }),
}

// ============================================================
// 知识问答
// ============================================================
export const knowledgeQA = {
  /** 流式问答（SSE），返回 ReadableStream */
  askStream: (question: string, onChunk: (text: string) => void, onSources: (sources: KbQAReference[]) => void, onDone: (data: { id: string; answer_full: string; reference_sources: KbQAReference[] }) => void, onError: (err: string) => void) => {
    const controller = new AbortController()
    const token = localStorage.getItem('aitestgen_token')
    fetch('/api/knowledge-qa/ask', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': token ? `Bearer ${token}` : '',
      },
      body: JSON.stringify({ question }),
      signal: controller.signal,
    }).then(async res => {
      if (res.status === 401) { forceLogout(); return }
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }))
        onError(err.detail || `请求失败 (${res.status})`)
        return
      }
      if (!res.body) { onError('浏览器不支持流式读取'); return }

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''

        for (const line of lines) {
          if (line.startsWith('event:') || !line.trim()) {
            // SSE event type line, handled below
            continue
          }
          if (!line.startsWith('data:')) continue
          const data = line.slice(5).trim()
          if (!data) continue

          // Find event type from previous lines
          const prevLines = lines.slice(0, lines.indexOf(line))
          const eventLine = prevLines.reverse().find(l => l.startsWith('event:'))
          const eventType = eventLine ? eventLine.slice(6).trim() : 'chunk'

          if (eventType === 'chunk') {
            onChunk(data)
          } else if (eventType === 'sources') {
            try { onSources(JSON.parse(data)) } catch {}
          } else if (eventType === 'done') {
            try { onDone(JSON.parse(data)) } catch {}
          }
        }
      }
    }).catch(e => {
      if (e.name !== 'AbortError') onError(e.message || '网络错误')
    })

    return controller
  },

  ask: (question: string, productId?: string) =>
    request<KbQAAskResponse>('/knowledge-qa/ask', {
      method: 'POST',
      body: JSON.stringify({ question, product_id: productId || undefined }),
    }),
  chat: (recordId: string, question: string) =>
    request<{ record_id: string; question: string; answer: string; conversation_history: Array<{ role: string; content: string }> }>('/knowledge-qa/chat', {
      method: 'POST',
      body: JSON.stringify({ record_id: recordId, question }),
    }),
  correct: (id: string, correctedAnswer: string, correctionNotes = '') =>
    request<KbQARecord>('/knowledge-qa/correct', {
      method: 'POST',
      body: JSON.stringify({ id, corrected_answer: correctedAnswer, correction_notes: correctionNotes }),
    }),
  archive: (id: string, archiveType: 'requirement' | 'test_case', title: string, content: string) =>
    request<{ archived_id: string; archived_type: string; title: string }>('/knowledge-qa/archive', {
      method: 'POST',
      body: JSON.stringify({ id, archive_type: archiveType, title, content }),
    }),
  history: (page = 1, pageSize = 20) =>
    request<PaginatedData<KbQARecord>>(`/knowledge-qa/history?page=${page}&page_size=${pageSize}`),
  detail: (id: string) =>
    request<KbQARecord>(`/knowledge-qa/history/${id}`),
  delete: (id: string) =>
    request<{ deleted: boolean; record_id: string }>(`/knowledge-qa/history/${id}`, { method: 'DELETE' }),
}

// ============================================================
// 系统设置（多供应商）
// ============================================================
export const settings = {
  get: () => request<LLMConfig>('/settings/llm'),
  update: (providers: Array<{
    provider: string; provider_label?: string; api_key: string;
    base_url?: string; model?: string; is_primary?: boolean
  }>) =>
    request<LLMConfig>('/settings/llm', {
      method: 'PUT',
      body: JSON.stringify({ providers }),
    }),
  test: (providerName?: string) =>
    request<{ success: boolean; latency_ms: number; provider: string; model: string; message: string }>(
      `/settings/llm/test${providerName ? `?provider_name=${providerName}` : ''}`,
      { method: 'POST' }
    ),
}

// ============================================================
// 认证
// ============================================================
export const auth = {
  changePassword: (currentPassword: string, newPassword: string) =>
    request<{ message: string }>('/auth/change-password', {
      method: 'POST',
      body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
    }),
}

// ============================================================
// 导出
// ============================================================
export const exportCases = {
  download: async (format: string, caseIds?: string[]) => {
    const params = new URLSearchParams({ format })
    const url = `${BASE}/export?${params}`
    if (caseIds?.length) {
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ format, case_ids: caseIds }),
      })
      return res.blob()
    }
    const res = await fetch(url)
    return res.blob()
  },
}

// ============================================================
// 产品/项目/模块
// ============================================================
export const products = {
  list: () => request<import('../types').Product[]>('/products/products'),
  create: (name: string, desc = '') =>
    request('/products/products', { method: 'POST', body: JSON.stringify({ name, description: desc }) }),
  delete: (id: string) =>
    request(`/products/products/${id}`, { method: 'DELETE' }),
  listProjects: (pid: string) =>
    request<import('../types').Project[]>(`/products/products/${pid}/projects`),
  createProject: (pid: string, name: string, desc = '', version = '1.0') =>
    request(`/products/products/${pid}/projects`, {
      method: 'POST', body: JSON.stringify({ name, description: desc, version }),
    }),
  deleteProject: (id: string) =>
    request(`/products/projects/${id}`, { method: 'DELETE' }),
  listModules: (pid: string) =>
    request<import('../types').Module[]>(`/products/products/${pid}/modules`),
  createModule: (pid: string, name: string, desc = '') =>
    request(`/products/products/${pid}/modules`, {
      method: 'POST', body: JSON.stringify({ name, description: desc }),
    }),
  deleteModule: (id: string) =>
    request(`/products/modules/${id}`, { method: 'DELETE' }),
  updateModule: (id: string, name: string, description = '') =>
    request(`/products/modules/${id}`, {
      method: 'PUT',
      body: JSON.stringify({ name, description }),
    }),
}

// ============================================================
// 提示词管理
// ============================================================
export const promptApi = {
  list: () => request<{ prompts: import('../types').PromptItem[] }>('/admin/prompts'),
  update: (module: string, name: string, value: string) =>
    request(`/admin/prompts/${module}/${name}`, {
      method: 'PUT', body: JSON.stringify({ value }),
    }),
  reset: (module: string, name: string) =>
    request(`/admin/prompts/${module}/${name}/reset`, { method: 'POST' }),
}

// ============================================================
// 依赖图谱
// ============================================================
export const dependencyGraph = {
  get: (productId?: string) => {
    const params = productId ? `?product_id=${productId}` : ''
    return request<import('../types').DependencyGraph>(`/products/dependency-graph${params}`)
  },
  addEdge: (source: string, target: string, relationType = 'depends_on', strength = 'weak') =>
    request('/products/dependency-graph/edges', {
      method: 'POST',
      body: JSON.stringify({ source, target, relation_type: relationType, strength }),
    }),
  removeEdge: (source: string, target: string) =>
    request(`/products/dependency-graph/edges?source=${encodeURIComponent(source)}&target=${encodeURIComponent(target)}`, {
      method: 'DELETE',
    }),
  updateNodePosition: (moduleId: string, x: number, y: number) =>
    request(`/products/modules/${moduleId}/position`, {
      method: 'PUT',
      body: JSON.stringify({ x, y }),
    }),
  // 项目影响分析
  analyzeProject: (projectId: string) =>
    request<import('../types').ProjectImpact>(`/products/projects/${projectId}/impact`),
  // 功能点依赖图谱（跨需求）
  fpGraph: (productId: string) =>
    request<{ nodes: any[]; edges: any[] }>(`/products/${productId}/fp/dependency-graph`),
  fpImpact: (productId: string, fpId: string) =>
    request<{
      source_fp_name: string; affected: any[]; affected_modules: any[];
      total: number; impact_summary: string;
    }>(`/products/${productId}/fp/impact?fp_id=${fpId}`),
}

// ============================================================
// 通用 HTTP 方法（v2.12 新增：用户/角色管理）
// ============================================================

export async function apiGet<T = any>(url: string): Promise<T> {
  return request<T>(url, { method: 'GET' })
}

export async function apiPost<T = any>(url: string, body?: any): Promise<T> {
  return request<T>(url, { method: 'POST', body: body ? JSON.stringify(body) : undefined })
}

export async function apiPut<T = any>(url: string, body?: any): Promise<T> {
  return request<T>(url, { method: 'PUT', body: body ? JSON.stringify(body) : undefined })
}

export async function apiDelete<T = any>(url: string): Promise<T> {
  return request<T>(url, { method: 'DELETE' })
}
