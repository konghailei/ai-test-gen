import { useState, useEffect, useRef } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { FileText, Upload, ArrowRight, Loader2, Filter, CheckCircle2, XCircle, AlertCircle, AlertTriangle, FileUp, Brain, Database, Sparkles, RefreshCw, Trash2, FileCode, File, FileType, MessageCircle, Send, Share2, Search, X, Download, Minimize2, Clock } from 'lucide-react'
import { requirements as reqApi, products as prodApi } from '../api/client'
import { useProduct } from '../store/ProductContext'
import type { Requirement, Product, ClarifyQuestion, Module, AnalysisTaskInfo, PhaseTiming } from '../types'
import { formatDate, priorityBadge, STATUS_LABELS, sourceTypeLabel } from '../utils'

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

export default function Requirements() {
  const { products, selectedProductId } = useProduct()
  const [reqs, setReqs] = useState<Requirement[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(true)

  // 项目筛选（产品由顶部栏全局控制）
  const [filterProject, setFilterProject] = useState('')
  const [filterStatus, setFilterStatus] = useState('')
  const [filterPriority, setFilterPriority] = useState('')
  const [searchKeyword, setSearchKeyword] = useState('')
  const [projects, setProjects] = useState<Record<string, { id: string; name: string }[]>>({})

  // 从 URL query 读取 project_id（支持从项目管理页面跳转过来）
  const [searchParams, setSearchParams] = useSearchParams()

  // Upload modal
  const [showUpload, setShowUpload] = useState(false)
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

  const navigate = useNavigate()

  // 头部产品变更时重置筛选，加载该产品的项目/模块
  useEffect(() => {
    // 优先从 URL 读取 project_id（由项目管理页面跳转过来时携带）
    const qProj = searchParams.get('project_id') || ''
    const qUpload = searchParams.get('upload') === 'true'  // v2.52: 从项目管理页面上传入口跳转
    const qReqId = searchParams.get('req_id') || ''
    setFilterProject(qProj)
    if (qProj) {
      // 清掉 query 参数，避免后续刷新重复带上
      setSearchParams({}, { replace: true })
    }
    // v2.52: 自动打开上传弹窗
    if (qUpload && !qReqId) {
      setShowUpload(true)
      setSelProject(qProj)
    }
    if (selectedProductId) loadProjects(selectedProductId)
    else { setModules([]); setSelModules([]) }
    setPage(1)
  }, [selectedProductId])

  const loadProjects = async (pid: string) => {
    if (pid && !projects[pid]) {
      const projs = await prodApi.listProjects(pid)
      setProjects(p => ({ ...p, [pid]: projs || [] }))
    }
    // 切换产品时加载模块
    if (pid) {
      prodApi.listModules(pid).then(mods => setModules(mods || [])).catch(() => setModules([]))
    } else {
      setModules([])
    }
  }

  const loadReqs = () => {
    setLoading(true)
    reqApi.list(page, 20, selectedProductId || undefined, filterProject || undefined, {
      status: filterStatus || undefined,
      priority: filterPriority || undefined,
      search: searchKeyword || undefined,
    }).then(r => {
      setReqs(r.items || [])
      setTotal(r.total || 0)
    }).catch(() => {}).finally(() => setLoading(false))
  }

  useEffect(() => { loadReqs() }, [page, selectedProductId, filterProject, filterStatus, filterPriority])

  // 搜索防抖
  useEffect(() => {
    const t = setTimeout(() => { setPage(1); loadReqs() }, 400)
    return () => clearTimeout(t)
  }, [searchKeyword])

  // 清理 progress timer
  useEffect(() => {
    return () => { if (progressTimer.current) clearInterval(progressTimer.current) }
  }, [])

  // ================================================================
  // 多文件上传（带进度反馈）
  // ================================================================
  const handleMultiFileUpload = async () => {
    if (uploadFiles.length === 0 || !selectedProductId) return

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
      const res: any = await reqApi.uploadMultiFiles(uploadFiles, selectedProductId, reqTitle, selProject, selModules.join(','))
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
        setProgress({ phase: 4, message: '影响分析完成（无影响范围），正在跳转...', error: null, progress: 100 })
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

  /** 完成上传流程 */
  const finishUpload = (reqId: string) => {
    setShowUpload(false)
    setUploadFiles([])
    setProgress(null)
    setImpactResult(null)
    // v2.55: 使用 replace 替换历史记录，详情页"返回"时跳过列表页，直接回到来源页（如项目管理页）
    if (reqId) navigate(`/requirements/${reqId}`, { replace: true })
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

  /** 阶段 4：重新生成最终 SRR 的进度模拟 */
  const startRegenProgressSimulation = () => {
    stopProgressTimer()
    let elapsed = 0
    progressTimer.current = setInterval(() => {
      elapsed += 1
      const hints = [
        '正在根据补充信息重新分析需求...',
        '正在整合用户反馈到需求模型...',
        '正在生成最终需求分析结果...',
      ]
      const hintIdx = Math.min(Math.floor(elapsed / 2), hints.length - 1)
      setProgress({
        phase: 3,
        message: `${hints[hintIdx]}${'.'.repeat(elapsed % 4)}`,
        error: null,
        progress: 85,
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

  const totalPages = Math.ceil(total / 20)

  // ================================================================
  // 删除需求（已归档不可删除）
  // ================================================================
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const [deleteModal, setDeleteModal] = useState<{ id: string; title: string; status: string; deps: any } | null>(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  // v2.0 导出 XMind
  const [exportingId, setExportingId] = useState<string | null>(null)

  const handleExportXmind = async (e: React.MouseEvent, r: Requirement) => {
    e.stopPropagation()
    setExportingId(r.id)
    try {
      const res = await reqApi.exportXmind(r.id)
      alert(`已导出 XMind 文件：${res.filename}（${(res.size / 1024).toFixed(1)} KB）`)
    } catch (err: any) {
      alert('XMind 导出失败: ' + (err?.message || '未知错误'))
    } finally {
      setExportingId(null)
    }
  }

  const handleDelete = async (e: React.MouseEvent, r: Requirement) => {
    e.stopPropagation()
    if (r.status === 'archived') {
      alert('已归档的需求不可删除。')
      return
    }
    // 拉取级联影响预览
    setPreviewLoading(true)
    try {
      const preview = await reqApi.deletePreview(r.id)
      setDeleteModal({
        id: r.id,
        title: r.title,
        status: r.status,
        deps: preview.dependencies,
      })
    } catch {
      // 预览失败也允许直接删除（兜底）
      if (!confirm(`确定删除需求「${r.title}」吗？`)) return
      await doDelete(r.id)
    } finally {
      setPreviewLoading(false)
    }
  }

  const doDelete = async (reqId: string) => {
    setDeletingId(reqId)
    try {
      await reqApi.delete(reqId)
      setReqs(prev => prev.filter(item => item.id !== reqId))
      setTotal(prev => prev - 1)
    } catch (err: any) {
      alert(err.message || '删除失败')
    } finally {
      setDeletingId(null)
      setDeleteModal(null)
    }
  }

  // ================================================================
  // 渲染
  // ================================================================
  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-4">
        <div>
          <h1 className="page-title">需求管理</h1>
          <p className="text-sm mt-1" style={{ color: 'var(--foreground-tertiary)' }}>管理需求文档 — 在顶部栏选择产品后管理需求</p>
        </div>
        <button onClick={() => { setShowUpload(true); setSelProject('') }} className="btn-primary">
          <Upload className="w-4 h-4" /> 上传需求
        </button>
      </div>

      {/* 查询条件栏（产品由顶部栏全局控制） — Apple 风格 */}
      <div
        className="flex items-center gap-3 flex-wrap"
        style={{
          background: 'var(--background)',
          border: '1px solid var(--border)',
          borderRadius: 'calc(var(--radius) * 0.75)',
          padding: '12px 20px',
          boxShadow: 'var(--shadow-apple-sm)',
        }}
      >
        <Filter className="w-4 h-4 flex-shrink-0" style={{ color: 'var(--foreground-tertiary)' }} />
        <span className="text-[13px] whitespace-nowrap" style={{ color: 'var(--foreground-tertiary)' }}>
          {selectedProductId
            ? `产品：${products.find(p => p.id === selectedProductId)?.name || selectedProductId}`
            : '提示：请在顶部栏选择产品'}
        </span>
        <select value={filterProject} onChange={e => { setFilterProject(e.target.value); setPage(1) }}
          className="select-field text-[13px] max-w-[160px]" style={{ padding: '6px 36px 6px 12px' }} disabled={!selectedProductId}>
          <option value="">全部项目</option>
          <option value="unassigned">(未绑定项目)</option>
          {(projects[selectedProductId] || []).map(p => (
            <option key={p.id} value={p.id}>{p.name}</option>
          ))}
        </select>
        <select value={filterStatus} onChange={e => { setFilterStatus(e.target.value); setPage(1) }}
          className="select-field text-[13px] max-w-[120px]" style={{ padding: '6px 36px 6px 12px' }}>
          <option value="">全部状态</option>
          <option value="draft">待澄清</option>
          <option value="analyzing">分析中</option>
          <option value="pending_review">待确认</option>
          <option value="confirmed">确认完成</option>
          <option value="active">已就绪</option>
          <option value="archived">已归档</option>
          <option value="superseded">已替代</option>
          <option value="deprecated">已废弃</option>
        </select>
        <select value={filterPriority} onChange={e => { setFilterPriority(e.target.value); setPage(1) }}
          className="select-field text-[13px] max-w-[100px]" style={{ padding: '6px 36px 6px 12px' }}>
          <option value="">全部优先级</option>
          <option value="P0">P0</option>
          <option value="P1">P1</option>
          <option value="P2">P2</option>
          <option value="P3">P3</option>
        </select>
        <div className="relative flex-1 min-w-[180px] max-w-[240px]">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5" style={{ color: 'var(--foreground-tertiary)' }} />
          <input
            type="text"
            value={searchKeyword}
            onChange={e => setSearchKeyword(e.target.value)}
            placeholder="搜索需求标题..."
            className="input-field text-[13px]"
            style={{ padding: '6px 28px 6px 30px' }}
          />
          {searchKeyword && (
            <button onClick={() => setSearchKeyword('')}
              className="absolute right-2 top-1/2 -translate-y-1/2"
              style={{ color: 'var(--foreground-tertiary)' }}>
              <X className="w-3.5 h-3.5" />
            </button>
          )}
        </div>
        <span className="text-xs ml-auto whitespace-nowrap" style={{ color: 'var(--foreground-tertiary)' }}>共 {total} 条</span>
      </div>

      {/* Upload Modal — Apple 风格 */}
      {showUpload && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4"
          style={{ background: 'rgba(0, 0, 0, 0.32)', backdropFilter: 'blur(4px)' }}
          onClick={() => { if (!progress) setShowUpload(false) }}>
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
                    setShowUpload(false)
                    setProgress(null)
                    setUploadFiles([])
                    navigate('/tasks')
                  }}
                  className="btn-secondary text-xs"
                  style={{ padding: '6px 12px' }}
                  title="关闭弹窗，前往任务管理页面查看进度"
                >
                  <Minimize2 className="w-3.5 h-3.5" /> 后台运行
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
                      {selectedProductId
                        ? products.find(p => p.id === selectedProductId)?.name || selectedProductId
                        : '请先在顶部栏选择产品'}
                    </div>
                  </div>
                  <div>
                    <label className="label">所属项目/版本 <span style={{ color: 'var(--state-error)' }}>*</span></label>
                    <select value={selProject} onChange={e => setSelProject(e.target.value)}
                      className="select-field text-sm" disabled={!selectedProductId}>
                      <option value="">请选择项目...</option>
                      {(projects[selectedProductId] || []).map(p => (
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
                  <button onClick={() => { setShowUpload(false); setUploadFiles([]); setReqTitle('') }} className="btn-secondary">取消</button>
                  <button onClick={handleMultiFileUpload} disabled={uploadFiles.length === 0 || !selectedProductId || !reqTitle.trim() || fileUploading} className="btn-primary">
                    {fileUploading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Upload className="w-4 h-4" />}
                    上传并解析
                  </button>
                </div>
              </>
            )}

            {/* ===== 上传后：澄清问题面板 — Apple 风格 ===== */}
            {uploadResult && (() => {
              // v2.55: 与澄清页面（RequirementClarify.tsx）保持一致，只显示 critical 和 important 问题
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
                    onClick={() => { setUploadResult(null); setShowUpload(false); setUploadFiles([]) }}
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
      )}

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
                忽略并查看需求
              </button>
              <button onClick={() => { finishUpload(impactResult.req_id) }}
                className="btn-primary text-sm">
                <CheckCircle2 className="w-4 h-4" />
                确认并查看需求
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 需求表格 — Apple 风格 */}
      <div className="overflow-hidden"
        style={{
          background: 'var(--background)',
          border: '1px solid var(--border)',
          borderRadius: 'calc(var(--radius) * 0.75)',
          boxShadow: 'var(--shadow-apple-sm)',
        }}>
        {loading ? (
          <div className="p-6 space-y-4">{[1,2,3,4,5].map(i => <div key={i} className="skeleton h-14" />)}</div>
        ) : reqs.length === 0 ? (
          <div className="text-center py-16">
            <FileText className="w-12 h-12 mx-auto mb-3" style={{ color: 'var(--foreground-quaternary)' }} />
            <p className="font-medium m-0" style={{ color: 'var(--foreground-secondary)' }}>{selectedProductId ? '该产品下暂无需求' : '暂无需求'}</p>
            <p className="text-sm mt-1 m-0" style={{ color: 'var(--foreground-tertiary)' }}>选择产品或上传第一个需求</p>
          </div>
        ) : (
          <table className="w-full">
            <thead>
              <tr style={{ background: 'var(--background-muted)' }}>
                <th className="text-left text-[11px] font-semibold uppercase tracking-wider px-5 py-3.5" style={{ color: 'var(--foreground-tertiary)' }}>ID</th>
                <th className="text-left text-[11px] font-semibold uppercase tracking-wider px-5 py-3.5" style={{ color: 'var(--foreground-tertiary)' }}>标题</th>
                <th className="text-left text-[11px] font-semibold uppercase tracking-wider px-5 py-3.5" style={{ color: 'var(--foreground-tertiary)' }}>模块</th>
                <th className="text-left text-[11px] font-semibold uppercase tracking-wider px-5 py-3.5" style={{ color: 'var(--foreground-tertiary)' }}>来源格式</th>
                <th className="text-left text-[11px] font-semibold uppercase tracking-wider px-5 py-3.5" style={{ color: 'var(--foreground-tertiary)' }}>产品</th>
                <th className="text-center text-[11px] font-semibold uppercase tracking-wider px-5 py-3.5" style={{ color: 'var(--foreground-tertiary)' }}>优先级</th>
                <th className="text-center text-[11px] font-semibold uppercase tracking-wider px-5 py-3.5" style={{ color: 'var(--foreground-tertiary)' }}>状态</th>
                <th className="text-right text-[11px] font-semibold uppercase tracking-wider px-5 py-3.5" style={{ color: 'var(--foreground-tertiary)' }}>创建时间</th>
                <th className="text-right px-5 py-3.5 w-24"></th>
              </tr>
            </thead>
            <tbody>
              {reqs.map(r => (
                <tr key={r.id} className="transition-colors cursor-pointer"
                  style={{ borderTop: '1px solid var(--border)' }}
                  onMouseEnter={e => { e.currentTarget.style.background = 'var(--background-muted)' }}
                  onMouseLeave={e => { e.currentTarget.style.background = 'transparent' }}
                  onClick={() => navigate(`/requirements/${r.id}`)}>
                  <td className="px-5 py-3.5"><span className="text-xs font-mono" style={{ color: 'var(--foreground-tertiary)' }}>{r.id}</span></td>
                  <td className="px-5 py-3.5"><p className="text-sm font-medium truncate max-w-[200px] m-0" style={{ color: 'var(--foreground)' }}>{r.title}</p></td>
                  <td className="px-5 py-3.5">
                    <div className="flex flex-wrap gap-1 max-w-[160px]">
                      {Array.isArray(r.modules) && r.modules.length > 0
                        ? (r.modules as string[]).map((mid: string) => (
                            <span key={mid} className="text-[10px] px-1.5 py-0.5 rounded font-mono truncate max-w-[80px]"
                              style={{ background: 'var(--background-muted)', color: 'var(--foreground-tertiary)' }} title={mid}>
                              {modules.find(m => m.id === mid)?.name || mid}
                            </span>
                          ))
                        : <span className="text-xs" style={{ color: 'var(--foreground-quaternary)' }}>—</span>
                      }
                    </div>
                  </td>
                  <td className="px-5 py-3.5">
                    <span className="inline-flex items-center gap-1 text-xs" style={{ color: 'var(--foreground-tertiary)' }}>
                      {r.source_type === 'markdown' ? <FileCode className="w-3.5 h-3.5" style={{ color: 'var(--brand-500)' }} /> :
                       r.source_type === 'docx' ? <FileType className="w-3.5 h-3.5" style={{ color: 'var(--state-warning)' }} /> :
                       <FileText className="w-3.5 h-3.5" style={{ color: 'var(--foreground-tertiary)' }} />}
                      {sourceTypeLabel(r.source_type)}
                    </span>
                  </td>
                  <td className="px-5 py-3.5"><span className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>{(products.find(p => p.id === r.product_id)?.name) || r.product_id}</span></td>
                  <td className="px-5 py-3.5 text-center"><span className={priorityBadge(r.priority)}>{r.priority}</span></td>
                  <td className="px-5 py-3.5 text-center">
                    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium"
                      style={(() => {
                        switch (r.status) {
                          case 'draft': return { background: 'var(--state-warning-surface)', color: 'var(--state-warning)' }
                          case 'active': return { background: 'var(--state-success-surface)', color: 'var(--state-success)' }
                          case 'archived': return { background: 'var(--background-muted)', color: 'var(--foreground-tertiary)' }
                          case 'superseded': return { background: 'var(--state-warning-surface)', color: 'var(--state-warning)' }
                          case 'deprecated': return { background: 'var(--state-error-surface)', color: 'var(--state-error)' }
                          default: return { background: 'rgba(0, 122, 255, 0.08)', color: 'var(--brand-500)' }
                        }
                      })()}>
                      {STATUS_LABELS[r.status] || r.status}
                      {r.status === 'draft' && <MessageCircle className="w-3 h-3 ml-1 animate-pulse" />}
                    </span>
                  </td>
                  <td className="px-5 py-3.5 text-right"><span className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>{formatDate(r.created_at)}</span></td>
                  <td className="px-5 py-3.5 text-right">
                    <div className="flex items-center justify-end gap-1">
                      <button onClick={e => { e.stopPropagation(); navigate(`/requirements/${r.id}`) }} className="btn-ghost p-1.5" title="查看详情">
                        <ArrowRight className="w-4 h-4" />
                      </button>
                      <button
                        onClick={e => handleExportXmind(e, r)}
                        disabled={exportingId === r.id}
                        className="p-1.5 rounded-lg transition-colors disabled:opacity-50"
                        style={{ color: 'var(--foreground-tertiary)' }}
                        onMouseEnter={e => { if (!exportingId) { e.currentTarget.style.background = 'rgba(175, 82, 222, 0.08)'; e.currentTarget.style.color = '#af52de' } }}
                        onMouseLeave={e => { if (!exportingId) { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = 'var(--foreground-tertiary)' } }}
                        title="导出 XMind 思维导图"
                      >
                        {exportingId === r.id
                          ? <Loader2 className="w-4 h-4 animate-spin" />
                          : <Download className="w-4 h-4" />
                        }
                      </button>
                      <button
                        onClick={e => handleDelete(e, r)}
                        disabled={deletingId === r.id || r.status === 'archived'}
                        className="p-1.5 rounded-lg transition-colors"
                        style={r.status === 'archived'
                          ? { color: 'var(--foreground-quaternary)', cursor: 'not-allowed', opacity: 0.4 }
                          : { color: 'var(--foreground-tertiary)' }}
                        onMouseEnter={e => { if (r.status !== 'archived' && !deletingId) { e.currentTarget.style.background = 'var(--state-error-surface)'; e.currentTarget.style.color = 'var(--state-error)' } }}
                        onMouseLeave={e => { if (r.status !== 'archived' && !deletingId) { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = 'var(--foreground-tertiary)' } }}
                        title={r.status === 'archived' ? '已归档需求不可删除' : '删除需求'}
                      >
                        {deletingId === r.id
                          ? <Loader2 className="w-4 h-4 animate-spin" />
                          : <Trash2 className="w-4 h-4" />
                        }
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {totalPages > 1 && (
          <div className="flex items-center justify-between px-5 py-3"
            style={{ borderTop: '1px solid var(--border)', background: 'var(--background-muted)' }}>
            <span className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>共 {total} 条</span>
            <div className="flex items-center gap-1">
              <button disabled={page <= 1} onClick={() => setPage(page - 1)} className="btn-ghost text-xs py-1 disabled:opacity-30">上一页</button>
              <span className="text-xs px-2" style={{ color: 'var(--foreground-secondary)' }}>{page}/{totalPages}</span>
              <button disabled={page >= totalPages} onClick={() => setPage(page + 1)} className="btn-ghost text-xs py-1 disabled:opacity-30">下一页</button>
            </div>
          </div>
        )}
      </div>

      {/* 删除确认弹窗 — Apple 风格 */}
      {deleteModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4"
          style={{ background: 'rgba(0, 0, 0, 0.32)', backdropFilter: 'blur(4px)' }}
          onClick={() => setDeleteModal(null)}>
          <div className="max-w-lg w-full animate-slide-up"
            style={{
              background: 'var(--background)',
              borderRadius: 'calc(var(--radius) * 1.1)',
              boxShadow: 'var(--shadow-apple-xl)',
              padding: 24,
            }}
            onClick={e => e.stopPropagation()}>
            <div className="flex items-start gap-3 mb-4">
              <div className="w-10 h-10 rounded-full flex items-center justify-center flex-shrink-0"
                style={{ background: 'var(--state-error-surface)' }}>
                <AlertTriangle className="w-5 h-5" style={{ color: 'var(--state-error)' }} />
              </div>
              <div>
                <h2 className="m-0" style={{ fontSize: 18, fontWeight: 600, color: 'var(--foreground)' }}>确认删除需求</h2>
                <p className="text-sm mt-0.5 m-0" style={{ color: 'var(--foreground-tertiary)' }}>
                  「{deleteModal.title}」将被<strong style={{ color: 'var(--state-error)' }}>永久删除</strong>，以下关联数据也将一并清除：
                </p>
              </div>
            </div>

            {/* 依赖清单 */}
            <div className="mb-4 p-3 space-y-1.5 text-sm"
              style={{ background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.5)' }}>
              {deleteModal.deps.test_cases > 0 && (
                <div className="flex justify-between"><span style={{ color: 'var(--foreground-tertiary)' }}>测试用例</span><span className="font-medium" style={{ color: 'var(--foreground-secondary)' }}>{deleteModal.deps.test_cases} 条</span></div>
              )}
              {deleteModal.deps.functional_points > 0 && (
                <div className="flex justify-between"><span style={{ color: 'var(--foreground-tertiary)' }}>功能点 + 依赖关系</span><span className="font-medium" style={{ color: 'var(--foreground-secondary)' }}>{deleteModal.deps.functional_points} 个</span></div>
              )}
              {deleteModal.deps.generation_batches > 0 && (
                <div className="flex justify-between"><span style={{ color: 'var(--foreground-tertiary)' }}>生成批次记录</span><span className="font-medium" style={{ color: 'var(--foreground-secondary)' }}>{deleteModal.deps.generation_batches} 条</span></div>
              )}
              {deleteModal.deps.req_dependencies > 0 && (
                <div className="flex justify-between"><span style={{ color: 'var(--foreground-tertiary)' }}>需求间依赖关系</span><span className="font-medium" style={{ color: 'var(--foreground-secondary)' }}>{deleteModal.deps.req_dependencies} 条</span></div>
              )}
              {deleteModal.deps.analysis_tasks > 0 && (
                <div className="flex justify-between"><span style={{ color: 'var(--foreground-tertiary)' }}>异步分析任务记录</span><span className="font-medium" style={{ color: 'var(--foreground-secondary)' }}>{deleteModal.deps.analysis_tasks} 条</span></div>
              )}
              {deleteModal.deps.traceability > 0 && (
                <div className="flex justify-between"><span style={{ color: 'var(--foreground-tertiary)' }}>需求-用例追溯记录</span><span className="font-medium" style={{ color: 'var(--foreground-secondary)' }}>{deleteModal.deps.traceability} 条</span></div>
              )}
              {deleteModal.deps.superseded_refs > 0 && (
                <div className="flex justify-between"><span style={{ color: 'var(--foreground-tertiary)' }}>其他需求的替代引用</span><span className="font-medium" style={{ color: 'var(--foreground-secondary)' }}>{deleteModal.deps.superseded_refs} 处（将置空）</span></div>
              )}
              {Object.values(deleteModal.deps).every((v: any) => v === 0) && (
                <p className="text-center py-2 m-0" style={{ color: 'var(--foreground-tertiary)' }}>未检测到关联数据</p>
              )}
            </div>

            <div className="flex items-center justify-end gap-3">
              <button onClick={() => setDeleteModal(null)} className="btn-secondary" disabled={!!deletingId}>
                取消
              </button>
              <button
                onClick={() => doDelete(deleteModal.id)}
                disabled={!!deletingId}
                className="btn-danger"
              >
                {deletingId === deleteModal.id ? (
                  <><Loader2 className="w-4 h-4 animate-spin" /> 删除中...</>
                ) : (
                  <><Trash2 className="w-4 h-4" /> 确认删除</>
                )}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* v2.24: 移除右下角浮动进度卡片 — 进度统一在任务管理页面展示 */}
    </div>
  )
}

function sleep(ms: number) { return new Promise(r => setTimeout(r, ms)) }
