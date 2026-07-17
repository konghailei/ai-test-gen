import { useState, useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  ListTodo, Loader2, CheckCircle2, XCircle, AlertCircle, RefreshCw,
  FileText, Brain, Database, MessageCircle, Sparkles, Share2, Ban,
} from 'lucide-react'
import { requirements as reqApi } from '../api/client'
import { useProduct } from '../store/ProductContext'
import type { AnalysisTaskInfo } from '../types'
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

export default function Tasks() {
  const { selectedProductId } = useProduct()
  const navigate = useNavigate()
  const [tasks, setTasks] = useState<AnalysisTaskInfo[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [filterStatus, setFilterStatus] = useState<string>('')
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

                  return (
                    <tr
                      key={task.id}
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
                      <td className="px-4 py-3 text-slate-500 text-xs">
                        {durationSec < 60 ? `${durationSec}s` : `${Math.floor(durationSec / 60)}m ${durationSec % 60}s`}
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
                          <span className="text-slate-300">—</span>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* 失败任务错误信息 */}
      {tasks.some(t => t.status === 'failed' && t.error) && (
        <div className="card p-4 bg-red-50 border-red-100">
          <h3 className="text-sm font-medium text-red-700 mb-2 flex items-center gap-1.5">
            <AlertCircle className="w-4 h-4" /> 失败任务详情
          </h3>
          <div className="space-y-2">
            {tasks.filter(t => t.status === 'failed' && t.error).map(t => (
              <div key={t.id} className="text-xs text-red-600 bg-white/50 rounded p-2">
                <span className="font-mono">{t.id}</span>: {t.error}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
