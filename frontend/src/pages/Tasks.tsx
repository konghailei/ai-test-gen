import { useState, useEffect, useRef, Fragment } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  ListTodo, Loader2, CheckCircle2, XCircle, AlertCircle, RefreshCw,
  FileText, Brain, Database, MessageCircle, Sparkles, Share2, Ban,
  Clock, ChevronDown, ChevronRight, X, Info,
} from 'lucide-react'
import { requirements as reqApi } from '../api/client'
import { useProduct } from '../store/ProductContext'
import type { AnalysisTaskInfo, PhaseTiming } from '../types'
import { formatDate } from '../utils'

const TASK_TYPE_LABELS: Record<string, { label: string; icon: any; color: string }> = {
  upload_requirement: { label: '需求解析', icon: Brain, color: 'primary' },
  submit_clarify: { label: '澄清提交', icon: MessageCircle, color: 'warning' },
  regenerate: { label: '重新生成', icon: Sparkles, color: 'success' },
  analyze_impact: { label: '影响分析', icon: Share2, color: 'violet' },
  generate_cases: { label: '用例生成', icon: FileText, color: 'accent' },
  merge_fp: { label: '功能点合并', icon: Share2, color: 'indigo' },
}

const STATUS_LABELS: Record<string, { label: string; color: string; icon: any }> = {
  pending: { label: '等待中', color: 'slate', icon: Loader2 },
  running: { label: '进行中', color: 'primary', icon: Loader2 },
  completed: { label: '已完成', color: 'success', icon: CheckCircle2 },
  failed: { label: '失败', color: 'red', icon: XCircle },
  interrupted: { label: '已中断', color: 'amber', icon: AlertCircle },
}

/** v2.24: 阶段标签映射（用于耗时明细展示） */
const PHASE_LABELS: Record<string, string> = {
  upload: '文件上传',
  parse: 'LLM 需求解析',
  kb_search: '知识库检索',
  clarify: '澄清分析',
  regenerate: '最终 SRR 生成',
  impact: '影响分析',
  generate: '用例生成',
  review: '质量审核',
  format: '格式化',
  done: '完成',
  '': '初始化',
}

/** v2.24: kb_search 子阶段标签（区分 KB 检索 vs LLM 调用） */
const KB_SUB_PHASE_LABELS: Record<string, string> = {
  kb_status_check_ms: 'KB 状态检查',
  kb_similar_search_ms: '相似需求检索',
  pg_enrich_ms: 'PG 关系增强',
  llm_kb_query_gen_ms: 'LLM 生成查询',
  kb_qa_ms: 'KB 问答查询',
  kb_apply_ms: 'KB 答案应用',
  llm_question_refine_ms: 'LLM 问题精炼',
}

/** v2.23: 格式化耗时 */
function formatDuration(ms: number): string {
  const sec = Math.round(ms / 1000)
  if (sec < 60) return `${sec}s`
  const min = Math.floor(sec / 60)
  const remSec = sec % 60
  if (min < 60) return `${min}m ${remSec}s`
  const hr = Math.floor(min / 60)
  return `${hr}h ${min % 60}m`
}

/** 将语义色名映射到 Apple 设计系统 CSS 变量（用于动态图标/文本着色） */
const COLOR_VAR_MAP: Record<string, string> = {
  primary: 'var(--brand-500)',
  warning: 'var(--state-warning)',
  success: 'var(--state-success)',
  violet: 'var(--brand-500)',
  accent: 'var(--brand-500)',
  indigo: 'var(--brand-500)',
  slate: 'var(--foreground-tertiary)',
  red: 'var(--state-error)',
  amber: 'var(--state-warning)',
}
const colorToVar = (c: string) => COLOR_VAR_MAP[c] || 'var(--foreground-tertiary)'

export default function Tasks() {
  const { selectedProductId } = useProduct()
  const navigate = useNavigate()
  const [tasks, setTasks] = useState<AnalysisTaskInfo[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [filterStatus, setFilterStatus] = useState<string>('')
  const [expandedTaskId, setExpandedTaskId] = useState<string | null>(null)  // v2.23: 展开耗时明细
  const [detailTask, setDetailTask] = useState<AnalysisTaskInfo | null>(null)  // v2.24: 任务详情弹窗
  const [serverNow, setServerNow] = useState<string | undefined>(undefined)  // v2.36: 服务器当前时间
  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null)

  const loadTasks = async () => {
    try {
      const res = await reqApi.listTasks({
        status: filterStatus || undefined,
        product_id: selectedProductId || undefined,
        limit: 100,
      })
      setTasks(res.items || [])
      setTotal(res.total || 0)
      setServerNow(res.server_now)
    } catch (e: any) {
      console.error('加载任务失败:', e?.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    setLoading(true)
    loadTasks()
    // 自动轮询（进行中任务实时刷新）
    if (pollTimer.current) clearInterval(pollTimer.current)
    pollTimer.current = setInterval(loadTasks, 3000)
    return () => {
      if (pollTimer.current) clearInterval(pollTimer.current)
    }
  }, [filterStatus, selectedProductId])

  const handleRowClick = (task: AnalysisTaskInfo) => {
    if (task.req_id) {
      navigate(`/requirements/${task.req_id}`)
    }
  }

  const handleCancel = async (e: React.MouseEvent, taskId: string) => {
    e.stopPropagation()
    if (!confirm('确定取消此任务？')) return
    try {
      await reqApi.cancelTask(taskId)
      loadTasks()
    } catch (err: any) {
      alert('取消失败: ' + (err?.message || '未知错误'))
    }
  }

  const statusFilters = ['', 'running', 'completed', 'failed', 'interrupted']
  const statusFilterLabels: Record<string, string> = {
    '': '全部',
    running: '进行中',
    completed: '已完成',
    failed: '失败',
    interrupted: '已中断',
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="page-title flex items-center gap-2">
            <ListTodo className="w-5 h-5" style={{ color: 'var(--brand-500)' }} /> 任务管理
          </h1>
          <p className="text-sm mt-1" style={{ color: 'var(--foreground-tertiary)' }}>
            查看所有分析任务的运行状态 — 共 {total} 个任务
          </p>
        </div>
        <button onClick={loadTasks} className="btn-secondary text-sm">
          <RefreshCw className="w-4 h-4" /> 刷新
        </button>
      </div>

      {/* 状态筛选 */}
      <div className="flex items-center gap-2 flex-wrap">
        {statusFilters.map(s => (
          <button
            key={s}
            onClick={() => setFilterStatus(s)}
            className="px-3 py-1.5 text-sm rounded-lg transition-all"
            style={filterStatus === s
              ? { background: 'var(--brand-500)', color: '#ffffff' }
              : { background: 'var(--background)', border: '1px solid var(--border)', color: 'var(--foreground-secondary)' }
            }
          >
            {statusFilterLabels[s]}
          </button>
        ))}
      </div>

      {/* 任务列表 */}
      <div className="card overflow-hidden">
        {loading ? (
          <div className="flex items-center justify-center py-16" style={{ color: 'var(--foreground-quaternary)' }}>
            <Loader2 className="w-5 h-5 animate-spin mr-2" /> 加载中...
          </div>
        ) : tasks.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-16" style={{ color: 'var(--foreground-quaternary)' }}>
            <ListTodo className="w-10 h-10 mb-3 opacity-30" />
            <p className="text-sm">暂无任务记录</p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead style={{ background: 'var(--background-muted)', borderBottom: '1px solid var(--border)' }}>
                <tr className="text-left text-xs uppercase tracking-wider" style={{ color: 'var(--foreground-tertiary)' }}>
                  <th className="px-4 py-3 font-semibold">任务类型</th>
                  <th className="px-4 py-3 font-semibold">关联需求</th>
                  <th className="px-4 py-3 font-semibold">状态</th>
                  <th className="px-4 py-3 font-semibold">进度</th>
                  <th className="px-4 py-3 font-semibold">创建时间</th>
                  <th className="px-4 py-3 font-semibold">耗时</th>
                  <th className="px-4 py-3 font-semibold">操作</th>
                </tr>
              </thead>
              <tbody>
                {tasks.map(task => {
                  const typeInfo = TASK_TYPE_LABELS[task.task_type] || { label: task.task_type, icon: FileText, color: 'slate' }
                  const statusInfo = STATUS_LABELS[task.status] || { label: task.status, color: 'slate', icon: AlertCircle }
                  const Icon = typeInfo.icon
                  const StatusIcon = statusInfo.icon
                  const isRunning = task.status === 'running' || task.status === 'pending'
                  // v2.36: 进行中的任务使用服务器时间，避免客户端时钟偏差导致负数
                  const nowMs = serverNow ? new Date(serverNow).getTime() : Date.now()
                  const duration = task.completed_at
                    ? new Date(task.completed_at).getTime() - new Date(task.created_at).getTime()
                    : nowMs - new Date(task.created_at).getTime()
                  const durationSec = Math.max(0, Math.round(duration / 1000))
                  // v2.23: 阶段耗时数据
                  const timings = task.phase_timings || {}
                  const hasTimings = Object.keys(timings).length > 0
                  const isExpanded = expandedTaskId === task.id

                  return (
                    <Fragment key={task.id}>
                      <tr
                        onClick={() => handleRowClick(task)}
                        className="cursor-pointer transition-colors"
                        style={{ borderBottom: '1px solid var(--background-muted)' }}
                      >
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-2">
                            <Icon className="w-4 h-4 flex-shrink-0" style={{ color: colorToVar(typeInfo.color) }} />
                            <span className="font-medium" style={{ color: 'var(--foreground-secondary)' }}>{typeInfo.label}</span>
                          </div>
                        </td>
                        <td className="px-4 py-3">
                          <div className="max-w-[280px]">
                            <p className="truncate" style={{ color: 'var(--foreground-secondary)' }}>{task.req_title || '(无标题)'}</p>
                            <p className="text-xs font-mono truncate" style={{ color: 'var(--foreground-quaternary)' }}>{task.req_id || '-'}</p>
                          </div>
                        </td>
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-1.5">
                            <StatusIcon className={`w-3.5 h-3.5 ${isRunning ? 'animate-spin' : ''}`} style={{ color: colorToVar(statusInfo.color) }} />
                            <span className="font-medium" style={{ color: colorToVar(statusInfo.color) }}>{statusInfo.label}</span>
                          </div>
                          {task.message && (
                            <p className="text-xs mt-0.5 truncate max-w-[200px]" style={{ color: 'var(--foreground-quaternary)' }}>{task.message}</p>
                          )}
                        </td>
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-2">
                            <div className="w-20 h-1.5 rounded-full overflow-hidden" style={{ background: 'var(--background-muted)' }}>
                              <div
                                className="h-full rounded-full transition-all"
                                style={{
                                  width: `${task.progress}%`,
                                  background: task.status === 'failed' ? 'var(--state-error)' : task.status === 'completed' ? 'var(--state-success)' : 'var(--brand-500)',
                                }}
                              />
                            </div>
                            <span className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>{task.progress}%</span>
                          </div>
                        </td>
                        <td className="px-4 py-3 text-xs" style={{ color: 'var(--foreground-tertiary)' }}>
                          {formatDate(task.created_at)}
                        </td>
                        <td className="px-4 py-3 text-xs">
                          {/* v2.23: 耗时可点击展开查看阶段明细 */}
                          <button
                            onClick={(e) => {
                              e.stopPropagation()
                              if (hasTimings) {
                                setExpandedTaskId(isExpanded ? null : task.id)
                              } else {
                                handleRowClick(task)
                              }
                            }}
                            className={`inline-flex items-center gap-1 ${hasTimings ? 'cursor-pointer' : 'cursor-default'}`}
                            style={{ color: hasTimings ? 'var(--foreground-secondary)' : 'var(--foreground-tertiary)' }}
                            title={hasTimings ? '点击查看阶段耗时明细' : '暂无阶段耗时数据'}
                          >
                            <Clock className="w-3 h-3" style={{ color: 'var(--foreground-quaternary)' }} />
                            {durationSec < 60 ? `${durationSec}s` : `${Math.floor(durationSec / 60)}m ${durationSec % 60}s`}
                            {hasTimings && (
                              isExpanded
                                ? <ChevronDown className="w-3 h-3" style={{ color: 'var(--foreground-quaternary)' }} />
                                : <ChevronRight className="w-3 h-3" style={{ color: 'var(--foreground-quaternary)' }} />
                            )}
                          </button>
                        </td>
                        <td className="px-4 py-3">
                          {isRunning ? (
                            <button
                              onClick={(e) => handleCancel(e, task.id)}
                              className="p-1 rounded transition-colors"
                              style={{ color: 'var(--foreground-quaternary)' }}
                              title="取消任务"
                            >
                              <Ban className="w-3.5 h-3.5" />
                            </button>
                          ) : (
                            <button
                              onClick={(e) => { e.stopPropagation(); setDetailTask(task) }}
                              className="inline-flex items-center gap-1 px-2 py-1 text-xs rounded transition-colors"
                              style={task.status === 'failed'
                                ? { color: 'var(--state-error)' }
                                : { color: 'var(--foreground-tertiary)' }
                              }
                              title="查看任务详情"
                            >
                              <Info className="w-3.5 h-3.5" />
                              详情
                            </button>
                          )}
                        </td>
                      </tr>
                      {/* v2.23: 阶段耗时明细展开行 */}
                      {isExpanded && hasTimings && (
                        <tr style={{ background: 'rgba(242, 242, 247, 0.5)' }}>
                          <td colSpan={7} className="px-4 py-3">
                            <div className="ml-8 max-w-2xl">
                              <div className="flex items-center gap-1.5 mb-2 text-xs font-medium" style={{ color: 'var(--foreground-secondary)' }}>
                                <Clock className="w-3.5 h-3.5" style={{ color: 'var(--brand-500)' }} />
                                阶段耗时明细
                              </div>
                              <div className="space-y-1">
                                {Object.entries(timings).map(([phaseKey, t]: [string, any]) => {
                                  if (!t?.start) return null
                                  const isPhaseDone = !!t.end
                                  // v2.36: 进行中的阶段使用服务器时间
                                  const elapsed = t.end
                                    ? t.duration_ms || (new Date(t.end).getTime() - new Date(t.start).getTime())
                                    : nowMs - new Date(t.start).getTime()
                                  // 计算占总耗时的百分比
                                  const totalMs = Object.values(timings).reduce((sum, pt) => {
                                    if (!pt?.start) return sum
                                    const ptElapsed = pt.end
                                      ? pt.duration_ms || (new Date(pt.end).getTime() - new Date(pt.start).getTime())
                                      : 0
                                    return sum + ptElapsed
                                  }, 0)
                                  const pct = totalMs > 0 ? Math.round((elapsed / totalMs) * 100) : 0
                                  const label = PHASE_LABELS[phaseKey] || phaseKey
                                  // v2.24: kb_search 子阶段分解
                                  const subBreakdown = t.sub_breakdown as Record<string, number> | undefined
                                  return (
                                    <div key={phaseKey}>
                                      <div className="flex items-center gap-3 text-xs">
                                        <span className="w-24 flex-shrink-0 flex items-center gap-1" style={{ color: isPhaseDone ? 'var(--foreground-secondary)' : 'var(--brand-500)', fontWeight: isPhaseDone ? 400 : 500 }}>
                                          {isPhaseDone
                                            ? <CheckCircle2 className="w-3 h-3" style={{ color: 'var(--state-success)' }} />
                                            : <Loader2 className="w-3 h-3 animate-spin" />}
                                          {label}
                                        </span>
                                        <div className="flex-1 h-1.5 rounded-full overflow-hidden" style={{ background: 'var(--background-muted)' }}>
                                          <div
                                            className="h-full rounded-full"
                                            style={{ width: `${Math.max(pct, 3)}%`, background: isPhaseDone ? 'var(--state-success)' : 'var(--brand-400)' }}
                                          />
                                        </div>
                                        <span className="w-16 text-right font-mono" style={{ color: isPhaseDone ? 'var(--foreground-tertiary)' : 'var(--brand-500)' }}>
                                          {formatDuration(elapsed)}
                                        </span>
                                        <span className="w-10 text-right" style={{ color: 'var(--foreground-quaternary)' }}>{pct}%</span>
                                      </div>
                                      {/* v2.24: kb_search 子阶段明细 — 区分 KB 检索 vs LLM 调用 */}
                                      {subBreakdown && (
                                        <div className="ml-28 mt-0.5 mb-1 space-y-0.5">
                                          {Object.entries(subBreakdown).map(([subKey, subMs]) => {
                                            if (!subMs || subMs < 1) return null
                                            const isLLM = subKey.startsWith('llm_')
                                            const subLabel = KB_SUB_PHASE_LABELS[subKey] || subKey
                                            const subPct = elapsed > 0 ? Math.round((subMs / elapsed) * 100) : 0
                                            return (
                                              <div key={subKey} className="flex items-center gap-2 text-[11px]">
                                                <span className="w-20 flex-shrink-0" style={{ color: isLLM ? 'var(--state-warning)' : 'var(--foreground-quaternary)' }}>
                                                  {isLLM && '🤖 '}{subLabel}
                                                </span>
                                                <div className="flex-1 h-1 rounded-full overflow-hidden" style={{ background: 'var(--background-muted)' }}>
                                                  <div
                                                    className="h-full rounded-full"
                                                    style={{ width: `${Math.max(subPct, 2)}%`, background: isLLM ? 'var(--state-warning)' : 'var(--foreground-quaternary)' }}
                                                  />
                                                </div>
                                                <span className="w-12 text-right font-mono" style={{ color: isLLM ? 'var(--state-warning)' : 'var(--foreground-quaternary)' }}>
                                                  {formatDuration(subMs)}
                                                </span>
                                              </div>
                                            )
                                          })}
                                        </div>
                                      )}
                                    </div>
                                  )
                                })}
                              </div>
                              {/* 总耗时汇总 */}
                              <div className="flex items-center justify-between mt-2 pt-2 border-t text-xs" style={{ borderColor: 'var(--border)' }}>
                                <span style={{ color: 'var(--foreground-tertiary)' }}>
                                  创建: {formatDate(task.created_at)}
                                  {task.completed_at && ` | 完成: ${formatDate(task.completed_at)}`}
                                </span>
                                <span className="font-medium" style={{ color: 'var(--foreground-secondary)' }}>
                                  总耗时: {formatDuration(duration)}
                                </span>
                              </div>
                            </div>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* v2.24: 任务详情弹窗（替代原列表下方错误卡片） */}
      {detailTask && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center"
          style={{ background: 'rgba(0, 0, 0, 0.4)' }}
          onClick={() => setDetailTask(null)}
        >
          <div
            className="max-w-lg w-full mx-4 max-h-[80vh] overflow-hidden flex flex-col"
            style={{ background: 'var(--background)', borderRadius: 'calc(var(--radius) * 1.1)', boxShadow: 'var(--shadow-apple-xl)' }}
            onClick={e => e.stopPropagation()}
          >
            {/* 弹窗头部 */}
            <div className="flex items-center justify-between px-5 py-4 border-b" style={{ borderColor: 'var(--border)' }}>
              <h3 className="text-sm font-semibold flex items-center gap-2" style={{ color: 'var(--foreground)' }}>
                <Info className="w-4 h-4" style={{ color: 'var(--foreground-quaternary)' }} />
                任务详情
              </h3>
              <button
                onClick={() => setDetailTask(null)}
                className="p-1 rounded transition-colors"
                style={{ color: 'var(--foreground-quaternary)' }}
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            {/* 弹窗内容 */}
            <div className="px-5 py-4 overflow-y-auto space-y-3 text-sm">
              {/* 基本信息 */}
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <p className="text-xs mb-0.5" style={{ color: 'var(--foreground-quaternary)' }}>任务 ID</p>
                  <p className="font-mono text-xs break-all" style={{ color: 'var(--foreground-secondary)' }}>{detailTask.id}</p>
                </div>
                <div>
                  <p className="text-xs mb-0.5" style={{ color: 'var(--foreground-quaternary)' }}>任务类型</p>
                  <p style={{ color: 'var(--foreground-secondary)' }}>
                    {TASK_TYPE_LABELS[detailTask.task_type]?.label || detailTask.task_type}
                  </p>
                </div>
                <div>
                  <p className="text-xs mb-0.5" style={{ color: 'var(--foreground-quaternary)' }}>状态</p>
                  <div className="flex items-center gap-1.5">
                    {(() => {
                      const si = STATUS_LABELS[detailTask.status] || { label: detailTask.status, color: 'slate', icon: AlertCircle }
                      const SIcon = si.icon
                      return (
                        <>
                          <SIcon className="w-3.5 h-3.5" style={{ color: colorToVar(si.color) }} />
                          <span className="font-medium" style={{ color: colorToVar(si.color) }}>{si.label}</span>
                        </>
                      )
                    })()}
                  </div>
                </div>
                <div>
                  <p className="text-xs mb-0.5" style={{ color: 'var(--foreground-quaternary)' }}>进度</p>
                  <p style={{ color: 'var(--foreground-secondary)' }}>{detailTask.progress}%</p>
                </div>
                <div>
                  <p className="text-xs mb-0.5" style={{ color: 'var(--foreground-quaternary)' }}>创建时间</p>
                  <p className="text-xs" style={{ color: 'var(--foreground-secondary)' }}>{formatDate(detailTask.created_at)}</p>
                </div>
                <div>
                  <p className="text-xs mb-0.5" style={{ color: 'var(--foreground-quaternary)' }}>完成时间</p>
                  <p className="text-xs" style={{ color: 'var(--foreground-secondary)' }}>
                    {detailTask.completed_at ? formatDate(detailTask.completed_at) : '—'}
                  </p>
                </div>
              </div>

              {/* 关联需求 */}
              {detailTask.req_id && (
                <div>
                  <p className="text-xs mb-0.5" style={{ color: 'var(--foreground-quaternary)' }}>关联需求</p>
                  <button
                    onClick={() => {
                      navigate(`/requirements/${detailTask.req_id}`)
                      setDetailTask(null)
                    }}
                    className="text-xs font-medium"
                    style={{ color: 'var(--brand-500)' }}
                  >
                    {detailTask.req_title || detailTask.req_id}
                  </button>
                </div>
              )}

              {/* 消息 */}
              {detailTask.message && (
                <div>
                  <p className="text-xs mb-0.5" style={{ color: 'var(--foreground-quaternary)' }}>消息</p>
                  <p className="text-xs rounded p-2" style={{ color: 'var(--foreground-secondary)', background: 'var(--background-muted)' }}>{detailTask.message}</p>
                </div>
              )}

              {/* 错误信息（重点展示） */}
              {detailTask.error && (
                <div>
                  <p className="text-xs mb-0.5 flex items-center gap-1" style={{ color: 'var(--state-error)' }}>
                    <AlertCircle className="w-3 h-3" /> 错误信息
                  </p>
                  <div className="text-xs rounded p-3 max-h-48 overflow-y-auto break-all whitespace-pre-wrap"
                    style={{ color: 'var(--state-error)', background: 'var(--state-error-surface)', border: '1px solid var(--state-error)' }}>
                    {detailTask.error}
                  </div>
                </div>
              )}

              {/* 阶段耗时 */}
              {detailTask.phase_timings && Object.keys(detailTask.phase_timings).length > 0 && (
                <div>
                  <p className="text-xs mb-0.5" style={{ color: 'var(--foreground-quaternary)' }}>阶段耗时</p>
                  <div className="space-y-1">
                    {Object.entries(detailTask.phase_timings).map(([phaseKey, t]: [string, PhaseTiming]) => {
                      if (!t?.start) return null
                      const isDone = !!t.end
                      // v2.36: 进行中的阶段使用服务器时间
                      const detailNowMs = detailTask.server_now ? new Date(detailTask.server_now).getTime() : (serverNow ? new Date(serverNow).getTime() : Date.now())
                      const elapsed = t.end
                        ? t.duration_ms || (new Date(t.end).getTime() - new Date(t.start).getTime())
                        : detailNowMs - new Date(t.start).getTime()
                      return (
                        <div key={phaseKey} className="flex items-center justify-between text-xs">
                          <span style={{ color: 'var(--foreground-secondary)' }}>
                            {isDone ? '✓' : '⏳'} {PHASE_LABELS[phaseKey] || phaseKey}
                          </span>
                          <span className="font-mono" style={{ color: 'var(--foreground-tertiary)' }}>{formatDuration(elapsed)}</span>
                        </div>
                      )
                    })}
                  </div>
                </div>
              )}

              {/* v2.0：生成统计信息（从 result_json 提取） */}
              {detailTask.result_json && Object.keys(detailTask.result_json).length > 0 && (
                <div>
                  <p className="text-xs mb-0.5" style={{ color: 'var(--foreground-quaternary)' }}>生成统计</p>
                  <div className="rounded p-3 space-y-2" style={{ background: 'var(--background-muted)' }}>
                    {(() => {
                      const r: any = detailTask.result_json
                      return (
                        <>
                          {/* 用例数 + 类型分布 */}
                          {r.case_count !== undefined && (
                            <div className="flex items-center justify-between text-xs">
                              <span style={{ color: 'var(--foreground-secondary)' }}>用例总数</span>
                              <span className="font-mono font-medium" style={{ color: 'var(--foreground)' }}>{r.case_count}</span>
                            </div>
                          )}
                          {r.type_dist && typeof r.type_dist === 'object' && Object.keys(r.type_dist).length > 0 && (
                            <div className="flex items-start justify-between text-xs gap-2">
                              <span style={{ color: 'var(--foreground-secondary)' }}>类型分布</span>
                              <div className="flex flex-wrap gap-1 justify-end">
                                {Object.entries(r.type_dist).map(([t, n]: [string, any]) => (
                                  <span key={t} className="px-1.5 py-0.5 rounded text-xs" style={{ background: 'var(--brand-100)', color: 'var(--brand-700)' }}>
                                    {t}: {n}
                                  </span>
                                ))}
                              </div>
                            </div>
                          )}
                          {/* 审核结果 */}
                          {(r.review_passed !== undefined || r.review_blocked !== undefined) && (
                            <div className="flex items-center justify-between text-xs">
                              <span style={{ color: 'var(--foreground-secondary)' }}>审核结果</span>
                              <span className="font-mono">
                                <span style={{ color: 'var(--state-success)' }}>通过 {r.review_passed ?? 0}</span>
                                <span style={{ color: 'var(--foreground-quaternary)' }}> / </span>
                                <span style={{ color: 'var(--state-error)' }}>拦截 {r.review_blocked ?? 0}</span>
                              </span>
                            </div>
                          )}
                          {/* 平均置信度 */}
                          {r.avg_confidence !== undefined && (
                            <div className="flex items-center justify-between text-xs">
                              <span style={{ color: 'var(--foreground-secondary)' }}>平均置信度</span>
                              <span className="font-mono font-medium" style={{ color: 'var(--brand-500)' }}>
                                {((r.avg_confidence || 0) * 100).toFixed(1)}%
                              </span>
                            </div>
                          )}
                          {/* 一致性异常标记 */}
                          {r.consistency_inconsistent !== undefined && (
                            <div className="flex items-center justify-between text-xs">
                              <span style={{ color: 'var(--foreground-secondary)' }}>一致性</span>
                              <span style={{ color: r.consistency_inconsistent ? 'var(--state-error)' : 'var(--state-success)' }} className="font-medium">
                                {r.consistency_inconsistent ? '⚠ 存在异常' : '✓ 一致'}
                              </span>
                            </div>
                          )}
                          {/* 耗时 */}
                          {r.duration_ms !== undefined && r.duration_ms > 0 && (
                            <div className="flex items-center justify-between text-xs">
                              <span style={{ color: 'var(--foreground-secondary)' }}>总耗时</span>
                              <span className="font-mono" style={{ color: 'var(--foreground-tertiary)' }}>{formatDuration(r.duration_ms)}</span>
                            </div>
                          )}
                        </>
                      )
                    })()}
                  </div>
                </div>
              )}
            </div>

            {/* 弹窗底部 */}
            <div className="px-5 py-3 border-t flex justify-end gap-2" style={{ borderColor: 'var(--border)' }}>
              {detailTask.req_id && (
                <button
                  onClick={() => {
                    navigate(`/requirements/${detailTask.req_id}`)
                    setDetailTask(null)
                  }}
                  className="btn-secondary text-sm"
                >
                  查看需求
                </button>
              )}
              <button
                onClick={() => setDetailTask(null)}
                className="btn-secondary text-sm"
              >
                关闭
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
