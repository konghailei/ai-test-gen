import { useState, useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { FileText, Upload, ArrowRight, Loader2, Filter, CheckCircle2, XCircle, AlertCircle, FileUp, Brain, Database, Sparkles, RefreshCw, Trash2, FileCode, File, FileType, MessageCircle, Send, Share2 } from 'lucide-react'
import { requirements as reqApi, products as prodApi } from '../api/client'
import { useProduct } from '../store/ProductContext'
import type { Requirement, Product, ClarifyQuestion, Module } from '../types'
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
  phase: number          // 当前阶段索引（0-3）
  message: string       // 当前状态描述
  error: string | null  // 错误信息（不为空时表示出错）
}

export default function Requirements() {
  const { products, selectedProductId } = useProduct()
  const [reqs, setReqs] = useState<Requirement[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(true)

  // 项目筛选（产品由顶部栏全局控制）
  const [filterProject, setFilterProject] = useState('')
  const [projects, setProjects] = useState<Record<string, { id: string; name: string }[]>>({})

  // Upload modal
  const [showUpload, setShowUpload] = useState(false)
  const [selProject, setSelProject] = useState('')
  const [uploadFiles, setUploadFiles] = useState<File[]>([])
  const [fileUploading, setFileUploading] = useState(false)

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

  // 上传后的影响分析结果
  const [impactResult, setImpactResult] = useState<{
    req_id: string
    req_title: string
    direct_modules: { id: string; name: string }[]
    indirect_modules: { id: string; name: string; reason: string; depth: number }[]
    affected_requirements: { id: string; title: string; priority: string; modules: string[] }[]
  } | null>(null)

  // 上传进度
  const [progress, setProgress] = useState<UploadProgress | null>(null)
  const progressTimer = useRef<ReturnType<typeof setInterval> | null>(null)

  const navigate = useNavigate()

  // 头部产品变更时重置筛选，加载该产品的项目/模块
  useEffect(() => {
    setFilterProject('')
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
    reqApi.list(page, 20, selectedProductId || undefined, filterProject || undefined).then(r => {
      setReqs(r.items || [])
      setTotal(r.total || 0)
    }).catch(() => {}).finally(() => setLoading(false))
  }

  useEffect(() => { loadReqs() }, [page, selectedProductId, filterProject])

  // 清理 progress timer
  useEffect(() => {
    return () => { if (progressTimer.current) clearInterval(progressTimer.current) }
  }, [])

  // ================================================================
  // 多文件上传（带进度反馈）
  // ================================================================
  const handleMultiFileUpload = async () => {
    if (uploadFiles.length === 0 || !selectedProductId) return

    // 校验项目和模块必选
    if (!selProject) {
      setProgress({ phase: 0, message: '请选择所属项目/版本', error: '所属项目/版本为必选项，请选择后再上传' })
      return
    }
    if (selModules.length === 0) {
      setProgress({ phase: 0, message: '请选择关联模块', error: '关联模块为必选项，请至少选择一个模块后再上传' })
      return
    }

    setProgress({ phase: 0, message: `正在上传 ${uploadFiles.length} 个文件...`, error: null })
    startProgressSimulation()
    setFileUploading(true)

    try {
      await sleep(400)
      setProgress({ phase: 0, message: '正在调用 AI 解析文档内容...', error: null })

      const res: any = await reqApi.uploadMultiFiles(uploadFiles, selectedProductId, '', selProject, selModules.join(','))

      stopProgressTimer()

      // 处理解析后的澄清问题
      const questions: ClarifyQuestion[] = res?.data?.questions || []
      const reqId = res?.data?.req_id

      if (questions.length > 0 && reqId) {
        // 有未回答问题：先停到阶段 3，再平滑切换到问题面板
        setProgress({ phase: 2, message: '检测到需要补充的内容，请确认以下问题', error: null })
        await sleep(800)
        setProgress(null)
        const initAnswers: Record<string, string> = {}
        questions.forEach(q => { initAnswers[q.id] = '' })
        setUploadResult({ req_id: reqId, questions, answers: initAnswers })
      } else {
        // 无问题：进入影响分析阶段
        await runImpactAnalysis(reqId)
      }
    } catch (e: any) {
      stopProgressTimer()
      setProgress(prev => ({
        phase: prev?.phase || 0,
        message: '文件上传失败',
        error: e.message || '请求失败，请稍后重试',
      }))
    } finally {
      setFileUploading(false)
    }
  }

  /** 运行影响分析（基于模块依赖图谱 BFS 遍历） */
  const runImpactAnalysis = async (reqId: string) => {
    try {
      setProgress({ phase: 4, message: '正在进行影响分析（基于模块依赖图谱 BFS 遍历）...', error: null })
      startProgressSimulation()

      const impactRes = await reqApi.analyzeImpact(reqId)

      stopProgressTimer()

      // 如果有间接影响或受影响需求，展示确认面板
      const hasImpacts = (impactRes.indirect_modules?.length || 0) > 0 ||
                         (impactRes.affected_requirements?.length || 0) > 0

      if (hasImpacts) {
        setProgress({ phase: 4, message: '影响分析完成，请确认以下影响范围', error: null })
        await sleep(600)
        setProgress(null)
        setImpactResult(impactRes)
      } else {
        // 无影响，直接完成
        setProgress({ phase: 4, message: '影响分析完成（无影响范围），正在跳转...', error: null })
        await sleep(800)
        finishUpload(reqId)
      }
    } catch (e: any) {
      stopProgressTimer()
      setProgress({ phase: 4, message: '影响分析未完成（可稍后在需求详情页重新分析）', error: null })
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
    if (reqId) navigate(`/requirements/${reqId}`)
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
  // 进度条模拟（根据阶段推进显示不同消息）
  // ================================================================
  const startProgressSimulation = () => {
    stopProgressTimer()
    let elapsed = 0
    progressTimer.current = setInterval(() => {
      elapsed += 1
      if (elapsed < 5) {
        // 阶段 0：LLM 分析整理
        const hints = [
          '正在提取需求功能描述...',
          '正在识别业务规则和边界条件...',
          '正在分析数据约束和流程...',
          '正在构建结构化需求模型...',
        ]
        const hintIdx = Math.min(Math.floor(elapsed / 1), hints.length - 1)
        setProgress(prev => prev?.error ? prev : {
          phase: 0, message: `${hints[hintIdx]}${'.'.repeat(elapsed % 4)}`, error: null,
        })
      } else if (elapsed < 8) {
        // 阶段 1：知识库检索补充
        const dots = '.'.repeat((elapsed - 5) % 4)
        const hints = [
          '正在查询知识库历史需求...',
          '正在比对相似功能模块...',
          '正在自动补充缺失字段...',
        ]
        const hintIdx = Math.min(Math.floor((elapsed - 5) / 1), hints.length - 1)
        setProgress(prev => prev?.error ? prev : {
          phase: 1, message: `${hints[hintIdx]}${dots}`, error: null,
        })
      } else if (elapsed < 12) {
        // 阶段 2：检测不清晰内容（即将切换为人机交互）
        const dots = '.'.repeat((elapsed - 8) % 4)
        setProgress(prev => prev?.error ? prev : {
          phase: 2, message: `正在检测不清晰的需求内容${dots}`, error: null,
        })
      } else {
        // 超过 12 秒：提示等待
        setProgress(prev => prev?.error ? prev : {
          phase: 2, message: '⏳ 正在等待 AI 完成分析...', error: null,
        })
      }
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
      })
    }, 1200)
  }

  const stopProgressTimer = () => {
    if (progressTimer.current) {
      clearInterval(progressTimer.current)
      progressTimer.current = null
    }
  }

  const totalPages = Math.ceil(total / 20)

  // ================================================================
  // 删除需求（已归档不可删除）
  // ================================================================
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const handleDelete = async (e: React.MouseEvent, r: Requirement) => {
    e.stopPropagation()
    if (!confirm(`确定删除需求「${r.title}」吗？${r.status === 'archived' ? '\n\n⚠️ 已归档需求不可删除' : ''}`)) return
    setDeletingId(r.id)
    try {
      await reqApi.delete(r.id)
      setReqs(prev => prev.filter(item => item.id !== r.id))
      setTotal(prev => prev - 1)
    } catch (err: any) {
      alert(err.message || '删除失败')
    } finally {
      setDeletingId(null)
    }
  }

  // ================================================================
  // 渲染
  // ================================================================
  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="page-title">需求管理</h1>
          <p className="text-sm text-slate-500 mt-1">管理需求文档 — 在顶部栏选择产品后管理需求</p>
        </div>
        <button onClick={() => { setShowUpload(true); setSelProject('') }} className="btn-primary">
          <Upload className="w-4 h-4" /> 上传需求
        </button>
      </div>

      {/* 项目筛选栏（产品由顶部栏全局控制） */}
      <div className="card py-3 px-5">
        <div className="flex items-center gap-3 flex-wrap">
          <Filter className="w-4 h-4 text-slate-400" />
          <span className="text-sm text-slate-500">
            {selectedProductId
              ? `产品：${products.find(p => p.id === selectedProductId)?.name || selectedProductId}`
              : '提示：请在顶部栏选择产品'}
          </span>
          <select value={filterProject} onChange={e => { setFilterProject(e.target.value); setPage(1) }}
            className="select-field text-sm max-w-[200px]" disabled={!selectedProductId}>
            <option value="">全部项目</option>
            {(projects[selectedProductId] || []).map(p => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
          <span className="text-xs text-slate-400">共 {total} 条</span>
        </div>
      </div>

      {/* Upload Modal */}
      {showUpload && (
        <div className="fixed inset-0 z-50 bg-black/30 backdrop-blur-sm flex items-center justify-center p-4" onClick={() => {
          if (!progress) setShowUpload(false)
        }}>
          <div className="bg-white rounded-2xl shadow-xl max-w-lg w-full p-6 animate-slide-up" onClick={e => e.stopPropagation()}>
            <h2 className="text-lg font-semibold text-slate-800 mb-4">
              {progress ? '正在上传需求' : uploadResult ? '请补充需求信息' : '上传需求'}
            </h2>

            {/* ===== 上传中：阶段化进度条 ===== */}
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
                        <div className={`w-9 h-9 rounded-full flex items-center justify-center transition-all duration-300 ${
                          isDone ? 'bg-emerald-50 text-emerald-600' :
                          isError ? 'bg-red-100 text-red-500' :
                          isCurrent ? 'bg-indigo-100 text-indigo-600 ring-4 ring-indigo-100/50' :
                          'bg-slate-100 text-slate-300'
                        }`}>
                          {isDone ? <CheckCircle2 className="w-5 h-5" /> :
                           isError ? <XCircle className="w-5 h-5" /> :
                           isCurrent ? <Loader2 className="w-5 h-5 animate-spin" /> :
                           <phase.icon className="w-5 h-5" />}
                        </div>
                        <span className={`text-[10px] font-medium text-center leading-tight max-w-[64px] ${
                          isCurrent ? 'text-indigo-600' : isDone ? 'text-emerald-600' : 'text-slate-400'
                        }`}>{phase.label}</span>
                      </div>
                    )
                  })}
                </div>

                {/* 连接线 */}
                <div className="relative h-0.5 bg-slate-100 -mt-2 mx-6">
                  <div className="absolute left-0 top-0 h-full bg-gradient-to-r from-indigo-500 to-emerald-500 transition-all duration-700 rounded-full"
                    style={{ width: `${(progress.phase / (UPLOAD_PHASES.length - 1)) * 100}%` }} />
                </div>

                {/* 当前阶段状态消息 */}
                <div className="text-center">
                  <div className="inline-flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm text-slate-600">
                    <span className="text-xs text-slate-400 font-mono">
                      阶段 {progress.phase + 1}/{UPLOAD_PHASES.length}
                    </span>
                    <span>{progress.message}</span>
                  </div>
                </div>

                {/* 错误信息 */}
                {progress.error && (
                  <div className="p-4 bg-red-50 border border-red-200 rounded-xl">
                    <div className="flex items-start gap-3">
                      <AlertCircle className="w-5 h-5 text-red-500 flex-shrink-0 mt-0.5" />
                      <div className="min-w-0">
                        <p className="text-sm font-semibold text-red-700 mb-1">上传失败</p>
                        <pre className="text-xs text-red-600 whitespace-pre-wrap leading-relaxed font-sans">
                          {progress.error}
                        </pre>
                      </div>
                    </div>
                    <div className="mt-3 flex gap-2">
                      <button onClick={() => setProgress(null)} className="btn-secondary text-xs py-1.5">
                        返回修改
                      </button>
                      <button onClick={handleMultiFileUpload} className="btn-primary text-xs py-1.5">
                        <RefreshCw className="w-3 h-3" /> 重试
                      </button>
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* ===== 非上传中：表单（仅在无问题需要回答时显示） ===== */}
            {!progress && !uploadResult && (
              <>
                {/* 选择项目和模块（产品已在顶部栏选中） */}
                <div className="space-y-3 mb-4">
                  <div>
                    <label className="label">当前产品</label>
                    <div className="text-sm font-medium text-slate-700 bg-slate-50 rounded-lg px-3 py-2">
                      {selectedProductId
                        ? products.find(p => p.id === selectedProductId)?.name || selectedProductId
                        : '请先在顶部栏选择产品'}
                    </div>
                  </div>
                  <div>
                    <label className="label">所属项目/版本 <span className="text-red-400">*</span></label>
                    <select value={selProject} onChange={e => setSelProject(e.target.value)}
                      className="select-field text-sm" disabled={!selectedProductId}>
                      <option value="">请选择项目...</option>
                      {(projects[selectedProductId] || []).map(p => (
                        <option key={p.id} value={p.id}>{p.name}</option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label className="label">关联模块 <span className="text-red-400">*</span></label>
                    <div className="flex flex-wrap gap-1.5">
                      {modules.length === 0 ? (
                        <span className="text-xs text-slate-400 py-1">暂无模块数据，请在「产品与项目」中创建</span>
                      ) : modules.map(m => (
                        <button key={m.id} onClick={() => {
                          setSelModules(prev => prev.includes(m.id) ? prev.filter(x => x !== m.id) : [...prev, m.id])
                        }} className={`px-3 py-1 rounded-lg text-xs font-medium border transition-all ${
                          selModules.includes(m.id)
                            ? 'bg-primary-50 border-primary-300 text-primary-700'
                            : 'bg-white border-slate-200 text-slate-500 hover:border-slate-300'
                        }`}>
                          {m.name}
                        </button>
                      ))}
                    </div>
                  </div>
                </div>

                {/* 多文件上传 */}
                <div className="mb-4 p-4 border-2 border-dashed border-slate-200 rounded-xl hover:border-primary-200 transition-colors">
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
                    className="block w-full text-sm text-slate-500 file:mr-4 file:py-2 file:px-4 file:rounded-xl file:border-0 file:text-sm file:font-medium file:bg-primary-50 file:text-primary-700 hover:file:bg-primary-100" />
                </div>

                {/* 已选文件列表 */}
                {uploadFiles.length > 0 && (
                  <div className="space-y-2 mb-4">
                    <p className="text-xs text-slate-400">已选择 {uploadFiles.length} 个文件：</p>
                    {uploadFiles.map((f, i) => (
                      <div key={i} className="flex items-center justify-between bg-slate-50 p-2.5 rounded-lg">
                        <span className="text-xs text-slate-600 flex items-center gap-1.5">
                          <FileUp className="w-3.5 h-3.5 text-slate-400" />
                          {f.name}
                          <span className="text-slate-400">({(f.size / 1024).toFixed(1)} KB)</span>
                        </span>
                        <button onClick={() => removeFile(i)} className="text-slate-400 hover:text-red-500 p-1" title="移除">
                          <XCircle className="w-4 h-4" />
                        </button>
                      </div>
                    ))}
                  </div>
                )}

                <div className="flex items-center justify-end gap-3 mt-5">
                  <button onClick={() => { setShowUpload(false); setUploadFiles([]) }} className="btn-secondary">取消</button>
                  <button onClick={handleMultiFileUpload} disabled={uploadFiles.length === 0 || !selectedProductId || fileUploading} className="btn-primary">
                    {fileUploading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Upload className="w-4 h-4" />}
                    上传并解析
                  </button>
                </div>
              </>
            )}

            {/* ===== 上传后：澄清问题面板 ===== */}
            {uploadResult && (
              <div className="space-y-4">
                <div className="flex items-center gap-2 py-2 border-b border-slate-100">
                  <MessageCircle className="w-4 h-4 text-primary-500" />
                  <span className="text-sm font-medium text-slate-700">
                    请回答以下问题以完善需求分析（共 {uploadResult.questions.length} 题）
                  </span>
                </div>

                <div className="max-h-[50vh] overflow-y-auto space-y-4 pr-1">
                  {uploadResult.questions.map((q) => {
                    const isSelect = q.input_type === 'select' || q.input_type === 'multi_select'
                    const isMulti = q.input_type === 'multi_select'
                    const selOptions = q.options || []
                    const currentVal = uploadResult.answers[q.id] || ''

                    return (
                      <div key={q.id} className={`p-4 rounded-xl border ${
                        q.priority === 'critical' ? 'border-red-200 bg-red-50/30' :
                        q.priority === 'important' ? 'border-amber-200 bg-amber-50/30' :
                        'border-slate-100 bg-slate-50'
                      }`}>
                        <div className="flex items-start gap-2 mb-2">
                          <MessageCircle className={`w-4 h-4 mt-0.5 flex-shrink-0 ${
                            q.priority === 'critical' ? 'text-red-500' :
                            q.priority === 'important' ? 'text-amber-500' :
                            'text-primary-500'
                          }`} />
                          <div className="min-w-0">
                            <p className="text-sm font-medium text-slate-700">{q.question}</p>
                            <p className="text-xs text-slate-400 mt-0.5">{q.context}</p>
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
                                  className={`w-full text-left px-3 py-2 rounded-lg text-sm border transition-all ${
                                    isSelected
                                      ? 'bg-primary-50 border-primary-300 text-primary-700'
                                      : 'bg-white border-slate-200 text-slate-600 hover:border-slate-300'
                                  }`}
                                >
                                  <span className="inline-flex items-center gap-2">
                                    <span className={`w-4 h-4 rounded-${isMulti ? 'sm' : 'full'} border-2 flex items-center justify-center ${
                                      isSelected
                                        ? 'border-primary-500 bg-primary-500'
                                        : 'border-slate-300'
                                    }`}>
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

                <div className="flex items-center justify-end gap-3 pt-3 border-t border-slate-100">
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
                <p className="text-xs text-slate-400">💡 回答后系统将重新生成更精确的需求分析结果</p>
              </div>
            )}
          </div>
        </div>
      )}

      {/* ===== 影响分析确认面板（独立弹窗）===== */}
      {impactResult && (
        <div className="fixed inset-0 z-50 bg-black/30 backdrop-blur-sm flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-xl max-w-lg w-full max-h-[80vh] overflow-y-auto p-6 animate-slide-up">
            <div className="flex items-center gap-3 mb-5">
              <Share2 className="w-5 h-5 text-violet-500" />
              <div>
                <h2 className="text-lg font-semibold text-slate-800">影响分析</h2>
                <p className="text-xs text-slate-400">需求「{impactResult.req_title}」的变更影响范围</p>
              </div>
            </div>

            {/* 直接涉及的模块 */}
            {impactResult.direct_modules.length > 0 && (
              <div className="mb-4">
                <p className="text-xs font-medium text-slate-500 mb-2">直接涉及的模块</p>
                <div className="flex flex-wrap gap-1.5">
                  {impactResult.direct_modules.map(m => (
                    <span key={m.id} className="px-2.5 py-1 bg-violet-50 text-violet-700 rounded-lg text-xs font-medium border border-violet-200">
                      {m.name}
                    </span>
                  ))}
                </div>
              </div>
            )}

            {/* 间接影响的模块 — 深度感知样式 */}
            {impactResult.indirect_modules.length > 0 && (
              <div className="mb-4">
                <p className="text-xs font-medium text-slate-500 mb-2">
                  间接受影响的模块 ({impactResult.indirect_modules.length})
                </p>
                <div className="space-y-2">
                  {impactResult.indirect_modules.map((m, i) => {
                    const isDirect = m.depth <= 1
                    return (
                    <div key={i} className={`flex items-start gap-3 px-4 py-3 rounded-xl border ${
                      isDirect
                        ? 'bg-amber-50 border-amber-200'
                        : 'bg-slate-50 border-slate-200'
                    }`}>
                      <div className={`w-2 h-2 rounded-full mt-2 flex-shrink-0 ${
                        isDirect ? 'bg-amber-400' : 'bg-slate-400'
                      }`} />
                      <div className="flex-1 min-w-0">
                        <p className={`text-sm font-semibold ${isDirect ? 'text-amber-800' : 'text-slate-700'}`}>
                          {m.name}
                        </p>
                        <p className={`text-xs mt-0.5 ${isDirect ? 'text-amber-600' : 'text-slate-500'}`}>
                          {m.reason}
                        </p>
                      </div>
                    </div>
                  )})}
                </div>
              </div>
            )}

            {/* 受影响的其他需求 */}
            {impactResult.affected_requirements.length > 0 && (
              <div className="mb-4">
                <p className="text-xs font-medium text-slate-500 mb-2">
                  受影响的其他需求 ({impactResult.affected_requirements.length})
                </p>
                <div className="space-y-1.5">
                  {impactResult.affected_requirements.map(r => (
                    <div key={r.id} className="px-4 py-3 bg-white rounded-xl border border-slate-200 flex items-center justify-between">
                      <div className="min-w-0 flex-1">
                        <p className="text-sm font-medium text-slate-700 truncate">{r.title}</p>
                        <span className="text-xs text-slate-400">{r.priority} · {r.id}</span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* 无影响 */}
            {impactResult.indirect_modules.length === 0 && impactResult.affected_requirements.length === 0 && (
              <div className="text-center py-6">
                <CheckCircle2 className="w-10 h-10 mx-auto mb-3 text-emerald-400" />
                <p className="text-sm font-medium text-emerald-600">未发现间接影响</p>
                <p className="text-xs text-slate-400 mt-1">该需求变更不会影响其他模块或需求</p>
              </div>
            )}

            <p className="text-xs text-slate-400 mt-4 pt-4 border-t border-slate-100">
              以上为基于模块依赖图谱的自动分析结果，仅供参考
            </p>
            <div className="flex justify-end gap-3 mt-4">
              <button onClick={() => { finishUpload(impactResult.req_id) }}
                className="btn-secondary text-sm">
                忽略并查看需求
              </button>
              <button onClick={() => { finishUpload(impactResult.req_id) }}
                className="btn-primary text-sm flex items-center gap-1.5">
                <CheckCircle2 className="w-4 h-4" />
                确认并查看需求
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 需求表格 */}
      <div className="card p-0 overflow-hidden">
        {loading ? (
          <div className="p-6 space-y-4">{[1,2,3,4,5].map(i => <div key={i} className="skeleton h-14" />)}</div>
        ) : reqs.length === 0 ? (
          <div className="text-center py-16">
            <FileText className="w-12 h-12 mx-auto mb-3 text-slate-300" />
            <p className="text-slate-500 font-medium">{selectedProductId ? '该产品下暂无需求' : '暂无需求'}</p>
            <p className="text-sm text-slate-400 mt-1">选择产品或上传第一个需求</p>
          </div>
        ) : (
          <table className="w-full">
            <thead>
              <tr className="border-b border-slate-100 bg-slate-50/50">
                <th className="text-left text-xs font-semibold text-slate-500 uppercase tracking-wider px-5 py-3.5">ID</th>
                <th className="text-left text-xs font-semibold text-slate-500 uppercase tracking-wider px-5 py-3.5">标题</th>
                <th className="text-left text-xs font-semibold text-slate-500 uppercase tracking-wider px-5 py-3.5">模块</th>
                <th className="text-left text-xs font-semibold text-slate-500 uppercase tracking-wider px-5 py-3.5">来源格式</th>
                <th className="text-left text-xs font-semibold text-slate-500 uppercase tracking-wider px-5 py-3.5">产品</th>
                <th className="text-center text-xs font-semibold text-slate-500 uppercase tracking-wider px-5 py-3.5">优先级</th>
                <th className="text-center text-xs font-semibold text-slate-500 uppercase tracking-wider px-5 py-3.5">状态</th>
                <th className="text-right text-xs font-semibold text-slate-500 uppercase tracking-wider px-5 py-3.5">创建时间</th>
                <th className="text-right px-5 py-3.5 w-24"></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-50">
              {reqs.map(r => (
                <tr key={r.id} className="hover:bg-slate-50/50 transition-colors cursor-pointer" onClick={() => navigate(`/requirements/${r.id}`)}>
                  <td className="px-5 py-3.5"><span className="text-xs font-mono text-slate-400">{r.id}</span></td>
                  <td className="px-5 py-3.5"><p className="text-sm font-medium text-slate-700 truncate max-w-[200px]">{r.title}</p></td>
                  <td className="px-5 py-3.5">
                    <div className="flex flex-wrap gap-1 max-w-[160px]">
                      {Array.isArray(r.modules) && r.modules.length > 0
                        ? (r.modules as string[]).map((mid: string) => (
                            <span key={mid} className="text-[10px] px-1.5 py-0.5 bg-slate-100 text-slate-500 rounded font-mono truncate max-w-[80px]" title={mid}>
                              {modules.find(m => m.id === mid)?.name || mid}
                            </span>
                          ))
                        : <span className="text-xs text-slate-300">—</span>
                      }
                    </div>
                  </td>
                  <td className="px-5 py-3.5">
                    <span className="inline-flex items-center gap-1 text-xs text-slate-500">
                      {r.source_type === 'markdown' ? <FileCode className="w-3.5 h-3.5 text-blue-500" /> :
                       r.source_type === 'docx' ? <FileType className="w-3.5 h-3.5 text-orange-500" /> :
                       <FileText className="w-3.5 h-3.5 text-slate-400" />}
                      {sourceTypeLabel(r.source_type)}
                    </span>
                  </td>
                  <td className="px-5 py-3.5"><span className="text-xs text-slate-400">{(products.find(p => p.id === r.product_id)?.name) || r.product_id}</span></td>
                  <td className="px-5 py-3.5 text-center"><span className={priorityBadge(r.priority)}>{r.priority}</span></td>
                  <td className="px-5 py-3.5 text-center">
                    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium ${
                      r.status === 'draft' ? 'bg-yellow-50 text-yellow-700 ring-1 ring-yellow-500/30' :
                      r.status === 'active' ? 'bg-emerald-50 text-emerald-700' :
                      r.status === 'archived' ? 'bg-slate-100 text-slate-600' :
                      r.status === 'superseded' ? 'bg-amber-50 text-amber-700' :
                      r.status === 'deprecated' ? 'bg-red-50 text-red-600' :
                      'bg-slate-100 text-slate-500'
                    }`}>
                      {STATUS_LABELS[r.status] || r.status}
                      {r.status === 'draft' && <MessageCircle className="w-3 h-3 ml-1 animate-pulse" />}
                    </span>
                  </td>
                  <td className="px-5 py-3.5 text-right"><span className="text-xs text-slate-400">{formatDate(r.created_at)}</span></td>
                  <td className="px-5 py-3.5 text-right">
                    <div className="flex items-center justify-end gap-1">
                      <button onClick={e => { e.stopPropagation(); navigate(`/requirements/${r.id}`) }} className="btn-ghost p-1.5" title="查看详情">
                        <ArrowRight className="w-4 h-4" />
                      </button>
                      <button
                        onClick={e => handleDelete(e, r)}
                        disabled={deletingId === r.id}
                        className={`p-1.5 rounded-lg transition-colors ${
                          r.status === 'archived'
                            ? 'text-slate-300 cursor-not-allowed'
                            : 'text-slate-400 hover:text-red-500 hover:bg-red-50'
                        }`}
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
          <div className="flex items-center justify-between px-5 py-3 border-t border-slate-100 bg-slate-50/30">
            <span className="text-xs text-slate-400">共 {total} 条</span>
            <div className="flex items-center gap-1">
              <button disabled={page <= 1} onClick={() => setPage(page - 1)} className="btn-ghost text-xs py-1 disabled:opacity-30">上一页</button>
              <span className="text-xs text-slate-500 px-2">{page}/{totalPages}</span>
              <button disabled={page >= totalPages} onClick={() => setPage(page + 1)} className="btn-ghost text-xs py-1 disabled:opacity-30">下一页</button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

function sleep(ms: number) { return new Promise(r => setTimeout(r, ms)) }
