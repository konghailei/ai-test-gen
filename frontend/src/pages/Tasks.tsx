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

export default function Tasks() {
  const { selectedProductId } = useProduct()
  const navigate = useNavigate()
  const [tasks, setTasks] = useState<AnalysisTaskInfo[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [filterStatus, setFilterStatus] = useState<string>('')
  const [expandedTaskId, setExpandedTaskId] = useState<string | null>(null)  // v2.23: 展开耗时明细
  const [detailTask, setDetailTask] = useState<AnalysisTaskInfo | null>(null)  // v2.24: 任务详情弹窗
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
            <ListTodo className="w-5 h-5 text-primary-500" /> 任务管理
          </h1>
          <p className="text-sm text-slate-500 mt-1">
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
            className={`px-3 py-1.5 text-sm rounded-lg transition-all ${
              filterStatus === s
                ? 'bg-primary-500 text-white'
                : 'bg-white border border-slate-200 text-slate-600 hover:bg-slate-50'
            }`}
          >
            {statusFilterLabels[s]}
          </button>
        ))}
      </div>

      {/* 任务列表 */}
      <div className="card overflow-hidden">
        {loading ? (
          <div className="flex items-center justify-center py-16 text-slate-400">
            <Loader2 className="w-5 h-5 animate-spin mr-2" /> 加载中...
          </div>
        ) : tasks.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-16 text-slate-400">
            <ListTodo className="w-10 h-10 mb-3 opacity-30" />
            <p className="text-sm">暂无任务记录</p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-slate-50 border-b border-slate-100">
                <tr className="text-left text-xs text-slate-500 uppercase tracking-wider">
                  <th className="px-4 py-3 font-semibold">任务类型</th>
                  <th className="px-4 py-3 font-semibold">关联需求</th>
                  <th className="px-4 py-3 font-semibold">状态</th>
                  <th className="px-4 py-3 font-semibold">进度</th>
                  <th className="px-4 py-3 font-semibold">创建时间</th>
                  <th className="px-4 py-3 font-semibold">耗时</th>
                  <th className="px-4 py-3 font-semibold">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-50">
                {tasks.map(task => {
                  const typeInfo = TASK_TYPE_LABELS[task.task_type] || { label: task.task_type, icon: FileText, color: 'slate' }
                  const statusInfo = STATUS_LABELS[task.status] || { label: task.status, color: 'slate', icon: AlertCircle }
                  const Icon = typeInfo.icon
                  const StatusIcon = statusInfo.icon
                  const isRunning = task.status === 'running' || task.status === 'pending'
                  const duration = task.completed_at
                    ? new Date(task.completed_at).getTime() - new Date(task.created_at).getTime()
                    : Date.now() - new Date(task.created_at).getTime()
                  const durationSec = Math.round(duration / 1000)
                  // v2.23: 阶段耗时数据
                  const timings = task.phase_timings || {}
                  const hasTimings = Object.keys(timings).length > 0
                  const isExpanded = expandedTaskId === task.id

                  return (
                    <Fragment key={task.id}>
                      <tr
                        onClick={() => handleRowClick(task)}
                        className="hover:bg-slate-50 cursor-pointer transition-colors"
                      >
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-2">
                            <Icon className={`w-4 h-4 text-${typeInfo.color}-500 flex-shrink-0`} />
                            <span className="font-medium text-slate-700">{typeInfo.label}</span>
                          </div>
                        </td>
                        <td className="px-4 py-3">
                          <div className="max-w-[280px]">
                            <p className="text-slate-700 truncate">{task.req_title || '(无标题)'}</p>
                            <p className="text-xs text-slate-400 font-mono truncate">{task.req_id || '-'}</p>
                          </div>
                        </td>
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-1.5">
                            <StatusIcon className={`w-3.5 h-3.5 text-${statusInfo.color}-500 ${isRunning ? 'animate-spin' : ''}`} />
                            <span className={`text-${statusInfo.color}-600 font-medium`}>{statusInfo.label}</span>
                          </div>
                          {task.message && (
                            <p className="text-xs text-slate-400 mt-0.5 truncate max-w-[200px]">{task.message}</p>
                          )}
                        </td>
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-2">
                            <div className="w-20 h-1.5 bg-slate-100 rounded-full overflow-hidden">
                              <div
                                className={`h-full rounded-full transition-all ${
                                  task.status === 'failed' ? 'bg-red-400' :
                                  task.status === 'completed' ? 'bg-success-500' :
                                  'bg-primary-500'
                                }`}
                                style={{ width: `${task.progress}%` }}
                              />
                            </div>
                            <span className="text-xs text-slate-500">{task.progress}%</span>
                          </div>
                        </td>
                        <td className="px-4 py-3 text-slate-500 text-xs">
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
                            className={`inline-flex items-center gap-1 ${hasTimings ? 'text-slate-600 hover:text-indigo-600 cursor-pointer' : 'text-slate-500 cursor-default'}`}
                            title={hasTimings ? '点击查看阶段耗时明细' : '暂无阶段耗时数据'}
                          >
                            <Clock className="w-3 h-3 text-slate-400" />
                            {durationSec < 60 ? `${durationSec}s` : `${Math.floor(durationSec / 60)}m ${durationSec % 60}s`}
                            {hasTimings && (
                              isExpanded
                                ? <ChevronDown className="w-3 h-3 text-slate-400" />
                                : <ChevronRight className="w-3 h-3 text-slate-400" />
                            )}
                          </button>
                        </td>
                        <td className="px-4 py-3">
                          {isRunning ? (
                            <button
                              onClick={(e) => handleCancel(e, task.id)}
                              className="p-1 text-slate-400 hover:text-red-500 rounded transition-colors"
                              title="取消任务"
                            >
                              <Ban className="w-3.5 h-3.5" />
                            </button>
                          ) : (
                            <button
                              onClick={(e) => { e.stopPropagation(); setDetailTask(task) }}
                              className={`inline-flex items-center gap-1 px-2 py-1 text-xs rounded transition-colors ${
                                task.status === 'failed'
                                  ? 'text-red-600 hover:bg-red-50'
                                  : 'text-slate-500 hover:bg-slate-100'
                              }`}
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
                        <tr className="bg-slate-50/50">
                          <td colSpan={7} className="px-4 py-3">
                            <div className="ml-8 max-w-2xl">
                              <div className="flex items-center gap-1.5 mb-2 text-xs font-medium text-slate-600">
                                <Clock className="w-3.5 h-3.5 text-indigo-500" />
                                阶段耗时明细
                              </div>
                              <div className="space-y-1">
                                {Object.entries(timings).map(([phaseKey, t]: [string, any]) => {
                                  if (!t?.start) return null
                                  const isPhaseDone = !!t.end
                                  const elapsed = t.end
                                    ? t.duration_ms || (new Date(t.end).getTime() - new Date(t.start).getTime())
                                    : Date.now() - new Date(t.start).getTime()
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
                                        <span className={`w-24 flex-shrink-0 flex items-center gap-1 ${isPhaseDone ? 'text-slate-600' : 'text-indigo-600 font-medium'}`}>
                                          {isPhaseDone
                                            ? <CheckCircle2 className="w-3 h-3 text-emerald-500" />
                                            : <Loader2 className="w-3 h-3 animate-spin" />}
                                          {label}
                                        </span>
                                        <div className="flex-1 h-1.5 bg-slate-100 rounded-full overflow-hidden">
                                          <div
                                            className={`h-full rounded-full ${isPhaseDone ? 'bg-emerald-400' : 'bg-indigo-400'}`}
                                            style={{ width: `${Math.max(pct, 3)}%` }}
                                          />
                                        </div>
                                        <span className={`w-16 text-right font-mono ${isPhaseDone ? 'text-slate-500' : 'text-indigo-600'}`}>
                                          {formatDuration(elapsed)}
                                        </span>
                                        <span className="w-10 text-right text-slate-400">{pct}%</span>
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
                                                <span className={`w-20 flex-shrink-0 ${isLLM ? 'text-amber-600' : 'text-slate-400'}`}>
                                                  {isLLM && '🤖 '}{subLabel}
                                                </span>
                                                <div className="flex-1 h-1 bg-slate-100 rounded-full overflow-hidden">
                                                  <div
                                                    className={`h-full rounded-full ${isLLM ? 'bg-amber-300' : 'bg-slate-300'}`}
                                                    style={{ width: `${Math.max(subPct, 2)}%` }}
                                                  />
                                                </div>
                                                <span className={`w-12 text-right font-mono ${isLLM ? 'text-amber-600' : 'text-slate-400'}`}>
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
                              <div className="flex items-center justify-between mt-2 pt-2 border-t border-slate-200 text-xs">
                                <span className="text-slate-500">
                                  创建: {formatDate(task.created_at)}
                                  {task.completed_at && ` | 完成: ${formatDate(task.completed_at)}`}
                                </span>
                                <span className="font-medium text-slate-700">
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
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
          onClick={() => setDetailTask(null)}
        >
          <div
            className="bg-white rounded-2xl shadow-xl max-w-lg w-full mx-4 max-h-[80vh] overflow-hidden flex flex-col"
            onClick={e => e.stopPropagation()}
          >
            {/* 弹窗头部 */}
            <div className="flex items-center justify-between px-5 py-4 border-b border-slate-100">
              <h3 className="text-sm font-semibold text-slate-800 flex items-center gap-2">
                <Info className="w-4 h-4 text-slate-400" />
                任务详情
              </h3>
              <button
                onClick={() => setDetailTask(null)}
                className="p-1 text-slate-400 hover:text-slate-600 hover:bg-slate-100 rounded transition-colors"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            {/* 弹窗内容 */}
            <div className="px-5 py-4 overflow-y-auto space-y-3 text-sm">
              {/* 基本信息 */}
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <p className="text-xs text-slate-400 mb-0.5">任务 ID</p>
                  <p className="font-mono text-xs text-slate-600 break-all">{detailTask.id}</p>
                </div>
                <div>
                  <p className="text-xs text-slate-400 mb-0.5">任务类型</p>
                  <p className="text-slate-600">
                    {TASK_TYPE_LABELS[detailTask.task_type]?.label || detailTask.task_type}
                  </p>
                </div>
                <div>
                  <p className="text-xs text-slate-400 mb-0.5">状态</p>
                  <div className="flex items-center gap-1.5">
                    {(() => {
                      const si = STATUS_LABELS[detailTask.status] || { label: detailTask.status, color: 'slate', icon: AlertCircle }
                      const SIcon = si.icon
                      return (
                        <>
                          <SIcon className={`w-3.5 h-3.5 text-${si.color}-500`} />
                          <span className={`text-${si.color}-600 font-medium`}>{si.label}</span>
                        </>
                      )
                    })()}
                  </div>
                </div>
                <div>
                  <p className="text-xs text-slate-400 mb-0.5">进度</p>
                  <p className="text-slate-600">{detailTask.progress}%</p>
                </div>
                <div>
                  <p className="text-xs text-slate-400 mb-0.5">创建时间</p>
                  <p className="text-slate-600 text-xs">{formatDate(detailTask.created_at)}</p>
                </div>
                <div>
                  <p className="text-xs text-slate-400 mb-0.5">完成时间</p>
                  <p className="text-slate-600 text-xs">
                    {detailTask.completed_at ? formatDate(detailTask.completed_at) : '—'}
                  </p>
                </div>
              </div>

              {/* 关联需求 */}
              {detailTask.req_id && (
                <div>
                  <p className="text-xs text-slate-400 mb-0.5">关联需求</p>
                  <button
                    onClick={() => {
                      navigate(`/requirements/${detailTask.req_id}`)
                      setDetailTask(null)
                    }}
                    className="text-primary-600 hover:text-primary-700 text-xs font-medium"
                  >
                    {detailTask.req_title || detailTask.req_id}
                  </button>
                </div>
              )}

              {/* 消息 */}
              {detailTask.message && (
                <div>
                  <p className="text-xs text-slate-400 mb-0.5">消息</p>
                  <p className="text-slate-600 text-xs bg-slate-50 rounded p-2">{detailTask.message}</p>
                </div>
              )}

              {/* 错误信息（重点展示） */}
              {detailTask.error && (
                <div>
                  <p className="text-xs text-red-400 mb-0.5 flex items-center gap-1">
                    <AlertCircle className="w-3 h-3" /> 错误信息
                  </p>
                  <div className="text-xs text-red-600 bg-red-50 border border-red-100 rounded p-3 max-h-48 overflow-y-auto break-all whitespace-pre-wrap">
                    {detailTask.error}
                  </div>
                </div>
              )}

              {/* 阶段耗时 */}
              {detailTask.phase_timings && Object.keys(detailTask.phase_timings).length > 0 && (
                <div>
                  <p className="text-xs text-slate-400 mb-0.5">阶段耗时</p>
                  <div className="space-y-1">
                    {Object.entries(detailTask.phase_timings).map(([phaseKey, t]: [string, PhaseTiming]) => {
                      if (!t?.start) return null
                      const isDone = !!t.end
                      const elapsed = t.end
                        ? t.duration_ms || (new Date(t.end).getTime() - new Date(t.start).getTime())
                        : Date.now() - new Date(t.start).getTime()
                      return (
                        <div key={phaseKey} className="flex items-center justify-between text-xs">
                          <span className="text-slate-600">
                            {isDone ? '✓' : '⏳'} {PHASE_LABELS[phaseKey] || phaseKey}
                          </span>
                          <span className="font-mono text-slate-500">{formatDuration(elapsed)}</span>
                        </div>
                      )
                    })}
                  </div>
                </div>
              )}
            </div>

            {/* 弹窗底部 */}
            <div className="px-5 py-3 border-t border-slate-100 flex justify-end gap-2">
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
