import { useState, useEffect, useCallback } from 'react'
import {
  Loader2, ChevronLeft, ChevronRight, X, CheckCircle2, XCircle,
  Cpu, FileText, Activity, Eye,
} from 'lucide-react'
import { logs as logsApi } from '../api/client'
import type { LLMCallLogItem, LLMCallLogDetail, OperationLogItem } from '../types'

type Tab = 'llm' | 'operation'

const AGENT_LABELS: Record<string, string> = {
  req_agent: '需求分析',
  fp_agent: '功能点拆分',
  clarify_agent: '需求澄清',
  gen_agent: '用例生成',
  review_agent: '用例评审',
  impact_agent: '影响分析',
}

const ACTION_LABELS: Record<string, string> = {
  login: '登录',
  logout: '登出',
  change_password: '修改密码',
  upload_requirement: '上传需求',
  submit_clarify: '提交澄清',
  regenerate_requirement: '重新生成',
  archive_requirement: '归档需求',
  delete_requirement: '删除需求',
  generate_cases: '生成用例',
  approve_fp: '审核功能点',
  merge_fp: '合并功能点',
  delete_fp: '删除功能点',
  update_prompt: '修改提示词',
}

function formatTime(iso: string): string {
  if (!iso) return '-'
  const d = new Date(iso)
  return d.toLocaleString('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

function formatDuration(ms: number): string {
  if (!ms) return '-'
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(2)}s`
}

function formatTokens(tokens: Record<string, number> | number | null): string {
  if (!tokens) return '-'
  // 兼容 dict 形式 {prompt_tokens, completion_tokens, total_tokens}
  const total = typeof tokens === 'number' ? tokens : (tokens.total_tokens || (tokens.prompt_tokens || 0) + (tokens.completion_tokens || 0))
  if (!total) return '-'
  if (total < 1000) return `${total}`
  return `${(total / 1000).toFixed(1)}k`
}

function formatDetail(detail: Record<string, unknown> | string | null | undefined): string {
  if (!detail) return '-'
  if (typeof detail === 'string') return detail
  try {
    return JSON.stringify(detail)
  } catch {
    return String(detail)
  }
}

// ============================================================================
// LLM 日志详情弹窗
// ============================================================================
function LlmLogDetailModal({ logId, onClose }: { logId: number; onClose: () => void }) {
  const [detail, setDetail] = useState<LLMCallLogDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [activeSection, setActiveSection] = useState<'system' | 'user' | 'response' | 'parsed'>('system')

  useEffect(() => {
    setLoading(true)
    logsApi.getLlmLogDetail(logId)
      .then(setDetail)
      .catch(() => setDetail(null))
      .finally(() => setLoading(false))
  }, [logId])

  const copyText = (text: string) => {
    navigator.clipboard.writeText(text).catch(() => {})
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" style={{ background: 'rgba(0, 0, 0, 0.4)', backdropFilter: 'blur(4px)' }} onClick={onClose}>
      <div className="max-w-5xl w-full max-h-[90vh] flex flex-col" style={{ background: 'var(--background)', borderRadius: 'calc(var(--radius) * 1.1)', boxShadow: 'var(--shadow-apple-xl)' }} onClick={e => e.stopPropagation()}>
        {/* 头部 */}
        <div className="flex items-center justify-between px-6 py-4 border-b" style={{ borderColor: 'var(--border)' }}>
          <div className="flex items-center gap-3">
            <Cpu className="w-5 h-5" style={{ color: 'var(--brand-500)' }} />
            <h2 className="text-lg font-semibold" style={{ color: 'var(--foreground)' }}>LLM 调用详情 #{logId}</h2>
            {detail && (
              <div className="flex items-center gap-2 text-xs">
                <span className="px-2 py-0.5 rounded" style={{ background: 'var(--background-muted)', color: 'var(--foreground-secondary)' }}>
                  {AGENT_LABELS[detail.agent_name] || detail.agent_name}
                </span>
                <span className="px-2 py-0.5 rounded" style={{ background: 'var(--background-muted)', color: 'var(--foreground-secondary)' }}>{detail.provider}</span>
                <span className="px-2 py-0.5 rounded" style={{ background: 'var(--background-muted)', color: 'var(--foreground-secondary)' }}>{detail.model}</span>
                {detail.success ? (
                  <span className="px-2 py-0.5 rounded" style={{ background: 'var(--state-success-surface)', color: 'var(--state-success)' }}>成功</span>
                ) : (
                  <span className="px-2 py-0.5 rounded" style={{ background: 'var(--state-error-surface)', color: 'var(--state-error)' }}>失败</span>
                )}
              </div>
            )}
          </div>
          <button onClick={onClose} className="p-1.5 rounded-lg" style={{ color: 'var(--foreground-tertiary)' }}>
            <X className="w-5 h-5" />
          </button>
        </div>

        {loading ? (
          <div className="flex-1 flex items-center justify-center py-12">
            <Loader2 className="w-6 h-6 animate-spin" style={{ color: 'var(--brand-500)' }} />
          </div>
        ) : !detail ? (
          <div className="flex-1 flex items-center justify-center py-12" style={{ color: 'var(--foreground-tertiary)' }}>加载失败</div>
        ) : (
          <>
            {/* 元信息 */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3 px-6 py-3 border-b text-xs" style={{ background: 'var(--background-muted)', borderColor: 'var(--border)' }}>
              <div>
                <span style={{ color: 'var(--foreground-tertiary)' }}>时间：</span>
                <span style={{ color: 'var(--foreground-secondary)' }}>{formatTime(detail.created_at)}</span>
              </div>
              <div>
                <span style={{ color: 'var(--foreground-tertiary)' }}>耗时：</span>
                <span style={{ color: 'var(--foreground-secondary)' }}>{formatDuration(detail.duration_ms)}</span>
              </div>
              <div>
                <span style={{ color: 'var(--foreground-tertiary)' }}>Token：</span>
                <span style={{ color: 'var(--foreground-secondary)' }}>{formatTokens(detail.token_usage)}</span>
              </div>
              <div>
                <span style={{ color: 'var(--foreground-tertiary)' }}>温度：</span>
                <span style={{ color: 'var(--foreground-secondary)' }}>{detail.temperature}</span>
              </div>
              {detail.task_id && (
                <div className="truncate">
                  <span style={{ color: 'var(--foreground-tertiary)' }}>任务 ID：</span>
                  <span className="font-mono" style={{ color: 'var(--foreground-secondary)' }}>{detail.task_id}</span>
                </div>
              )}
              {detail.req_id && (
                <div className="truncate">
                  <span style={{ color: 'var(--foreground-tertiary)' }}>需求 ID：</span>
                  <span className="font-mono" style={{ color: 'var(--foreground-secondary)' }}>{detail.req_id}</span>
                </div>
              )}
              {detail.error_message && (
                <div className="col-span-2" style={{ color: 'var(--state-error)' }}>
                  <span style={{ color: 'var(--state-error)' }}>错误：</span>
                  {detail.error_message}
                </div>
              )}
            </div>

            {/* 内容区 Tab */}
            <div className="flex items-center gap-1 px-6 pt-3 border-b" style={{ borderColor: 'var(--border)' }}>
              {([
                { key: 'system', label: 'System Prompt' },
                { key: 'user', label: 'User Prompt' },
                { key: 'response', label: 'Response' },
                { key: 'parsed', label: 'Parsed JSON' },
              ] as const).map(t => {
                const hasContent = t.key === 'system' ? !!detail.system_prompt
                  : t.key === 'user' ? !!detail.user_prompt
                  : t.key === 'response' ? !!detail.response
                  : !!detail.parsed_json
                return (
                  <button
                    key={t.key}
                    onClick={() => setActiveSection(t.key)}
                    className={`px-3 py-2 text-xs font-medium border-b-2 transition-colors ${!hasContent ? 'opacity-40 cursor-not-allowed' : ''}`}
                    style={activeSection === t.key
                      ? { borderColor: 'var(--brand-500)', color: 'var(--brand-500)' }
                      : { borderColor: 'transparent', color: 'var(--foreground-tertiary)' }}
                    disabled={!hasContent}
                  >
                    {t.label}
                  </button>
                )
              })}
            </div>

            {/* 文本内容 */}
            <div className="flex-1 overflow-auto px-6 py-4">
              {activeSection === 'parsed' && detail.parsed_json ? (
                <pre className="text-xs font-mono whitespace-pre-wrap break-all" style={{ color: 'var(--foreground-secondary)' }}>
                  {JSON.stringify(detail.parsed_json, null, 2)}
                </pre>
              ) : (
                <div className="relative">
                  <pre className="text-xs font-mono whitespace-pre-wrap break-all pr-12" style={{ color: 'var(--foreground-secondary)' }}>
                    {activeSection === 'system' ? (detail.system_prompt || '(空)')
                      : activeSection === 'user' ? (detail.user_prompt || '(空)')
                      : (detail.response || '(空)')}
                  </pre>
                  <button
                    onClick={() => copyText(
                      activeSection === 'system' ? (detail.system_prompt || '')
                        : activeSection === 'user' ? (detail.user_prompt || '')
                        : (detail.response || '')
                    )}
                    className="absolute top-0 right-0 px-2 py-1 text-[10px] border rounded"
                    style={{ background: 'var(--background)', borderColor: 'var(--border)', color: 'var(--foreground-tertiary)' }}
                  >
                    复制
                  </button>
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  )
}

// ============================================================================
// LLM 调用日志列表
// ============================================================================
function LlmLogsTab() {
  const [items, setItems] = useState<LLMCallLogItem[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize] = useState(20)
  const [loading, setLoading] = useState(true)
  const [detailId, setDetailId] = useState<number | null>(null)

  // 筛选
  const [agentName, setAgentName] = useState('')
  const [taskId, setTaskId] = useState('')
  const [reqId, setReqId] = useState('')
  const [successFilter, setSuccessFilter] = useState<'all' | 'success' | 'failed'>('all')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const res = await logsApi.listLlmLogs({
        page, page_size: pageSize,
        agent_name: agentName || undefined,
        task_id: taskId || undefined,
        req_id: reqId || undefined,
        success: successFilter === 'all' ? undefined : successFilter === 'success',
      })
      setItems(res.items || [])
      setTotal(res.total || 0)
    } catch {
      setItems([]); setTotal(0)
    }
    setLoading(false)
  }, [page, pageSize, agentName, taskId, reqId, successFilter])

  useEffect(() => { load() }, [load])

  const totalPages = Math.ceil(total / pageSize) || 1

  return (
    <div className="space-y-3">
      {/* 筛选 */}
      <div className="flex flex-wrap items-center gap-2 p-3 border" style={{ background: 'var(--background)', borderColor: 'var(--border)', borderRadius: 'calc(var(--radius) * 0.75)' }}>
        <select
          value={agentName}
          onChange={e => { setAgentName(e.target.value); setPage(1) }}
          className="input-field text-xs py-1.5 w-36"
        >
          <option value="">全部 Agent</option>
          {Object.entries(AGENT_LABELS).map(([k, v]) => (
            <option key={k} value={k}>{v}</option>
          ))}
        </select>
        <input
          value={taskId}
          onChange={e => { setTaskId(e.target.value); setPage(1) }}
          placeholder="任务 ID"
          className="input-field text-xs py-1.5 w-40"
        />
        <input
          value={reqId}
          onChange={e => { setReqId(e.target.value); setPage(1) }}
          placeholder="需求 ID"
          className="input-field text-xs py-1.5 w-40"
        />
        <select
          value={successFilter}
          onChange={e => { setSuccessFilter(e.target.value as any); setPage(1) }}
          className="input-field text-xs py-1.5 w-28"
        >
          <option value="all">全部状态</option>
          <option value="success">仅成功</option>
          <option value="failed">仅失败</option>
        </select>
        <button
          onClick={() => { setAgentName(''); setTaskId(''); setReqId(''); setSuccessFilter('all'); setPage(1) }}
          className="px-3 py-1.5 text-xs hover:text-[var(--foreground)]"
          style={{ color: 'var(--foreground-tertiary)' }}
        >
          重置
        </button>
        <span className="ml-auto text-xs" style={{ color: 'var(--foreground-tertiary)' }}>共 {total} 条</span>
      </div>

      {/* 表格 */}
      <div className="overflow-hidden border" style={{ background: 'var(--background)', borderColor: 'var(--border)', borderRadius: 'calc(var(--radius) * 0.75)' }}>
        {loading ? (
          <div className="flex items-center justify-center py-12">
            <Loader2 className="w-6 h-6 animate-spin" style={{ color: 'var(--brand-500)' }} />
          </div>
        ) : items.length === 0 ? (
          <div className="py-12 text-center text-sm" style={{ color: 'var(--foreground-tertiary)' }}>暂无 LLM 调用日志</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-xs" style={{ background: 'var(--background-muted)', color: 'var(--foreground-tertiary)' }}>
                <tr>
                  <th className="text-left px-3 py-2.5 font-medium">ID</th>
                  <th className="text-left px-3 py-2.5 font-medium">Agent</th>
                  <th className="text-left px-3 py-2.5 font-medium">模型</th>
                  <th className="text-left px-3 py-2.5 font-medium">任务/需求</th>
                  <th className="text-left px-3 py-2.5 font-medium">耗时</th>
                  <th className="text-left px-3 py-2.5 font-medium">Token</th>
                  <th className="text-left px-3 py-2.5 font-medium">状态</th>
                  <th className="text-left px-3 py-2.5 font-medium">时间</th>
                  <th className="text-right px-3 py-2.5 font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {items.map(log => (
                  <tr key={log.id} className="border-t hover:bg-[var(--background-muted)]" style={{ borderColor: 'var(--border)' }}>
                    <td className="px-3 py-2 text-xs" style={{ color: 'var(--foreground-tertiary)' }}>#{log.id}</td>
                    <td className="px-3 py-2">
                      <span className="px-2 py-0.5 rounded text-xs" style={{ background: 'rgba(0, 122, 255, 0.08)', color: 'var(--brand-500)' }}>
                        {AGENT_LABELS[log.agent_name] || log.agent_name}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-xs" style={{ color: 'var(--foreground-tertiary)' }}>{log.model}</td>
                    <td className="px-3 py-2 text-xs" style={{ color: 'var(--foreground-tertiary)' }}>
                      {log.task_id && <div className="font-mono truncate max-w-[120px]" title={log.task_id}>{log.task_id}</div>}
                      {log.req_id && <div className="font-mono truncate max-w-[120px]" style={{ color: 'var(--foreground-quaternary)' }} title={log.req_id}>{log.req_id}</div>}
                    </td>
                    <td className="px-3 py-2 text-xs" style={{ color: 'var(--foreground-tertiary)' }}>{formatDuration(log.duration_ms)}</td>
                    <td className="px-3 py-2 text-xs" style={{ color: 'var(--foreground-tertiary)' }}>{formatTokens(log.token_usage)}</td>
                    <td className="px-3 py-2">
                      {log.success ? (
                        <span className="flex items-center gap-1 text-xs" style={{ color: 'var(--state-success)' }}>
                          <CheckCircle2 className="w-3.5 h-3.5" /> 成功
                        </span>
                      ) : (
                        <span className="flex items-center gap-1 text-xs" style={{ color: 'var(--state-error)' }} title={log.error_message || ''}>
                          <XCircle className="w-3.5 h-3.5" /> 失败
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-xs whitespace-nowrap" style={{ color: 'var(--foreground-tertiary)' }}>{formatTime(log.created_at)}</td>
                    <td className="px-3 py-2 text-right">
                      <button
                        onClick={() => setDetailId(log.id)}
                        className="px-2 py-1 text-xs rounded inline-flex items-center gap-1 hover:bg-[rgba(0,122,255,0.08)]"
                        style={{ color: 'var(--brand-500)' }}
                      >
                        <Eye className="w-3.5 h-3.5" /> 详情
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* 分页 */}
        {!loading && items.length > 0 && (
          <div className="flex items-center justify-between px-3 py-2 border-t text-xs" style={{ borderColor: 'var(--border)' }}>
            <span style={{ color: 'var(--foreground-tertiary)' }}>
              第 {page} / {totalPages} 页
            </span>
            <div className="flex items-center gap-1">
              <button
                onClick={() => setPage(p => Math.max(1, p - 1))}
                disabled={page <= 1}
                className="p-1.5 rounded disabled:opacity-40 hover:bg-[var(--background-muted)]"
                style={{ color: 'var(--foreground-tertiary)' }}
              >
                <ChevronLeft className="w-4 h-4" />
              </button>
              <button
                onClick={() => setPage(p => Math.min(totalPages, p + 1))}
                disabled={page >= totalPages}
                className="p-1.5 rounded disabled:opacity-40 hover:bg-[var(--background-muted)]"
                style={{ color: 'var(--foreground-tertiary)' }}
              >
                <ChevronRight className="w-4 h-4" />
              </button>
            </div>
          </div>
        )}
      </div>

      {/* 详情弹窗 */}
      {detailId !== null && (
        <LlmLogDetailModal logId={detailId} onClose={() => setDetailId(null)} />
      )}
    </div>
  )
}

// ============================================================================
// 用户操作日志列表
// ============================================================================
function OperationLogsTab() {
  const [items, setItems] = useState<OperationLogItem[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize] = useState(20)
  const [loading, setLoading] = useState(true)

  // 筛选
  const [username, setUsername] = useState('')
  const [action, setAction] = useState('')
  const [targetType, setTargetType] = useState('')
  const [successFilter, setSuccessFilter] = useState<'all' | 'success' | 'failed'>('all')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const res = await logsApi.listOperationLogs({
        page, page_size: pageSize,
        username: username || undefined,
        action: action || undefined,
        target_type: targetType || undefined,
        success: successFilter === 'all' ? undefined : successFilter === 'success',
      })
      setItems(res.items || [])
      setTotal(res.total || 0)
    } catch {
      setItems([]); setTotal(0)
    }
    setLoading(false)
  }, [page, pageSize, username, action, targetType, successFilter])

  useEffect(() => { load() }, [load])

  const totalPages = Math.ceil(total / pageSize) || 1

  return (
    <div className="space-y-3">
      {/* 筛选 */}
      <div className="flex flex-wrap items-center gap-2 p-3 border" style={{ background: 'var(--background)', borderColor: 'var(--border)', borderRadius: 'calc(var(--radius) * 0.75)' }}>
        <input
          value={username}
          onChange={e => { setUsername(e.target.value); setPage(1) }}
          placeholder="用户名"
          className="input-field text-xs py-1.5 w-36"
        />
        <select
          value={action}
          onChange={e => { setAction(e.target.value); setPage(1) }}
          className="input-field text-xs py-1.5 w-40"
        >
          <option value="">全部操作</option>
          {Object.entries(ACTION_LABELS).map(([k, v]) => (
            <option key={k} value={k}>{v}</option>
          ))}
        </select>
        <input
          value={targetType}
          onChange={e => { setTargetType(e.target.value); setPage(1) }}
          placeholder="目标类型"
          className="input-field text-xs py-1.5 w-32"
        />
        <select
          value={successFilter}
          onChange={e => { setSuccessFilter(e.target.value as any); setPage(1) }}
          className="input-field text-xs py-1.5 w-28"
        >
          <option value="all">全部状态</option>
          <option value="success">仅成功</option>
          <option value="failed">仅失败</option>
        </select>
        <button
          onClick={() => { setUsername(''); setAction(''); setTargetType(''); setSuccessFilter('all'); setPage(1) }}
          className="px-3 py-1.5 text-xs hover:text-[var(--foreground)]"
          style={{ color: 'var(--foreground-tertiary)' }}
        >
          重置
        </button>
        <span className="ml-auto text-xs" style={{ color: 'var(--foreground-tertiary)' }}>共 {total} 条</span>
      </div>

      {/* 表格 */}
      <div className="overflow-hidden border" style={{ background: 'var(--background)', borderColor: 'var(--border)', borderRadius: 'calc(var(--radius) * 0.75)' }}>
        {loading ? (
          <div className="flex items-center justify-center py-12">
            <Loader2 className="w-6 h-6 animate-spin" style={{ color: 'var(--brand-500)' }} />
          </div>
        ) : items.length === 0 ? (
          <div className="py-12 text-center text-sm" style={{ color: 'var(--foreground-tertiary)' }}>暂无操作日志</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-xs" style={{ background: 'var(--background-muted)', color: 'var(--foreground-tertiary)' }}>
                <tr>
                  <th className="text-left px-3 py-2.5 font-medium">ID</th>
                  <th className="text-left px-3 py-2.5 font-medium">用户</th>
                  <th className="text-left px-3 py-2.5 font-medium">操作</th>
                  <th className="text-left px-3 py-2.5 font-medium">目标</th>
                  <th className="text-left px-3 py-2.5 font-medium">详情</th>
                  <th className="text-left px-3 py-2.5 font-medium">IP</th>
                  <th className="text-left px-3 py-2.5 font-medium">状态</th>
                  <th className="text-left px-3 py-2.5 font-medium">时间</th>
                </tr>
              </thead>
              <tbody>
                {items.map(log => (
                  <tr key={log.id} className="border-t hover:bg-[var(--background-muted)]" style={{ borderColor: 'var(--border)' }}>
                    <td className="px-3 py-2 text-xs" style={{ color: 'var(--foreground-tertiary)' }}>#{log.id}</td>
                    <td className="px-3 py-2 text-xs font-medium" style={{ color: 'var(--foreground-secondary)' }}>{log.username}</td>
                    <td className="px-3 py-2">
                      <span className="px-2 py-0.5 rounded text-xs" style={{ background: 'var(--background-muted)', color: 'var(--foreground-secondary)' }}>
                        {ACTION_LABELS[log.action] || log.action}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-xs" style={{ color: 'var(--foreground-tertiary)' }}>
                      <div>{log.target_type || '-'}</div>
                      {log.target_id && <div className="font-mono truncate max-w-[120px]" style={{ color: 'var(--foreground-quaternary)' }} title={log.target_id}>{log.target_id}</div>}
                    </td>
                    <td className="px-3 py-2 text-xs max-w-xs truncate" style={{ color: 'var(--foreground-tertiary)' }} title={formatDetail(log.detail)}>{formatDetail(log.detail)}</td>
                    <td className="px-3 py-2 text-xs font-mono" style={{ color: 'var(--foreground-tertiary)' }}>{log.ip_address || '-'}</td>
                    <td className="px-3 py-2">
                      {log.success ? (
                        <span className="flex items-center gap-1 text-xs" style={{ color: 'var(--state-success)' }}>
                          <CheckCircle2 className="w-3.5 h-3.5" /> 成功
                        </span>
                      ) : (
                        <span className="flex items-center gap-1 text-xs" style={{ color: 'var(--state-error)' }} title={log.error_message || ''}>
                          <XCircle className="w-3.5 h-3.5" /> 失败
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-xs whitespace-nowrap" style={{ color: 'var(--foreground-tertiary)' }}>{formatTime(log.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* 分页 */}
        {!loading && items.length > 0 && (
          <div className="flex items-center justify-between px-3 py-2 border-t text-xs" style={{ borderColor: 'var(--border)' }}>
            <span style={{ color: 'var(--foreground-tertiary)' }}>
              第 {page} / {totalPages} 页
            </span>
            <div className="flex items-center gap-1">
              <button
                onClick={() => setPage(p => Math.max(1, p - 1))}
                disabled={page <= 1}
                className="p-1.5 rounded disabled:opacity-40 hover:bg-[var(--background-muted)]"
                style={{ color: 'var(--foreground-tertiary)' }}
              >
                <ChevronLeft className="w-4 h-4" />
              </button>
              <button
                onClick={() => setPage(p => Math.min(totalPages, p + 1))}
                disabled={page >= totalPages}
                className="p-1.5 rounded disabled:opacity-40 hover:bg-[var(--background-muted)]"
                style={{ color: 'var(--foreground-tertiary)' }}
              >
                <ChevronRight className="w-4 h-4" />
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

// ============================================================================
// 主页面：Tab 切换
// ============================================================================
export default function Logs() {
  const [tab, setTab] = useState<Tab>('llm')

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold flex items-center gap-2" style={{ color: 'var(--foreground)' }}>
            <Activity className="w-5 h-5" style={{ color: 'var(--brand-500)' }} />
            日志查看
          </h1>
          <p className="text-xs mt-0.5" style={{ color: 'var(--foreground-tertiary)' }}>
            查看大模型调用日志和用户操作日志，便于验证 AI 返回结果是否正确
          </p>
        </div>
      </div>

      {/* Tab 切换 */}
      <div className="flex items-center gap-1 border-b" style={{ borderColor: 'var(--input)' }}>
        <button
          onClick={() => setTab('llm')}
          className="flex items-center gap-1.5 px-4 py-2 text-sm font-medium border-b-2 transition-colors"
          style={tab === 'llm'
            ? { borderColor: 'var(--brand-500)', color: 'var(--brand-500)' }
            : { borderColor: 'transparent', color: 'var(--foreground-tertiary)' }}
        >
          <Cpu className="w-4 h-4" />
          LLM 调用日志
        </button>
        <button
          onClick={() => setTab('operation')}
          className="flex items-center gap-1.5 px-4 py-2 text-sm font-medium border-b-2 transition-colors"
          style={tab === 'operation'
            ? { borderColor: 'var(--brand-500)', color: 'var(--brand-500)' }
            : { borderColor: 'transparent', color: 'var(--foreground-tertiary)' }}
        >
          <FileText className="w-4 h-4" />
          用户操作日志
        </button>
      </div>

      {/* 内容 */}
      {tab === 'llm' ? <LlmLogsTab /> : <OperationLogsTab />}
    </div>
  )
}
