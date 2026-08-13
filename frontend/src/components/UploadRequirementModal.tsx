/**
 * 上传需求弹窗组件
 *
 * 从原 Requirements.tsx 抽取的上传需求完整逻辑，包含：
 *  - 上传表单（文件选择、需求标题、项目/模块选择）
 *  - 上传进度展示（多阶段进度条 + 任务轮询）
 *  - 上传后的澄清问题面板
 *  - 影响分析结果展示
 *
 * 该组件为受控组件，由父组件通过 `open` 控制显隐。
 */
import { useState, useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Upload, Loader2, CheckCircle2, XCircle, AlertCircle, AlertTriangle,
  FileUp, Brain, Database, Sparkles, RefreshCw, MessageCircle, Send,
  Share2, X, Minimize2, Clock,
} from 'lucide-react'
import { requirements as reqApi, products as prodApi } from '../api/client'
import { useProduct } from '../store/ProductContext'
import type { ClarifyQuestion, Module, AnalysisTaskInfo, PhaseTiming } from '../types'

/** 上传进度步骤定义（v2.4 影响分析合并） */
const UPLOAD_PHASES = [
  { key: 'parse', label: 'LLM 分析整理', icon: Brain, color: 'primary' },
  { key: 'kb_search', label: '知识库检索补充', icon: Database, color: 'accent' },
  { key: 'clarify', label: '人工填写补充', icon: MessageCircle, color: 'warning' },
  { key: 'regenerate', label: '重新生成分析', icon: Sparkles, color: 'success' },
  { key: 'impact', label: '影响分析', icon: Share2, color: 'violet' },
]

type UploadProgress = {
  phase: number          // 当前阶段索引（0-4）
  message: string       // 当前状态描述
  error: string | null  // 错误信息（不为空时表示出错）
  progress: number      // 真实进度 0-100（v2.14 来自后端任务轮询）
  taskId?: string       // v2.23: 关联的任务 ID（用于最小化后继续轮询）
  timings?: Record<string, PhaseTiming>  // v2.23: 阶段耗时数据
  serverNow?: string    // v2.36: 服务器当前时间（UTC ISO），避免客户端时钟偏差
}

export interface UploadRequirementModalProps {
  /** 是否显示弹窗 */
  open: boolean
  /** 关闭弹窗回调 */
  onClose: () => void
  /** 当前产品 ID */
  productId: string
  /** 从项目管理跳转时预选的项目 ID */
  preselectedProjectId?: string
  /** 上传成功后回调（用于刷新需求列表） */
  onUploaded?: (reqId: string) => void
}

function sleep(ms: number) { return new Promise(r => setTimeout(r, ms)) }

export default function UploadRequirementModal({
  open, onClose, productId, preselectedProjectId, onUploaded,
}: UploadRequirementModalProps) {
  const { products } = useProduct()
  const navigate = useNavigate()

  // 项目列表缓存（按产品 ID 分组）
  const [projects, setProjects] = useState<Record<string, { id: string; name: string }[]>>({})
  const [selProject, setSelProject] = useState('')
  const [uploadFiles, setUploadFiles] = useState<File[]>([])
  const [fileUploading, setFileUploading] = useState(false)

  // 上传标题（手动输入，不再由 LLM 生成）
  const [reqTitle, setReqTitle] = useState('')

  // 上传模块选择
  const [selModules, setSelModules] = useState<string[]>([])
  const [modules, setModules] = useState<Module[]>([])

  // 上传后的澄清问题（v2.3 新增）
  const [uploadResult, setUploadResult] = useState<{
    req_id: string
    questions: ClarifyQuestion[]
    answers: Record<string, string>
  } | null>(null)
  const [submittingClarify, setSubmittingClarify] = useState(false)

  // 上传后的影响分析结果（v2.35: 移除 affected_requirements，新增 affected_fps）
  const [impactResult, setImpactResult] = useState<{
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
  } | null>(null)

  // 上传进度
  const [progress, setProgress] = useState<UploadProgress | null>(null)
  const progressTimer = useRef<ReturnType<typeof setInterval> | null>(null)
  const taskPollingRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // 弹窗打开时：加载项目/模块，设置预选项目
  useEffect(() => {
    if (!open) return
    // 初始化预选项目
    setSelProject(preselectedProjectId || '')
    if (productId) {
      loadProjects(productId)
    } else {
      setModules([])
      setSelModules([])
    }
  }, [open, productId, preselectedProjectId])

  // 卸载时清理定时器
  useEffect(() => {
    return () => {
      if (progressTimer.current) clearInterval(progressTimer.current)
      if (taskPollingRef.current) clearInterval(taskPollingRef.current)
    }
  }, [])

  const loadProjects = async (pid: string) => {
    if (pid && !projects[pid]) {
      try {
        const projs = await prodApi.listProjects(pid)
        setProjects(p => ({ ...p, [pid]: projs || [] }))
      } catch {
        setProjects(p => ({ ...p, [pid]: [] }))
      }
    }
    // 加载模块
    if (pid) {
      prodApi.listModules(pid).then(mods => setModules(mods || [])).catch(() => setModules([]))
    } else {
      setModules([])
    }
  }

  // ================================================================
  // 多文件上传（带进度反馈）
  // ================================================================
  const handleMultiFileUpload = async () => {
    if (uploadFiles.length === 0 || !productId) return

    // 校验需求名称必填
    if (!reqTitle.trim()) {
      setProgress({ phase: 0, message: '请输入需求名称', error: '需求名称为必填项，请填写后再上传', progress: 0 })
      return
    }

    // 校验项目和模块必选
    if (!selProject) {
      setProgress({ phase: 0, message: '请选择所属项目/版本', error: '所属项目/版本为必选项，请选择后再上传', progress: 0 })
      return
    }
    if (selModules.length === 0) {
      setProgress({ phase: 0, message: '请选择关联模块', error: '关联模块为必选项，请至少选择一个模块后再上传', progress: 0 })
      return
    }

    setProgress({ phase: 0, message: `正在上传 ${uploadFiles.length} 个文件...`, error: null, progress: 0 })
    setFileUploading(true)

    try {
      // v2.14: 异步模式——upload 立即返回 task_id，后台执行分析
      const res: any = await reqApi.uploadMultiFiles(uploadFiles, productId, reqTitle, selProject, selModules.join(','))
      const taskId = res?.data?.task_id
      const reqId = res?.data?.req_id

      if (!taskId || !reqId) {
        throw new Error(res?.message || '上传失败：未返回任务 ID')
      }

      // 开始轮询任务状态
      startTaskPolling(taskId, reqId)
    } catch (e: any) {
      stopProgressTimer()
      setProgress(prev => ({
        phase: prev?.phase || 0,
        message: '文件上传失败',
        error: e.message || '请求失败，请稍后重试',
        progress: prev?.progress || 0,
      }))
      setFileUploading(false)
    }
  }

  /** v2.14: 轮询任务状态，更新真实进度 */
  const startTaskPolling = (taskId: string, reqId: string) => {
    stopProgressTimer()
    if (taskPollingRef.current) clearInterval(taskPollingRef.current)

    const PHASE_MAP: Record<string, number> = {
      upload: 0, parse: 0, kb_search: 1, clarify: 2, regenerate: 3, impact: 4, done: 4,
    }

    const poll = async () => {
      try {
        const task: AnalysisTaskInfo = await reqApi.getTask(taskId)

        // 映射后端 phase 到前端 UPLOAD_PHASES 索引
        const phaseIdx = PHASE_MAP[task.phase] ?? 0
        const timings = task.phase_timings || {}
        // v2.36: 使用服务器时间计算耗时，避免客户端时钟偏差导致负数
        const serverNow = task.server_now

        if (task.status === 'running' || task.status === 'pending') {
          setProgress({
            phase: phaseIdx,
            message: task.message || '正在分析...',
            error: null,
            progress: task.progress,
            taskId,
            timings,
            serverNow,
          })
        } else if (task.status === 'completed') {
          // 任务完成
          if (taskPollingRef.current) { clearInterval(taskPollingRef.current); taskPollingRef.current = null }
          setFileUploading(false)

          const result = task.result_json || {}
          const questions: ClarifyQuestion[] = (result.questions as ClarifyQuestion[]) || []

          // v2.18: 降级警告——SRR 生成了但质量有折扣，提示用户
          if (result.degraded) {
            setProgress({
              phase: phaseIdx,
              message: '分析完成（降级）',
              error: `⚠️ ${result.degraded_reason || '最终 SRR 生成失败，已降级使用首次解析结果'}`,
              progress: 100,
              taskId,
              timings,
              serverNow,
            })
            await sleep(3000) // 让用户看到警告 3 秒
          }

          if (questions.length > 0) {
            // 有未回答问题：显示澄清面板
            setProgress({ phase: 2, message: '检测到需要补充的内容，请确认以下问题', error: null, progress: 100, taskId, timings, serverNow })
            await sleep(800)
            setProgress(null)
            const initAnswers: Record<string, string> = {}
            questions.forEach(q => { initAnswers[q.id] = '' })
            setUploadResult({ req_id: reqId, questions, answers: initAnswers })
          } else {
            // 无问题：进入影响分析阶段
            await runImpactAnalysis(reqId)
          }
        } else if (task.status === 'failed') {
          if (taskPollingRef.current) { clearInterval(taskPollingRef.current); taskPollingRef.current = null }
          setFileUploading(false)
          setProgress({
            phase: phaseIdx,
            message: '分析失败',
            error: task.error || '分析过程中发生错误',
            progress: task.progress,
            taskId,
            timings,
            serverNow,
          })
        } else if (task.status === 'interrupted') {
          if (taskPollingRef.current) { clearInterval(taskPollingRef.current); taskPollingRef.current = null }
          setFileUploading(false)
          setProgress({
            phase: phaseIdx,
            message: '任务已中断（服务重启）',
            error: task.error || '服务重启导致任务中断，请重新上传',
            progress: task.progress,
            taskId,
            timings,
            serverNow,
          })
        }
      } catch (e: any) {
        // 轮询失败，不中断，下次重试
        console.warn('任务轮询失败:', e?.message)
      }
    }

    // 立即轮询一次，然后每 1.5 秒轮询
    poll()
    taskPollingRef.current = setInterval(poll, 1500)
  }

  /** 运行影响分析（基于模块依赖图谱 + FP 依赖图谱 BFS 遍历） */
  const runImpactAnalysis = async (reqId: string) => {
    try {
      setProgress({ phase: 4, message: '正在进行影响分析（基于模块+FP 依赖图谱 BFS 遍历）...', error: null, progress: 90 })
      startProgressSimulation()

      const impactRes = await reqApi.impactAnalysis(reqId)

      stopProgressTimer()

      // 如果有间接影响或受影响功能点，展示确认面板
      const hasImpacts = (impactRes.indirect_modules?.length || 0) > 0 ||
                         (impactRes.affected_fps?.length || 0) > 0

      if (hasImpacts) {
        setProgress({ phase: 4, message: '影响分析完成，请确认以下影响范围', error: null, progress: 95 })
        await sleep(600)
        setProgress(null)
        setImpactResult(impactRes)
      } else {
        // 无影响，直接完成
        setProgress({ phase: 4, message: '影响分析完成（无影响范围），正在完成...', error: null, progress: 100 })
        await sleep(800)
        finishUpload(reqId)
      }
    } catch (e: any) {
      stopProgressTimer()
      setProgress({ phase: 4, message: '影响分析未完成（可稍后在需求详情页重新分析）', error: null, progress: 90 })
      await sleep(800)
      finishUpload(reqId)
    }
  }

  /** 完成上传流程：清理状态，通知父组件刷新，跳转到需求详情页 */
  const finishUpload = (reqId: string) => {
    resetState()
    onUploaded?.(reqId)
    onClose()
    // v2.8.66: 上传分析完成后跳转到需求详情页，而非停在项目管理
    navigate(`/requirements/${encodeURIComponent(reqId)}`)
  }

  /** 重置弹窗内部状态 */
  const resetState = () => {
    stopProgressTimer()
    setUploadFiles([])
    setReqTitle('')
    setSelModules([])
    setProgress(null)
    setImpactResult(null)
    setUploadResult(null)
    setFileUploading(false)
    setSubmittingClarify(false)
  }

  /** 提交上传后的澄清答案并执行影响分析 */
  const submitUploadClarify = async () => {
    if (!uploadResult) return
    setSubmittingClarify(true)
    try {
      await reqApi.submitClarify(uploadResult.req_id, uploadResult.answers)
      // 澄清提交后进入影响分析
      await runImpactAnalysis(uploadResult.req_id)
    } catch (e: any) {
      alert('提交澄清失败: ' + e.message)
    } finally {
      setSubmittingClarify(false)
    }
  }

  /** 更新上传后的单条澄清答案 */
  const updateClarifyAnswer = (qId: string, value: string) => {
    setUploadResult(prev => prev ? { ...prev, answers: { ...prev.answers, [qId]: value } } : prev)
  }

  const removeFile = (idx: number) => {
    setUploadFiles(prev => prev.filter((_, i) => i !== idx))
  }

  // ================================================================
  // 进度条模拟（影响分析阶段的辅助提示，主进度由 taskPolling 提供）
  // ================================================================
  const startProgressSimulation = () => {
    stopProgressTimer()
    let elapsed = 0
    progressTimer.current = setInterval(() => {
      elapsed += 1
      // 影响分析阶段（phase 4）的提示动画
      const dots = '.'.repeat(elapsed % 4)
      setProgress(prev => prev?.error ? prev : {
        phase: 4,
        message: `正在进行影响分析（基于模块依赖图谱 BFS 遍历）${dots}`,
        error: null,
        progress: prev?.progress ?? 90,
      })
    }, 1200)
  }

  const stopProgressTimer = () => {
    if (progressTimer.current) {
      clearInterval(progressTimer.current)
      progressTimer.current = null
    }
    if (taskPollingRef.current) {
      clearInterval(taskPollingRef.current)
      taskPollingRef.current = null
    }
  }

  if (!open) return null

  return (
    <>
      {/* ===== 上传弹窗 — Apple 风格 ===== */}
      <div className="fixed inset-0 z-50 flex items-center justify-center p-4"
        style={{ background: 'rgba(0, 0, 0, 0.32)', backdropFilter: 'blur(4px)' }}
        onClick={() => { if (!progress) onClose() }}>
        <div
          className="max-w-lg w-full max-h-[90vh] overflow-y-auto animate-slide-up"
          style={{
            background: 'var(--background)',
            borderRadius: 'calc(var(--radius) * 1.1)',
            boxShadow: 'var(--shadow-apple-xl)',
            padding: 24,
          }}
          onClick={e => e.stopPropagation()}>
          <div className="flex items-center justify-between mb-4 sticky top-0 pb-2 z-10"
            style={{ background: 'var(--background)' }}>
            <h2 className="m-0" style={{ fontSize: 18, fontWeight: 600, color: 'var(--foreground)' }}>
              {progress ? '正在上传需求' : uploadResult ? '请补充需求信息' : '上传需求'}
            </h2>
            {/* v2.24: 后台运行按钮 — 关闭弹窗并跳转到任务管理页面查看进度 */}
            {progress && !progress.error && (
              <button
                onClick={() => {
                  if (taskPollingRef.current) { clearInterval(taskPollingRef.current); taskPollingRef.current = null }
                  setFileUploading(false)
                  resetState()
                  onClose()
                  navigate('/tasks')
                }}
                className="btn-secondary text-xs"
                style={{ padding: '6px 12px' }}
                title="关闭弹窗，前往任务管理页面查看进度"
              >
                <Minimize2 className="w-3.5 h-3.5" /> 后台运行
              </button>
            )}
            {/* 非上传中显示关闭按钮 */}
            {!progress && (
              <button onClick={onClose} className="btn-ghost p-1" title="关闭">
                <X className="w-4 h-4" />
              </button>
            )}
          </div>

          {/* ===== 上传中：阶段化进度条 — Apple 风格 ===== */}
          {progress && (
            <div className="space-y-4 py-2">
              {/* 阶段指示器 */}
              <div className="flex items-center justify-between px-1">
                {UPLOAD_PHASES.map((phase, idx) => {
                  const isCurrent = idx === progress.phase
                  const isDone = idx < progress.phase
                  const isError = progress.error && idx === progress.phase
                  return (
                    <div key={phase.key} className="flex flex-col items-center gap-1.5 flex-1">
                      <div className="w-9 h-9 rounded-full flex items-center justify-center transition-all duration-300"
                        style={{
                          background: isDone ? 'var(--state-success-surface)' :
                            isError ? 'var(--state-error-surface)' :
                            isCurrent ? 'rgba(0, 122, 255, 0.12)' :
                            'var(--background-muted)',
                          color: isDone ? 'var(--state-success)' :
                            isError ? 'var(--state-error)' :
                            isCurrent ? 'var(--brand-500)' :
                            'var(--foreground-quaternary)',
                          boxShadow: isCurrent ? '0 0 0 4px rgba(0, 122, 255, 0.1)' : 'none',
                        }}>
                        {isDone ? <CheckCircle2 className="w-5 h-5" /> :
                         isError ? <XCircle className="w-5 h-5" /> :
                         isCurrent ? <Loader2 className="w-5 h-5 animate-spin" /> :
                         <phase.icon className="w-5 h-5" />}
                      </div>
                      <span className="text-[10px] font-medium text-center leading-tight max-w-[64px]"
                        style={{
                          color: isCurrent ? 'var(--brand-500)' :
                            isDone ? 'var(--state-success)' :
                            'var(--foreground-tertiary)'
                        }}>{phase.label}</span>
                    </div>
                  )
                })}
              </div>

              {/* 连接线 */}
              <div className="relative h-0.5 -mt-2 mx-6" style={{ background: 'var(--border)' }}>
                <div className="absolute left-0 top-0 h-full transition-all duration-700 rounded-full"
                  style={{
                    width: `${(progress.phase / (UPLOAD_PHASES.length - 1)) * 100}%`,
                    background: 'linear-gradient(90deg, var(--brand-500), var(--state-success))',
                  }} />
              </div>

              {/* 当前阶段状态消息 */}
              <div className="text-center">
                <div className="inline-flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm"
                  style={{ color: 'var(--foreground-secondary)' }}>
                  <span className="text-xs font-mono" style={{ color: 'var(--foreground-tertiary)' }}>
                    阶段 {progress.phase + 1}/{UPLOAD_PHASES.length}
                  </span>
                  <span>{progress.message}</span>
                  {/* v2.23: 当前阶段已用时间 */}
                  {progress.timings && progress.taskId && (() => {
                    const currentPhaseKey = UPLOAD_PHASES[progress.phase]?.key
                    const t = currentPhaseKey ? progress.timings[currentPhaseKey] : undefined
                    if (!t?.start) return null
                    // v2.36: 优先使用服务器时间，避免客户端时钟偏差导致负数
                    const nowMs = progress.serverNow ? new Date(progress.serverNow).getTime() : Date.now()
                    const elapsed = (t.end ? new Date(t.end).getTime() : nowMs) - new Date(t.start).getTime()
                    const sec = Math.max(0, Math.round(elapsed / 1000))
                    return (
                      <span className="inline-flex items-center gap-0.5 text-xs" style={{ color: 'var(--foreground-tertiary)' }}>
                        <Clock className="w-3 h-3" />
                        {sec < 60 ? `${sec}s` : `${Math.floor(sec / 60)}m${sec % 60}s`}
                      </span>
                    )
                  })()}
                </div>
              </div>

              {/* v2.23: 已完成阶段的耗时明细 */}
              {progress.timings && Object.keys(progress.timings).length > 0 && (
                <div className="space-y-1.5 p-3" style={{ background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.5)' }}>
                  <p className="text-xs font-medium flex items-center gap-1 m-0" style={{ color: 'var(--foreground-tertiary)' }}>
                    <Clock className="w-3 h-3" /> 阶段耗时明细
                  </p>
                  {UPLOAD_PHASES.map(phase => {
                    const t = progress.timings?.[phase.key]
                    if (!t?.start) return null
                    const isDone = !!t.end
                    // v2.36: 进行中的阶段优先使用服务器时间，避免客户端时钟偏差导致负数
                    const nowMs = progress.serverNow ? new Date(progress.serverNow).getTime() : Date.now()
                    const elapsed = t.end
                      ? t.duration_ms || (new Date(t.end).getTime() - new Date(t.start).getTime())
                      : nowMs - new Date(t.start).getTime()
                    const sec = Math.max(0, Math.round(elapsed / 1000))
                    const timeStr = sec < 60 ? `${sec}s` : `${Math.floor(sec / 60)}m${sec % 60}s`
                    return (
                      <div key={phase.key} className="flex items-center justify-between text-xs">
                        <span className="flex items-center gap-1.5"
                          style={{ color: isDone ? 'var(--foreground-tertiary)' : 'var(--brand-500)', fontWeight: isDone ? 400 : 500 }}>
                          {isDone ? <CheckCircle2 className="w-3 h-3" style={{ color: 'var(--state-success)' }} /> : <Loader2 className="w-3 h-3 animate-spin" />}
                          {phase.label}
                        </span>
                        <span className="font-mono" style={{ color: isDone ? 'var(--foreground-tertiary)' : 'var(--brand-500)' }}>
                          {timeStr}
                        </span>
                      </div>
                    )
                  })}
                </div>
              )}

              {/* 错误信息 / 降级警告 */}
              {progress.error && (
                <div className="p-4"
                  style={{
                    borderRadius: 'calc(var(--radius) * 0.6)',
                    background: progress.message?.includes('降级') ? 'var(--state-warning-surface)' : 'var(--state-error-surface)',
                    border: `1px solid ${progress.message?.includes('降级') ? '#ffe0b3' : 'rgba(255, 59, 48, 0.2)'}`,
                  }}>
                  <div className="flex items-start gap-3">
                    {progress.message?.includes('降级')
                      ? <AlertTriangle className="w-5 h-5 flex-shrink-0 mt-0.5" style={{ color: 'var(--state-warning)' }} />
                      : <AlertCircle className="w-5 h-5 flex-shrink-0 mt-0.5" style={{ color: 'var(--state-error)' }} />
                    }
                    <div className="min-w-0">
                      <p className="text-sm font-semibold mb-1"
                        style={{ color: progress.message?.includes('降级') ? 'var(--state-warning)' : 'var(--state-error)' }}>
                        {progress.message?.includes('降级') ? '分析完成（降级警告）' : (progress.message || '分析失败')}
                      </p>
                      <pre className="text-xs whitespace-pre-wrap leading-relaxed font-sans m-0"
                        style={{ color: progress.message?.includes('降级') ? 'var(--state-warning)' : 'var(--state-error)', opacity: 0.85 }}>
                        {progress.error}
                      </pre>
                    </div>
                  </div>
                  <div className="mt-3 flex gap-2">
                    <button onClick={() => setProgress(null)} className="btn-secondary text-xs" style={{ padding: '6px 12px' }}>
                      返回修改
                    </button>
                    <button onClick={handleMultiFileUpload} className="btn-primary text-xs" style={{ padding: '6px 12px' }}>
                      <RefreshCw className="w-3 h-3" /> 重试
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}

          {/* ===== 非上传中：表单 — Apple 风格 ===== */}
          {!progress && !uploadResult && (
            <>
              {/* 选择项目和模块（产品已在顶部栏选中） */}
              <div className="space-y-3 mb-4">
                <div>
                  <label className="label">需求名称 <span style={{ color: 'var(--state-error)' }}>*</span></label>
                  <input
                    type="text"
                    value={reqTitle}
                    onChange={e => setReqTitle(e.target.value)}
                    placeholder="请输入需求名称（简洁描述需求内容）"
                    className="input-field text-sm"
                    autoFocus
                  />
                </div>
                <div>
                  <label className="label">当前产品</label>
                  <div className="text-sm font-medium"
                    style={{
                      color: 'var(--foreground)',
                      background: 'var(--background-muted)',
                      borderRadius: 'calc(var(--radius) * 0.4)',
                      padding: '8px 12px',
                    }}>
                    {productId
                      ? products.find(p => p.id === productId)?.name || productId
                      : '请先在顶部栏选择产品'}
                  </div>
                </div>
                <div>
                  <label className="label">所属项目/版本 <span style={{ color: 'var(--state-error)' }}>*</span></label>
                  <select value={selProject} onChange={e => setSelProject(e.target.value)}
                    className="select-field text-sm" disabled={!productId}>
                    <option value="">请选择项目...</option>
                    {(projects[productId] || []).map(p => (
                      <option key={p.id} value={p.id}>{p.name}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="label">关联模块 <span style={{ color: 'var(--state-error)' }}>*</span></label>
                  <div className="flex flex-wrap gap-1.5">
                    {modules.length === 0 ? (
                      <span className="text-xs py-1" style={{ color: 'var(--foreground-tertiary)' }}>暂无模块数据，请在「产品与项目」中创建</span>
                    ) : modules.map(m => (
                      <button key={m.id} onClick={() => {
                        setSelModules(prev => prev.includes(m.id) ? prev.filter(x => x !== m.id) : [...prev, m.id])
                      }}
                        className="px-3 py-1 rounded-lg text-xs font-medium border transition-all"
                        style={selModules.includes(m.id) ? {
                          background: 'rgba(0, 122, 255, 0.1)',
                          borderColor: 'var(--brand-500)',
                          color: 'var(--brand-500)',
                        } : {
                          background: 'var(--background)',
                          borderColor: 'var(--border)',
                          color: 'var(--foreground-tertiary)',
                        }}>
                        {m.name}
                      </button>
                    ))}
                  </div>
                </div>
              </div>

              {/* 多文件上传 */}
              <div className="mb-4 p-4 rounded-xl transition-colors"
                style={{ border: `2px dashed ${'var(--border)'}` }}>
                <label className="label">上传需求文档（支持多选，.md / .docx / .txt）</label>
                <input
                  type="file"
                  accept=".md,.docx,.txt,.markdown"
                  multiple
                  onChange={e => {
                    const selected = Array.from(e.target.files || [])
                    if (selected.length > 0) {
                      setUploadFiles(prev => [...prev, ...selected])
                    }
                  }}
                  className="block w-full text-sm"
                  style={{ color: 'var(--foreground-tertiary)' }} />
              </div>

              {/* 已选文件列表 */}
              {uploadFiles.length > 0 && (
                <div className="max-h-[180px] overflow-y-auto space-y-2 mb-4 pr-1">
                  <p className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>已选择 {uploadFiles.length} 个文件：</p>
                  {uploadFiles.map((f, i) => (
                    <div key={i} className="flex items-center justify-between p-2.5"
                      style={{ background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.35)' }}>
                      <span className="text-xs flex items-center gap-1.5" style={{ color: 'var(--foreground-secondary)' }}>
                        <FileUp className="w-3.5 h-3.5" style={{ color: 'var(--foreground-tertiary)' }} />
                        {f.name}
                        <span style={{ color: 'var(--foreground-tertiary)' }}>({(f.size / 1024).toFixed(1)} KB)</span>
                      </span>
                      <button onClick={() => removeFile(i)} className="p-1" style={{ color: 'var(--foreground-tertiary)' }} title="移除">
                        <XCircle className="w-4 h-4" />
                      </button>
                    </div>
                  ))}
                </div>
              )}

              <div className="flex items-center justify-end gap-3 mt-5">
                <button onClick={() => { resetState(); onClose() }} className="btn-secondary">取消</button>
                <button onClick={handleMultiFileUpload} disabled={uploadFiles.length === 0 || !productId || !reqTitle.trim() || fileUploading} className="btn-primary">
                  {fileUploading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Upload className="w-4 h-4" />}
                  上传并解析
                </button>
              </div>
            </>
          )}

          {/* ===== 上传后：澄清问题面板 — Apple 风格 ===== */}
          {uploadResult && (() => {
            // v2.8.55: 与澄清页面（RequirementClarify.tsx）保持一致，只显示 critical 和 important 问题
            // optional 问题不展示给用户（不阻塞 SRR 生成），避免弹窗与澄清页面计数不一致
            const visibleQuestions = uploadResult.questions.filter(q => q.priority === 'critical' || q.priority === 'important')
            return (
            <div className="space-y-4">
              <div className="flex items-center gap-2 py-2"
                style={{ borderBottom: '1px solid var(--border)' }}>
                <MessageCircle className="w-4 h-4" style={{ color: 'var(--brand-500)' }} />
                <span className="text-sm font-medium" style={{ color: 'var(--foreground)' }}>
                  请回答以下问题以完善需求分析（共 {visibleQuestions.length} 题）
                </span>
              </div>

              <div className="max-h-[50vh] overflow-y-auto space-y-4 pr-1">
                {visibleQuestions.map((q) => {
                  const isSelect = q.input_type === 'select' || q.input_type === 'multi_select'
                  const isMulti = q.input_type === 'multi_select'
                  const selOptions = q.options || []
                  const currentVal = uploadResult.answers[q.id] || ''

                  return (
                    <div key={q.id} className="p-4 rounded-xl border"
                      style={{
                        borderColor: q.priority === 'critical' ? 'rgba(255, 59, 48, 0.2)' :
                          q.priority === 'important' ? 'rgba(255, 149, 0, 0.2)' :
                          'var(--border)',
                        background: q.priority === 'critical' ? 'rgba(255, 59, 48, 0.04)' :
                          q.priority === 'important' ? 'rgba(255, 149, 0, 0.04)' :
                          'var(--background-muted)',
                      }}>
                      <div className="flex items-start gap-2 mb-2">
                        <MessageCircle className="w-4 h-4 mt-0.5 flex-shrink-0"
                          style={{ color: q.priority === 'critical' ? 'var(--state-error)' :
                            q.priority === 'important' ? 'var(--state-warning)' :
                            'var(--brand-500)' }} />
                        <div className="min-w-0">
                          <p className="text-sm font-medium m-0" style={{ color: 'var(--foreground)' }}>{q.question}</p>
                          <p className="text-xs mt-0.5 m-0" style={{ color: 'var(--foreground-tertiary)' }}>{q.context}</p>
                        </div>
                      </div>

                      {isSelect ? (
                        /* 选项选择器（select / multi_select） */
                        <div className="mt-2 space-y-1.5">
                          {selOptions.map((opt, oi) => {
                            const isSelected = isMulti
                              ? currentVal.split(',').includes(opt)
                              : currentVal === opt
                            return (
                              <button
                                key={oi}
                                onClick={() => {
                                  if (isMulti) {
                                    const parts = currentVal ? currentVal.split(',').filter(Boolean) : []
                                    const idx = parts.indexOf(opt)
                                    const next = idx >= 0
                                      ? parts.filter(p => p !== opt)
                                      : [...parts, opt]
                                    updateClarifyAnswer(q.id, next.join(','))
                                  } else {
                                    updateClarifyAnswer(q.id, currentVal === opt ? '' : opt)
                                  }
                                }}
                                className="w-full text-left px-3 py-2 rounded-lg text-sm border transition-all"
                                style={isSelected ? {
                                  background: 'rgba(0, 122, 255, 0.08)',
                                  borderColor: 'var(--brand-500)',
                                  color: 'var(--brand-500)',
                                } : {
                                  background: 'var(--background)',
                                  borderColor: 'var(--border)',
                                  color: 'var(--foreground-secondary)',
                                }}>
                                <span className="inline-flex items-center gap-2">
                                  <span className={`w-4 h-4 ${isMulti ? 'rounded-sm' : 'rounded-full'} border-2 flex items-center justify-center`}
                                    style={isSelected ? {
                                      borderColor: 'var(--brand-500)',
                                      background: 'var(--brand-500)',
                                    } : { borderColor: 'var(--border)' }}>
                                    {isSelected && <CheckCircle2 className="w-3 h-3 text-white" />}
                                  </span>
                                  {opt}
                                </span>
                              </button>
                            )
                          })}
                          {/* 自定义输入 */}
                          <div className="mt-2">
                            <textarea
                              value={currentVal}
                              onChange={e => updateClarifyAnswer(q.id, e.target.value)}
                              rows={2}
                              className="input-field text-sm"
                              placeholder={isMulti ? '选择以上选项或自行补充...' : '选择以上选项或自行填写...'}
                            />
                          </div>
                        </div>
                      ) : (
                        /* 纯文本输入 */
                        <textarea
                          value={currentVal}
                          onChange={e => updateClarifyAnswer(q.id, e.target.value)}
                          rows={2}
                          className="input-field mt-2 text-sm"
                          placeholder={q.default_answer || (q.priority === 'critical' ? '必填，请补充' : '选填，如有需要请补充...')}
                        />
                      )}
                    </div>
                  )
                })}
              </div>

              <div className="flex items-center justify-end gap-3 pt-3"
                style={{ borderTop: '1px solid var(--border)' }}>
                <button
                  onClick={() => { if (uploadResult) finishUpload(uploadResult.req_id) }}
                  className="btn-secondary"
                >
                  暂不处理
                </button>
                <button
                  onClick={submitUploadClarify}
                  disabled={submittingClarify}
                  className="btn-primary"
                >
                  {submittingClarify ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
                  提交并生成分析
                </button>
              </div>
              <p className="text-xs m-0" style={{ color: 'var(--foreground-tertiary)' }}>回答后系统将重新生成更精确的需求分析结果</p>
            </div>
            )
          })()}
        </div>
      </div>

      {/* ===== 影响分析确认面板 — Apple 风格 ===== */}
      {impactResult && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4"
          style={{ background: 'rgba(0, 0, 0, 0.32)', backdropFilter: 'blur(4px)' }}>
          <div className="max-w-lg w-full max-h-[80vh] overflow-y-auto animate-slide-up"
            style={{
              background: 'var(--background)',
              borderRadius: 'calc(var(--radius) * 1.1)',
              boxShadow: 'var(--shadow-apple-xl)',
              padding: 24,
            }}>
            <div className="flex items-center gap-3 mb-5">
              <Share2 className="w-5 h-5" style={{ color: '#af52de' }} />
              <div>
                <h2 className="m-0" style={{ fontSize: 18, fontWeight: 600, color: 'var(--foreground)' }}>影响分析</h2>
                <p className="text-xs m-0 mt-0.5" style={{ color: 'var(--foreground-tertiary)' }}>需求「{impactResult.req_title}」的变更影响范围</p>
              </div>
            </div>

            {/* 直接涉及的模块 */}
            {impactResult.direct_modules.length > 0 && (
              <div className="mb-4">
                <p className="text-xs font-medium mb-2 m-0" style={{ color: 'var(--foreground-tertiary)' }}>直接涉及的模块</p>
                <div className="flex flex-wrap gap-1.5">
                  {impactResult.direct_modules.map(m => (
                    <span key={m.id} className="px-2.5 py-1 rounded-lg text-xs font-medium border"
                      style={{ background: 'rgba(175, 82, 222, 0.08)', color: '#af52de', borderColor: 'rgba(175, 82, 222, 0.2)' }}>
                      {m.name}
                    </span>
                  ))}
                </div>
              </div>
            )}

            {/* 间接影响的模块 — 深度感知样式 */}
            {impactResult.indirect_modules.length > 0 && (
              <div className="mb-4">
                <p className="text-xs font-medium mb-2 m-0" style={{ color: 'var(--foreground-tertiary)' }}>
                  间接受影响的模块 ({impactResult.indirect_modules.length})
                </p>
                <div className="space-y-2">
                  {impactResult.indirect_modules.map((m, i) => {
                    const isDirect = m.depth <= 1
                    return (
                    <div key={i} className="flex items-start gap-3 px-4 py-3 rounded-xl border"
                      style={isDirect ? {
                        background: 'var(--state-warning-surface)',
                        borderColor: 'rgba(255, 149, 0, 0.2)',
                      } : {
                        background: 'var(--background-muted)',
                        borderColor: 'var(--border)',
                      }}>
                      <div className="w-2 h-2 rounded-full mt-2 flex-shrink-0"
                        style={{ background: isDirect ? 'var(--state-warning)' : 'var(--foreground-tertiary)' }} />
                      <div className="flex-1 min-w-0">
                        <p className="text-sm font-semibold m-0"
                          style={{ color: isDirect ? 'var(--state-warning)' : 'var(--foreground-secondary)' }}>
                          {m.name}
                        </p>
                        <p className="text-xs mt-0.5 m-0"
                          style={{ color: isDirect ? 'var(--state-warning)' : 'var(--foreground-tertiary)', opacity: 0.85 }}>
                          {m.reason}
                        </p>
                      </div>
                    </div>
                  )})}
                </div>
              </div>
            )}

            {/* 受影响的功能点（基于 FP 依赖图谱 BFS 分析） */}
            {impactResult.affected_fps.length > 0 && (
              <div className="mb-4">
                <p className="text-xs font-medium mb-2 m-0" style={{ color: 'var(--foreground-tertiary)' }}>
                  受影响的功能点 ({impactResult.affected_fps.length})
                  <span className="font-normal ml-1" style={{ color: 'var(--foreground-quaternary)' }}>基于 FP 依赖图谱 BFS 分析</span>
                </p>
                <div className="space-y-1.5">
                  {impactResult.affected_fps.map(fp => (
                    <div key={fp.fp_id} className="px-4 py-3 rounded-xl border"
                      style={{ background: 'var(--background)', borderColor: 'var(--border)' }}>
                      <div className="flex items-center justify-between mb-1">
                        <div className="flex items-center gap-2 min-w-0">
                          <p className="text-sm font-medium truncate m-0" style={{ color: 'var(--foreground)' }}>{fp.fp_name}</p>
                          {fp.fp_status === 'archived' && (
                            <span className="text-[10px] px-1.5 py-0.5 rounded font-medium flex-shrink-0"
                              style={{ background: 'var(--state-success-surface)', color: 'var(--state-success)' }}>已归档</span>
                          )}
                        </div>
                        <span className="text-xs flex-shrink-0" style={{ color: 'var(--foreground-tertiary)' }}>{fp.depth}跳</span>
                      </div>
                      <p className="text-xs m-0" style={{ color: 'var(--foreground-tertiary)' }}>{fp.impact_reason}</p>
                      {fp.module_name && (
                        <span className="text-[10px] mt-0.5 inline-block" style={{ color: 'var(--foreground-quaternary)' }}>模块: {fp.module_name}</span>
                      )}
                      {fp.kb_context && (
                        <div className="mt-2 p-2 rounded border"
                          style={{ background: 'rgba(0, 122, 255, 0.04)', borderColor: 'rgba(0, 122, 255, 0.15)' }}>
                          <div className="flex items-center gap-1 mb-0.5">
                            <Database className="w-3 h-3" style={{ color: 'var(--brand-500)' }} />
                            <span className="text-[10px] font-medium" style={{ color: 'var(--brand-500)' }}>知识库说明</span>
                          </div>
                          <p className="text-[11px] whitespace-pre-wrap line-clamp-3 m-0" style={{ color: 'var(--foreground-secondary)' }}>{fp.kb_context}</p>
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* 无影响 */}
            {impactResult.indirect_modules.length === 0 && impactResult.affected_fps.length === 0 && (
              <div className="text-center py-6">
                <CheckCircle2 className="w-10 h-10 mx-auto mb-3" style={{ color: 'var(--state-success)' }} />
                <p className="text-sm font-medium m-0" style={{ color: 'var(--state-success)' }}>未发现间接影响</p>
                <p className="text-xs mt-1 m-0" style={{ color: 'var(--foreground-tertiary)' }}>该需求变更不会影响其他模块或功能点</p>
              </div>
            )}

            <p className="text-xs mt-4 pt-4 m-0"
              style={{ color: 'var(--foreground-tertiary)', borderTop: '1px solid var(--border)' }}>
              以上为基于模块+FP 依赖图谱的自动分析结果，仅供参考
            </p>
            <div className="flex justify-end gap-3 mt-4">
              <button onClick={() => { finishUpload(impactResult.req_id) }}
                className="btn-secondary text-sm">
                忽略并完成
              </button>
              <button onClick={() => { finishUpload(impactResult.req_id) }}
                className="btn-primary text-sm">
                <CheckCircle2 className="w-4 h-4" />
                确认并完成
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}
