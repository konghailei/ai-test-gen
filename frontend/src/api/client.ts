/**
 * API 客户端 — 封装所有后端 API 调用，自动附带认证令牌
 */
import type {
  ApiResponse, Requirement, RequirementDetail, TestCase, TestPattern,
  KnowledgeStats, ClarifyQuestion, PaginatedData, LLMConfig, HealthStatus, KbFlowback, KbEntry, KbQAAskResponse, KbQAReference, KbQARecord,
  KbAugmentation, FieldConflict, ClarifyHistoryEntry, KbDiagnostics, KbModelStatus, AnalysisTaskInfo,
  FPItem, FPDependencyItem, FPNameSuggestion,
  ReqVersionSummary, ReqVersionDetail,
  LLMCallLogItem, LLMCallLogDetail, OperationLogItem,
  RequirementRevision,
  DependencyVersion, DependencyVersionDetail, VersionDiff, KbProject, SystemResources,
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

/** v3.1.2: 请求级超时兜底 — 后端连接池僵死等极端场景下请求不会永远挂起 */
const DEFAULT_TIMEOUT_MS = 30000

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

  const doFetch = (): Promise<Response> => {
    // v3.1.2: 每次请求自带 30s 超时（与调用方传入的 signal 取并集）
    const timeoutSignal = AbortSignal.timeout(DEFAULT_TIMEOUT_MS)
    const signal = options?.signal
      ? AbortSignal.any([options.signal, timeoutSignal])
      : timeoutSignal
    return fetch(`${BASE}${url}`, {
      headers: { ...headers, ...options?.headers as Record<string, string> },
      ...options,
      signal,
    })
  }

  // v3.1.1 修复：GET 请求网络层瞬断（API 容器重启/Nginx 短暂不可达导致 Failed to fetch）
  // 自动重试 1 次，消除切换产品时偶发「加载项目列表失败」的硬报错。
  const isGet = !options?.method || options.method === 'GET'
  let res: Response
  try {
    res = await doFetch()
  } catch (err) {
    // 页面导航/组件卸载导致的主动取消（fetch 抛出 AbortError），并非真实错误。
    // 标记 _aborted 供调用方识别，避免污染 Console 或弹出错误提示。
    if ((err as any)?.name === 'AbortError') {
      throw Object.assign(err as Error, { _aborted: true })
    }
    // v3.1.2: 超时（TimeoutError）或网络瞬断 → GET 幂等请求自动重试一次
    if (isGet) {
      await new Promise(r => setTimeout(r, 400))
      try {
        res = await doFetch()
      } catch (err2) {
        if ((err2 as any)?.name === 'AbortError') {
          throw Object.assign(err2 as Error, { _aborted: true })
        }
        if ((err2 as any)?.name === 'TimeoutError') {
          (err2 as Error).message = `请求超时（${DEFAULT_TIMEOUT_MS / 1000}s），请稍后重试`
        }
        throw err2
      }
    } else {
      if ((err as any)?.name === 'TimeoutError') {
        (err as Error).message = `请求超时（${DEFAULT_TIMEOUT_MS / 1000}s），请稍后重试`
      }
      throw err
    }
  }
  // Token 过期或未授权 → 强制退出
  if (res.status === 401) {
    forceLogout()
    // v3.1.1: 标记 _unauthorized，调用方跳过「加载失败」类提示（页面即将跳转登录页）
    throw Object.assign(new Error('登录已过期，请重新登录'), { _unauthorized: true })
  }

  // v4.1.6: 429 限流 → 按 Retry-After 退避重试，不直接抛错。
  //
  // 为什么必须重试（现场症状："分析期间刷新页面非常慢"）：
  //   后端限流桶被前端轮询（Layout 5s / Tasks 3s / 上传弹窗 1.5s）打满后，
  //   用户的真实请求会拿到 429。旧实现把 429 当成普通失败直接抛，
  //   调用方大多是 `catch {}` 静默吞掉 → 表现为"数据不刷新/页面卡住"，
  //   用户只能反复刷新，越刷越卡（每次刷新又是一轮请求洪峰）。
  //
  // 为什么不重试写操作（POST/PUT/DELETE）：
  //   非幂等，重试可能造成重复提交（建两个任务、重复生成）。
  //   这类请求直接把 429 抛给调用方，由用户决定是否重来。
  if (res.status === 429 && isGet) {
    const retryAfter = Number(res.headers.get('Retry-After')) || 1
    // 最多 2 次，且总等待不超过 3 秒：轮询是背景任务，
    // 为它长时间阻塞没有意义（下一次定时器本来就会再发）。
    for (let attempt = 0; attempt < 2; attempt++) {
      const waitMs = Math.min(3000, Math.max(200, retryAfter * 1000))
      await new Promise(r => setTimeout(r, waitMs))
      try {
        res = await doFetch()
      } catch (e) {
        if ((e as any)?.name === 'AbortError') {
          throw Object.assign(e as Error, { _aborted: true })
        }
        break
      }
      if (res.status !== 429) break
    }
    if (res.status === 429) {
      // 广播给关心限流状态的组件（如 Tasks 页暂停高频轮询）。
      // 用 window 事件而不是引入全局状态库：只有少数页面需要，不值得为此加依赖。
      try {
        window.dispatchEvent(new CustomEvent('aitestgen:rate-limited'))
      } catch { /* SSR / 测试环境无 window，忽略 */ }
      throw Object.assign(
        new Error('请求过于频繁，请稍后再试'),
        { _rateLimited: true },
      )
    }
  }

  // v3.1.2: 502/504 等网关错误返回 HTML 页时 res.json() 会抛 SyntaxError，收敛为友好错误
  let json: any
  try {
    json = await res.json()
  } catch {
    throw new Error(`服务暂不可用（HTTP ${res.status}），请稍后重试`)
  }
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
  update: (id: string, srrJson: Record<string, unknown>, rawText?: string, expectedRevision?: number) =>
    request(`/requirements/${id}`, {
      method: 'PUT',
      // v3.4（docs/15 §4.8）：expected_revision 乐观锁——两人并发编辑，后提交者得 409
      body: JSON.stringify({ srr_json: srrJson, ...(rawText != null ? { raw_text: rawText } : {}),
        ...(expectedRevision != null ? { expected_revision: expectedRevision } : {}) }),
    }),
  upload: (raw: string, meta?: Record<string, string>) =>
    request<{ task_id: string; req_id: string; status: string }>('/requirements/upload', {
      method: 'POST',
      body: JSON.stringify({ raw_text: raw, ...meta }),
    }),
    uploadMultiFiles: (files: File[], productId = 'PROD-DEFAULT', title = '', projectId = '', modules = '', executabilityClarify = true) => {
        const form = new FormData()
        files.forEach(f => form.append('files', f))
        form.append('product_id', productId)
        if (title) form.append('title', title)
        if (projectId) form.append('project_id', projectId)
        if (modules) form.append('modules', modules)
        // v2.49：需求级可执行性追问开关（上传页勾选，默认开）
        form.append('executability_clarify', String(executabilityClarify))
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
  /** 影响分析：基于模块依赖图谱 + FP 依赖图谱分析需求变更的影响范围 */
  impactAnalysis: async (id: string) => {
    // 修复 H9：统一走 POST 新端点（v3.1 RequirementImpactResult），避免双结构漂移；
    // 再把新结构映射回前端 state 字段（与 handleReRunImpact 使用的字段一致）
    type _OldShape = {
      req_id: string
      req_title: string
      direct_modules: { id: string; name: string }[]
      indirect_modules: { id: string; name: string; reason: string; depth: number }[]
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
    const r = await request<import('../types').RequirementImpactResult>(`/requirements/${id}/impact-analysis`, { method: 'POST' })
    return {
      req_id: r.requirement?.id || id,
      req_title: r.requirement?.title || '',
      direct_modules: (r.module_impact?.direct || []).map((m: any) => ({
        id: m.id || m.module_id || '',
        name: m.name || m.module_name || m.module_id || '',
      })),
      indirect_modules: (r.module_impact?.indirect || []).map((m: any) => ({
        id: m.id || m.module_id || '',
        name: m.name || m.module_name || m.module_id || '',
        reason: m.reason || '',
        depth: typeof m.depth === 'number' ? m.depth : 1,
      })),
      affected_fps: (r.affected_fps || []).map((fp: any) => ({
        fp_id: fp.fp_id || fp.id || '',
        fp_name: fp.fp_name || fp.name || '',
        fp_status: fp.fp_status || fp.review_status || fp.status || '',
        module_id: fp.module_id || '',
        module_name: fp.module_name || '',
        impact_reason: fp.impact_reason || fp.impact_description || fp.description || '',
        depth: typeof fp.depth === 'number' ? fp.depth : 1,
        source_fp_name: fp.source_fp_name || '',
        kb_context: fp.kb_context || '',
        existing_cases_count: Number(fp.existing_cases_count || fp.case_count || 0),
      })),
      total_affected: Number(r.total_affected || 0),
    } as _OldShape
  },

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
  /**
   * v3.3（docs/14 二期 A5）：需求质量报表。
   *
   * 结构分由后端**按需实时计算**（纯字典检查、无 LLM），
   * 因此存量需求也能立即拿到报表，无需等重新解析。
   */
  getQualityReport: (id: string) =>
    request<{
      req_id: string
      title: string
      status: string
      structure_source: 'computed' | 'stored' | 'none'
      structure: {
        score: number
        summary?: string
        fp_count?: number
        filled_weight?: number
        applicable_weight?: number
        filled: { key: string; label: string; weight: number }[]
        missing: { key: string; label: string; weight: number; hint?: string }[]
        not_applicable: { key: string; label: string; weight: number; reason: string }[]
        report_only: { key: string; label: string; hit: boolean }[]
      }
      quality: {
        overall_score: number | null
        completeness_score: number | null
        ambiguity_score: number | null
        ambiguity_count: number | null
        score_source: string | null
        summary: string | null
        breakdown_available: boolean
      }
      clarify: {
        total_rounds: number
        force_passed: boolean
        force_passed_reason: string
        current_status: string
        rounds: { round: number; questions_count: number; answers_count: number; created_at?: string; note?: string }[]
      }
    }>(`/requirements/${id}/quality-report`),
  submitClarify: (id: string, answers: Record<string, string>, extraNotes = '') =>
    request<{ task_id: string; req_id: string; status: string; answers_count: number }>(`/requirements/${id}/clarify`, {
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
  /** v2.0 需求历史版本管理：列出所有历史版本（重新分析前的 SRR 快照摘要） */
  listVersions: (id: string) =>
    request<{
      items: ReqVersionSummary[];
      total: number;
      current_title: string;
      current_status: string;
      current_updated_at: string;
    }>(`/requirements/${id}/versions`),
  /** v2.0 查看指定历史版本的完整 SRR 快照 */
  getVersion: (id: string, versionIndex: number) =>
    request<ReqVersionDetail>(`/requirements/${id}/versions/${versionIndex}`),
  /** v2.2 切换使用指定历史版本（用快照替换当前 SRR） */
  restoreVersion: (id: string, versionIndex: number) =>
    request<{ req_id: string; version_index: number; status: string }>(
      `/requirements/${id}/versions/${versionIndex}/restore`,
      { method: 'POST' },
    ),
  /** v2.0 需求 SRR → XMind 思维导图导出（直接触发浏览器下载 .xmind 文件）
   *  v2.20: 支持 format 参数选择 XMind 2020+ JSON 或 XMind 8 XML 格式
   */
  exportXmind: async (id: string, format: 'xmind' | 'xmind8' = 'xmind'): Promise<{ filename: string; size: number }> => {
    const resp = await fetch(`${BASE}/requirements/${id}/export/xmind?format=${format}`, {
      headers: { Authorization: `Bearer ${getToken()}` },
    })
    if (!resp.ok) {
      const msg = await resp.text().catch(() => resp.statusText)
      throw new Error(`导出失败 (${resp.status}): ${msg.slice(0, 200)}`)
    }
    // 从 Content-Disposition 解析文件名
    const cd = resp.headers.get('Content-Disposition') || ''
    const m = cd.match(/filename="?([^";]+)"?/i)
    const filename = m ? m[1] : `requirement_${id}.xmind`
    const blob = await resp.blob()
    // 触发浏览器下载
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    // 延迟释放 URL，确保浏览器已开始下载（部分浏览器 a.click() 是异步的）
    setTimeout(() => URL.revokeObjectURL(url), 1000)
    return { filename, size: blob.size }
  },
  /** 补充信息后重新生成需求分析（异步任务模式，返回 task_id 供轮询）
   *  v2.17: 支持上传新需求文件（可选），以新文件内容为准重新分析
   *  后端统一使用 multipart/form-data（Form + File），前端统一用 FormData
   */
  regenerate: (id: string, extraNotes: string, files?: File[]) => {
    const form = new FormData()
    form.append('extra_notes', extraNotes || '')
    if (files && files.length > 0) {
      files.forEach(f => form.append('files', f))
    }
    const headers: Record<string, string> = {}
    const token = getToken()
    if (token) headers['Authorization'] = `Bearer ${token}`
    return fetch(`${BASE}/requirements/${id}/regenerate`, {
      method: 'POST',
      body: form,
      headers,
    }).then(async res => {
      if (res.status === 401) { forceLogout(); throw new Error('登录已过期，请重新登录') }
      const json = await res.json()
      if (!res.ok && json.detail) throw new Error(json.detail)
      if (json.success === false) throw new Error(json.message || '请求失败')
      return json.data as { task_id: string; req_id: string; status: string; previous_title: string }
    })
  },
  /** v1.6: 确认需求完成（pending_review → confirmed），允许生成测试用例 */
  confirm: (id: string) =>
    request<{ req_id: string; status: string }>(`/requirements/${id}/confirm`, { method: 'POST' }),
  /** v2.33: 轻量查询 revision + 任务状态，供前端轮询检测多人协作冲突 */
  getRevision: (id: string) =>
    request<RequirementRevision>(`/requirements/${id}/revision`),
  /** v2.14: 查询分析任务列表（任务管理） */
  listTasks: (params?: { status?: string; task_type?: string; product_id?: string; limit?: number; offset?: number }) => {
    const query = new URLSearchParams()
    if (params?.status) query.append('status', params.status)
    if (params?.task_type) query.append('task_type', params.task_type)
    if (params?.product_id) query.append('product_id', params.product_id)
    if (params?.limit) query.append('limit', String(params.limit))
    if (params?.offset) query.append('offset', String(params.offset))
    const qs = query.toString()
    return request<{ items: AnalysisTaskInfo[]; total: number; server_now?: string }>(`/requirements/tasks${qs ? '?' + qs : ''}`)
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
  /** v3.0: 自动生成 FP 影响说明并写入 SRR */
  generateImpactDescription: (reqId: string, fpId: string) =>
    request<import('../types').FPImpactDescription>(`/requirements/${reqId}/fp/${fpId}/impact-description`, { method: 'POST' }),
  /** v3.0: 保存用户编辑后的 FP 影响说明 */
  saveImpactDescription: (reqId: string, fpId: string, payload: { description: string; test_focus?: string }) =>
    request(`/requirements/${reqId}/impact-description/${fpId}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  /** v3.1: 全量影响分析（POST /impact-analysis，返回 RequirementImpactResult 结构） */
  runImpactAnalysis: (reqId: string) =>
    request<import('../types').RequirementImpactResult>(`/requirements/${reqId}/impact-analysis`, { method: 'POST' }),
  /** v3.1: 用户确认勾选的受影响 FP（写入 SRR.impact_analysis，is_affected=True + confirmed_at） */
  confirmImpactAnalysis: (reqId: string, payload: import('../types').ImpactConfirmPayload) =>
    request<import('../types').ImpactConfirmResult>(`/requirements/${reqId}/impact-analysis/confirm`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  fpUpdate: (reqId: string, fpId: string, updates: { name?: string; module_id?: string; modules?: string[]; fp_type?: string; srr_json?: any }) =>
    request<{ id: string; name: string }>(`/requirements/${reqId}/fp/${fpId}`, {
      method: 'PUT', body: JSON.stringify(updates),
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
  fpMerge: (reqId: string, fpIds: string[], mergedName?: string) =>
    request<{ id: string; name: string; fp_type: string }>(
      `/requirements/${reqId}/fp/merge`,
      { method: 'POST', body: JSON.stringify({ fp_ids: fpIds, merged_name: mergedName || '' }) },
    ),
  /** v2.3: 列出可复用的已归档功能点 */
  fpListReusable: (reqId: string, moduleId?: string, q?: string) => {
    const params = new URLSearchParams()
    if (moduleId) params.append('module_id', moduleId)
    if (q) params.append('q', q)
    const qs = params.toString() ? `?${params.toString()}` : ''
    return request<{ items: import('../types').FPReusableItem[]; total: number }>(
      `/requirements/${reqId}/fp/reusable${qs}`
    )
  },
  /** v2.3: 复用已归档功能点 */
  fpReuse: (reqId: string, sourceFpId: string, name?: string) =>
    request<{ id: string; name: string; fp_type: string; module_id: string | null; modules: string[]; status: string; source_fp_id: string; source_req_id: string }>(
      `/requirements/${reqId}/fp/reuse`,
      { method: 'POST', body: JSON.stringify({ source_fp_id: sourceFpId, name }) }
    ),
  /** v2.3: 审核功能点依赖关系 */
  fpReviewDependency: (reqId: string, depId: number, action: 'approve' | 'reject', reason = '') =>
    request<{ id: number; review_status: string; reviewed_by: string; reviewed_at: string }>(
      `/requirements/${reqId}/fp/dependencies/${depId}/review`,
      { method: 'PATCH', body: JSON.stringify({ action, reason }) }
    ),
  /** v2.3: 获取功能点依赖关系变动历史 */
  fpDependencyHistory: (reqId: string, depId?: number, action?: string, limit = 100) => {
    const params = new URLSearchParams({ limit: String(limit) })
    if (depId) params.append('dep_id', String(depId))
    if (action) params.append('action', action)
    return request<{ items: import('../types').FPDependencyHistoryItem[]; total: number }>(
      `/requirements/${reqId}/fp/dependencies/history?${params}`
    )
  },
}

// ============================================================
// 用例生成（SSE）
// ============================================================
export function generateCases(
  reqId: string,
  onEvent: (event: Record<string, unknown>) => void,
  onError?: (err: string) => void,
  onDone?: () => void,
  opts?: { resume?: boolean; resume_from_task?: string },
): AbortController {
  const controller = new AbortController()
  const token = getToken()
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (token) headers['Authorization'] = `Bearer ${token}`

  const body: Record<string, unknown> = { req_id: reqId }
  if (opts?.resume) body.resume = true
  if (opts?.resume_from_task) body.resume_from_task = opts.resume_from_task

  fetch(`${BASE}/generate`, {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
    signal: controller.signal,
  }).then(async (res) => {
    // 错误响应处理：后端可能返回 400/404/409 等，body 为 JSON 错误信息而非 SSE 流
    // 不检查 res.ok 会导致前端尝试读取错误响应体作为 SSE 流，无法解析出任何 data: 事件，看起来像是卡住
    if (!res.ok) {
      let errMsg = `生成请求失败（HTTP ${res.status}）`
      try {
        const errBody = await res.json()
        // 后端 ApiResponse 或 FastAPI HTTPException 错误格式兼容
        const detail = errBody?.detail || errBody?.message || errBody?.error
        if (detail) errMsg = typeof detail === 'string' ? detail : JSON.stringify(detail)
      } catch { /* 非 JSON 响应体，使用默认错误消息 */ }
      onError?.(errMsg)
      return
    }
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
  list: (reqId?: string, projectId?: string, search?: string, page = 1, pageSize = 20, productId?: string) => {
    const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) })
    if (reqId) params.set('req_id', reqId)
    if (projectId) params.set('project_id', projectId)
    if (search) params.set('search', search)
    if (productId) params.set('product_id', productId)
    return request<PaginatedData<TestCase>>(`/testcases?${params}`)
  },
  get: (id: string) => request<TestCase>(`/testcases/${id}`),
  /** 人工新增用例（必须关联需求 req_id） */
  create: (data: {
    req_id: string
    product_id?: string
    title: string
    type?: string
    priority?: string
    description?: string
    preconditions?: string[]
    steps?: { step: number; action: string; expected: string }[]
    expected?: string
    assertions?: { target: string; comparison: string; value: string }[]
    functional_point?: string
  }) =>
    request<{ id: string; req_id: string; product_id: string; title: string; type: string; priority: string; status: string; functional_point?: string }>(
      '/testcases',
      { method: 'POST', body: JSON.stringify(data) },
    ),
  update: (id: string, data: Record<string, unknown>) =>
    request(`/testcases/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  delete: (id: string) => request(`/testcases/${id}`, { method: 'DELETE' }),
  deleteByRequirement: (reqId: string) =>
    request<{ deleted_count: number }>(`/testcases/batch/by-requirement/${reqId}`, { method: 'DELETE' }),
  feedback: (id: string, accepted: boolean, comments = '') =>
    request(`/testcases/${id}/feedback`, {
      method: 'POST',
      body: JSON.stringify({ accepted, comments }),
    }),
  batchFeedback: (caseIds: string[], accepted: boolean, comments = '') =>
    request<{ updated_count: number; new_status: string }>(`/testcases/batch/feedback`, {
      method: 'POST',
      body: JSON.stringify({ case_ids: caseIds, accepted, comments }),
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
  /**
   * v3.3（docs/14 方向 B / B5）：用例库质量报表。
   *
   * 注意 `reuse.note` 与 `weight.note` 是**后端刻意附带的免责说明**：
   * 复用计数自 v3.3 才上线（历史未计入）、weight 公式部分输入尚无数据。
   * 前端**必须展示**这两条，否则用户会把"计数器刚上线"误读成"用例库没被用"。
   */
  caseQualityReport: (productId?: string, topN = 10) => {
    const q = new URLSearchParams({ top_n: String(topN) })
    if (productId) q.set('product_id', productId)
    return request<{
      product_id: string | null
      total: number
      status_distribution: Record<string, number>
      status_ratio: Record<string, number>
      reuse: {
        approved_total: number
        reused_count: number
        reuse_rate: number
        benchmark: number
        note: string
      }
      weight: {
        mean: number
        std: number
        min: number
        max: number
        histogram: { bucket: string; count: number; ratio: number }[]
        note: string
      }
      top_reused: { id: string; title: string; execution_count: number; weight: number; status: string }[]
      never_reused: {
        count: number
        samples: { id: string; title: string; weight: number }[]
      }
    }>(`/knowledge/case-quality-report?${q.toString()}`)
  },
  patterns: (category?: string, page = 1, pageSize = 20) => {
    const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) })
    if (category) params.set('category', category)
    return request<PaginatedData<TestPattern>>(`/knowledge/patterns?${params}`)
  },
  listEntries: (productId?: string, type?: string, filters?: { search?: string; vectorized?: string; modules?: string[]; limit?: number }) => {
    const params = new URLSearchParams()
    if (productId) params.set('product_id', productId)
    if (type) params.set('type', type)
    if (filters?.search) params.set('search', filters.search)
    if (filters?.vectorized) params.set('vectorized', filters.vectorized)
    if (filters?.modules?.length) params.set('modules', filters.modules.join(','))
    if (filters?.limit) params.set('limit', String(filters.limit))
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
  /** 获取多模态 LLM 配置（图片/流程图识别专用） */
  getVisionLlm: () => request<{ provider: string; providers: Array<{ provider: string; provider_label: string; model: string; is_primary: boolean }> }>('/knowledge/vision-llm'),
  /** 设置多模态 LLM 供应商 */
  setVisionLlm: (provider: string) =>
    request<{ provider: string }>('/knowledge/vision-llm', {
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
  correct: (
    id: string,
    correctedAnswer: string,
    correctionNotes = '',
    options?: {
      correction_module?: string;
      correction_target_type?: 'requirement' | 'functional_point';
      correction_target_id?: string;
    }
  ) =>
    request<KbQARecord>('/knowledge-qa/correct', {
      method: 'POST',
      body: JSON.stringify({
        id,
        corrected_answer: correctedAnswer,
        correction_notes: correctionNotes,
        correction_module: options?.correction_module || '',
        correction_target_type: options?.correction_target_type || 'requirement',
        correction_target_id: options?.correction_target_id || '',
      }),
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
  // v2.8.56: 纠正审核接口
  corrections: (params: { status?: string; module?: string; product_id?: string; page?: number; page_size?: number } = {}) =>
    request<PaginatedData<KbQARecord>>(
      `/knowledge-qa/corrections?status=${params.status || 'pending_review'}&module=${params.module || ''}&product_id=${params.product_id || ''}&page=${params.page || 1}&page_size=${params.page_size || 20}`
    ),
  approve: (id: string, reviewNotes = '') =>
    request<{ archived_id: string; archived_type: string; correction_target_type: string }>(
      `/knowledge-qa/corrections/${id}/approve`,
      { method: 'POST', body: JSON.stringify({ id, review_notes: reviewNotes }) }
    ),
  reject: (id: string, reviewNotes = '') =>
    request<{ correction_status: string }>(
      `/knowledge-qa/corrections/${id}/reject`,
      { method: 'POST', body: JSON.stringify({ id, review_notes: reviewNotes }) }
    ),
}

// ============================================================
// 系统设置（多供应商）
// ============================================================
export const settings = {
  get: () => request<LLMConfig>('/settings/llm'),
  update: (providers: Array<{
    provider: string; provider_label?: string; api_key: string;
    base_url?: string; model?: string; is_primary?: boolean;
    enable_thinking?: boolean; thinking_params?: string
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
  // v3.5.5: 生成性能设置（FP 子批并发开关）
  getGeneration: () =>
    request<{ fp_concurrency: number; min: number; max: number }>('/settings/generation'),
  setGeneration: (fp_concurrency: number) =>
    request<{ fp_concurrency: number }>('/settings/generation', {
      method: 'PUT',
      body: JSON.stringify({ fp_concurrency }),
    }),
  // v3.5.6: 供应商启用/禁用（持久化，管理员手动操作）
  setProviderEnabled: (provider: string, enabled: boolean) =>
    request<{ provider: string; enabled: boolean }>(`/settings/llm/${encodeURIComponent(provider)}/enabled`, {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
    }),
  // v3.5.6: 管理员通知
  listNotifications: (params: { only_unread?: boolean; page?: number; page_size?: number } = {}) => {
    const sp = new URLSearchParams()
    if (params.only_unread) sp.set('only_unread', 'true')
    sp.set('page', String(params.page || 1))
    sp.set('page_size', String(params.page_size || 20))
    return request<{ items: Array<{ id: number; type: string; title: string; content: string; related_type: string; related_id: string; is_read: boolean; created_at: string | null }>; total: number; unread: number; page: number; page_size: number }>(`/settings/notifications?${sp}`)
  },
  markNotificationsRead: (ids?: number[]) =>
    request<{ marked: number }>('/settings/notifications/read', {
      method: 'POST',
      body: JSON.stringify(ids || null),
    }),
}

// ============================================================
// v2.22: 日志管理（管理员） — LLM 调用日志 + 用户操作日志
// ============================================================
export const logs = {
  /** LLM 调用日志列表（分页，不含完整 prompt/response） */
  listLlmLogs: (params: {
    page?: number; page_size?: number;
    agent_name?: string; model?: string; task_id?: string; req_id?: string;
    success?: boolean; start_date?: string; end_date?: string;
  } = {}) => {
    const sp = new URLSearchParams({
      page: String(params.page || 1),
      page_size: String(params.page_size || 20),
    })
    if (params.agent_name) sp.set('agent_name', params.agent_name)
    if (params.model) sp.set('model', params.model)
    if (params.task_id) sp.set('task_id', params.task_id)
    if (params.req_id) sp.set('req_id', params.req_id)
    if (params.success !== undefined) sp.set('success', String(params.success))
    if (params.start_date) sp.set('start_date', params.start_date)
    if (params.end_date) sp.set('end_date', params.end_date)
    return request<PaginatedData<LLMCallLogItem>>(`/admin/llm-logs?${sp}`)
  },
  /** LLM 调用日志详情（含完整 system_prompt/user_prompt/response/parsed_json） */
  getLlmLogDetail: (id: number) =>
    request<LLMCallLogDetail>(`/admin/llm-logs/${id}`),
  /** 用户操作日志列表（分页） */
  listOperationLogs: (params: {
    page?: number; page_size?: number;
    username?: string; action?: string; target_type?: string; target_id?: string;
    success?: boolean; start_date?: string; end_date?: string;
  } = {}) => {
    const sp = new URLSearchParams({
      page: String(params.page || 1),
      page_size: String(params.page_size || 20),
    })
    if (params.username) sp.set('username', params.username)
    if (params.action) sp.set('action', params.action)
    if (params.target_type) sp.set('target_type', params.target_type)
    if (params.target_id) sp.set('target_id', params.target_id)
    if (params.success !== undefined) sp.set('success', String(params.success))
    if (params.start_date) sp.set('start_date', params.start_date)
    if (params.end_date) sp.set('end_date', params.end_date)
    return request<PaginatedData<OperationLogItem>>(`/admin/operation-logs?${sp}`)
  },
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
  download: async (format: string, caseIds?: string[], projectName?: string, includeDetails?: boolean) => {
    const params = new URLSearchParams({ format })
    const url = `${BASE}/export?${params}`
    // ⚠️ 必须携带 Authorization 头：后端 export_testcases 依赖 Depends(get_current_user)
    const token = getToken()
    const headers: Record<string, string> = { 'Content-Type': 'application/json' }
    if (token) headers['Authorization'] = `Bearer ${token}`
    if (caseIds?.length) {
      const body: Record<string, unknown> = { format, case_ids: caseIds }
      if (projectName) body.project_name = projectName
      if (includeDetails !== undefined) body.include_details = includeDetails
      const res = await fetch(url, {
        method: 'POST',
        headers,
        body: JSON.stringify(body),
      })
      if (res.status === 401) { forceLogout(); throw new Error('登录已过期，请重新登录') }
      if (!res.ok) {
        const j = await res.json().catch(() => ({}))
        throw new Error(j.detail || j.message || `导出失败 (${res.status})`)
      }
      return res.blob()
    }
    // 无 caseIds 时仍需 POST + 空 body 以匹配后端签名（后端要求 case_ids 非空，会返回 400）
    const res = await fetch(url, { method: 'POST', headers, body: JSON.stringify({ format }) })
    if (res.status === 401) { forceLogout(); throw new Error('登录已过期，请重新登录') }
    if (!res.ok) {
      const j = await res.json().catch(() => ({}))
      throw new Error(j.detail || j.message || `导出失败 (${res.status})`)
    }
    return res.blob()
  },
}

// ============================================================
// 产品/项目/模块
// ============================================================
export const products = {
  list: () => request<import('../types').Product[]>('/products/'),
  create: (name: string, desc = '') =>
    request('/products/', { method: 'POST', body: JSON.stringify({ name, description: desc }) }),
  /** 删除产品（需传入 confirm_name 进行二次确认，名称必须完全匹配） */
  delete: (id: string, confirmName: string) =>
    request(`/products/${id}?confirm_name=${encodeURIComponent(confirmName)}`, { method: 'DELETE' }),
  /** 删除预检查：返回产品名称与关联数据统计（项目/模块/需求/用例/功能点数量） */
  deleteCheck: (id: string) =>
    request<{ id: string; name: string; stats: { projects: number; modules: number; requirements: number; test_cases: number; functional_points: number } }>(`/products/${id}/delete-check`),
  listProjects: (pid: string) =>
    request<import('../types').Project[]>(`/products/${pid}/projects`),
  /** v3.2.7: 项目详情单查（项目详情页双标签用，刷新不丢元信息） */
  getProjectDetail: (projId: string) =>
    request<import('../types').Project>(`/products/projects/${projId}/detail`),
  createProject: (pid: string, name: string, desc = '', version = '1.0') =>
    request(`/products/${pid}/projects`, {
      method: 'POST', body: JSON.stringify({ name, description: desc, version }),
    }),
  deleteProject: (id: string) =>
    request(`/products/projects/${id}`, { method: 'DELETE' }),
  listModules: (pid: string) =>
    request<import('../types').Module[]>(`/products/${pid}/modules`),
  createModule: (pid: string, name: string, desc = '') =>
    request(`/products/${pid}/modules`, {
      method: 'POST', body: JSON.stringify({ name, description: desc }),
    }),
  deleteModule: (id: string) =>
    request(`/products/modules/${id}`, { method: 'DELETE' }),
  updateModule: (id: string, name: string, description = '') =>
    request(`/products/modules/${id}`, {
      method: 'PUT',
      body: JSON.stringify({ name, description }),
    }),
  updateProject: (id: string, name: string, description = '', version = '1.0') =>
    request(`/products/projects/${id}`, {
      method: 'PUT',
      body: JSON.stringify({ name, description, version }),
    }),
  // v2.45 项目归档
  archiveProject: (projectId: string, goLiveDate: string, changeSummary?: string) =>
    request<{ project_id: string; status: string; version_id: string; requirement_count: number; fp_count: number; dependency_count: number; kb_written: number; go_live_date: string }>(
      `/products/projects/${projectId}/archive`,
      { method: 'POST', body: JSON.stringify({ go_live_date: goLiveDate, change_summary: changeSummary || '' }) }
    ),
  // v2.45 项目恢复活动
  reactivateProject: (projectId: string) =>
    request<{ project_id: string; status: string; requirement_count: number; fp_count: number; versions_deactivated: number }>(
      `/products/projects/${projectId}/reactivate`,
      { method: 'POST' }
    ),
  // v3.2.13 项目级修改上线时间 + 知识库元数据重整合
  updateProjectGoLiveDate: (projectId: string, goLiveDate: string, reason = '') =>
    request<{
      project_id: string; go_live_date: string; changed: boolean;
      old_go_live_date?: string; requirement_count: number; version_count?: number;
      fp_count?: number; milvus_updated: boolean; milvus_count?: number;
      milvus_error?: string; cache_invalidated?: number;
      fp_case_sets_refreshed?: number; fp_case_sets_failed?: string[];
    }>(
      `/products/projects/${projectId}/go-live-date`,
      { method: 'PATCH', body: JSON.stringify({ go_live_date: goLiveDate, reason }) }
    ),
  // v2.45 依赖版本查询
  listDependencyVersionsByProduct: (productId: string) =>
    request<DependencyVersion[]>(`/products/${productId}/fp/dependency-versions`),
  listDependencyVersionsByProject: (projectId: string) =>
    request<DependencyVersion[]>(`/products/projects/${projectId}/fp/dependency-versions`),
  getDependencyVersionDetail: (versionPk: number) =>
    request<DependencyVersionDetail>(`/products/fp/dependency-versions/${versionPk}`),
  diffDependencyVersions: (v1: number, v2: number) =>
    request<VersionDiff>(`/products/fp/dependency-versions/${v1}/diff/${v2}`),
  // v2.45 知识库按项目展示
  listKbProjects: (productId: string) =>
    request<KbProject[]>(`/products/${productId}/kb/projects`),
  listKbEntriesByProject: (projectId: string) =>
    request<{ project: any; requirements: any[]; test_cases: any[] }>(`/products/projects/${projectId}/kb/entries`),
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
  /** v2.3: 获取提示词修改历史 */
  history: (module: string, name: string, limit = 50) =>
    request<{ items: import('../types').PromptHistoryItem[]; total: number }>(
      `/admin/prompts/${module}/${name}/history?limit=${limit}`
    ),
  /** v2.3: 从历史版本恢复提示词 */
  restore: (module: string, name: string, historyId: number) =>
    request<{ prompt_id: string; module: string; name: string; value: string; restored_from_history_id: number; changed_by: string }>(
      `/admin/prompts/${module}/${name}/restore/${historyId}`,
      { method: 'POST' }
    ),
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
  // 功能点依赖图谱（跨需求）v2.3: 支持 module_id 和 review_status 筛选
  // v2.46: 新增 includeInactive，默认为 true（项目归档后 is_active=false 的冻结边也要展示给用户查看历史依赖；用户可主动关闭仅看活跃边）
  fpGraph: (productId: string, moduleId?: string, reviewStatus?: string, includeInactive: boolean = true) => {
    const params = new URLSearchParams()
    if (moduleId) params.append('module_id', moduleId)
    if (reviewStatus) params.append('review_status', reviewStatus)
    // include_inactive=false: 只展示 is_active=true 的活跃边（新分析/新编辑用）
    // include_inactive=true (默认): 展示所有边，含项目归档后的冻结历史边
    params.append('include_inactive', includeInactive ? 'true' : 'false')
    const qs = params.toString() ? `?${params.toString()}` : ''
    return request<{ nodes: any[]; edges: any[]; modules: { id: string; name: string }[] }>(
      `/products/${productId}/fp/dependency-graph${qs}`
    )
  },
  fpImpact: (productId: string, fpId: string) =>
    request<{
      source_fp_name: string; affected: any[]; affected_modules: any[];
      total: number; impact_summary: string;
    }>(`/products/${productId}/fp/impact?fp_id=${fpId}`),
  /** v3.2: 功能点权威完整版 SRR（跨版本聚合，最新最完整内容） */
  fpCrsr: (fpId: string) =>
    request<import('../types').FpCrsrResult>(`/products/fp-crsr/${fpId}`),
}

// ============================================================
// 评审报告中心（v3.1.3 P1）
// ============================================================
export const reviewReportsApi = {
  /** 报告列表（产品隔离 + 类型/需求筛选 + 分页） */
  list: (params?: {
    product_id?: string
    report_type?: string
    req_id?: string
    task_id?: string
    page?: number
    page_size?: number
  }) => {
    const query = new URLSearchParams()
    if (params?.product_id) query.append('product_id', params.product_id)
    if (params?.report_type) query.append('report_type', params.report_type)
    if (params?.req_id) query.append('req_id', params.req_id)
    if (params?.task_id) query.append('task_id', params.task_id)
    if (params?.page) query.append('page', String(params.page))
    if (params?.page_size) query.append('page_size', String(params.page_size))
    const qs = query.toString()
    return request<{ items: import('../types').ReviewReportSummary[]; total: number; page: number; page_size: number }>(
      `/review-reports${qs ? '?' + qs : ''}`
    )
  },
  /** 报告详情（含维度置信度、问题列表、原始报告） */
  get: (reportId: string) =>
    request<import('../types').ReviewReportDetail>(`/review-reports/${reportId}`),
  /** 类型统计（顶部概览卡片） */
  stats: (productId?: string) => {
    const params = productId ? `?product_id=${productId}` : ''
    return request<Record<string, { count: number; avg_confidence: number }>>(`/review-reports/stats${params}`)
  },
  /** P2: 批量采纳/驳回报告意见 */
  adoptIssues: (
    reportId: string,
    items: Array<{ issue_id: string; accepted: boolean; comment?: string }>,
  ) =>
    request<{
      updated: number
      not_found?: string[]
      adoption_summary?: { total: number; accepted: number; rejected: number; pending: number }
    }>(`/review-reports/${reportId}/issues/adopt`, {
      method: 'POST',
      body: JSON.stringify({ items }),
    }),
  /** P3: 可整合的目标提示词列表 */
  evolutionTargets: () =>
    request<{ items: import('../types').EvolutionTarget[] }>('/review-reports/prompt-evolution/targets'),
  /** P3: 执行多报告意见整合 → 提示词新版本 */
  runEvolution: (params: {
    product_id: string
    report_ids: string[]
    target_prompt_id: string
    manual_notes?: string
  }) =>
    request<import('../types').EvolutionResult>('/review-reports/prompt-evolution', {
      method: 'POST',
      body: JSON.stringify(params),
    }),
  /** P3: 整合历史列表 */
  evolutionHistory: (params?: { product_id?: string; target_prompt_id?: string; limit?: number }) => {
    const query = new URLSearchParams()
    if (params?.product_id) query.append('product_id', params.product_id)
    if (params?.target_prompt_id) query.append('target_prompt_id', params.target_prompt_id)
    if (params?.limit) query.append('limit', String(params.limit))
    const qs = query.toString()
    return request<{ items: import('../types').EvolutionHistoryItem[]; total: number }>(
      `/review-reports/prompt-evolution${qs ? '?' + qs : ''}`
    )
  },
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

// ============================================================
// 可观测性：服务器资源与生成并发（v4.0.2）
// ============================================================
export const observability = {
  /** 服务器资源 + 并发生成负载快照（首页仪表盘 10s 轮询） */
  systemResources: () => request<SystemResources>('/observability/system-resources'),
}
