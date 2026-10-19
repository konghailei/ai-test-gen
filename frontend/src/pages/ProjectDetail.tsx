import { useState, useEffect, useCallback } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { ArrowLeft, FileText, TestTube, Loader2, Layers, Calendar, Archive, Upload, Info, Edit3, Trash2, RotateCcw, Search, X, Filter } from 'lucide-react'
import { products as prodApi, requirements as reqApi, testCases as caseApi } from '../api/client'
import type { Project } from '../types'
import { formatDate, priorityBadge, STATUS_LABELS } from '../utils'
import TestCases from './TestCases'
import UploadRequirementModal from '../components/UploadRequirementModal'
import { EditModal, ArchiveModal } from '../components/ProjectModals'

/**
 * 需求状态白名单（v3.2.12）
 * STATUS_LABELS 为通用状态表（含项目状态），此处仅取需求可用状态，避免下拉出现无关项。
 */
const REQ_STATUS_OPTIONS = ['draft', 'analyzing', 'pending_review', 'confirmed', 'archived', 'superseded', 'deprecated']

/**
 * 项目详情页（v3.2.8）
 *
 * 三标签结构：
 *  - 项目信息：项目元信息 + 统计（需求数/用例数）
 *  - 需求管理：需求列表 + 上传需求
 *  - 用例管理：完整测试用例页（复用 TestCases 组件，锁定当前项目）
 *
 * 顶部保持精简（仅标题行 + 标签栏），避免详情信息挤占内容区。
 */
export default function ProjectDetail() {
  const { id: projectId } = useParams<{ id: string }>()
  const navigate = useNavigate()

  const [tab, setTab] = useState<'info' | 'reqs' | 'cases'>('info')
  const [project, setProject] = useState<Project | null>(null)
  const [loadingProject, setLoadingProject] = useState(true)
  const [reqs, setReqs] = useState<any[]>([])
  const [caseCount, setCaseCount] = useState(0)
  const [loadingReqs, setLoadingReqs] = useState(false)
  const [showUpload, setShowUpload] = useState(false)
  const [error, setError] = useState('')
  // v3.2.12: 需求筛选（与用例页筛选栏风格对齐）
  const [reqSearch, setReqSearch] = useState('')
  const [filterReqStatus, setFilterReqStatus] = useState('')
  const [filterReqPriority, setFilterReqPriority] = useState('')
  // v3.2.11: 项目级操作（归档/恢复/编辑/删除）——由列表页迁入详情页
  const [editModal, setEditModal] = useState(false)
  const [archiveModal, setArchiveModal] = useState(false)
  const [saving, setSaving] = useState(false)
  const [reactivating, setReactivating] = useState(false)

  // 项目元信息
  useEffect(() => {
    if (!projectId) return
    let cancelled = false
    setLoadingProject(true)
    prodApi.getProjectDetail(projectId)
      .then((r: any) => { if (!cancelled) setProject((r?.data || r) as Project) })
      .catch((e: any) => { if (!cancelled) setError(e?.message || '加载项目失败') })
      .finally(() => { if (!cancelled) setLoadingProject(false) })
    return () => { cancelled = true }
  }, [projectId])

  // 需求列表
  const loadReqs = useCallback(async () => {
    if (!projectId) return
    setLoadingReqs(true)
    try {
      const r = await reqApi.list(1, 50, undefined, projectId)
      setReqs(r.items || [])
    } catch (e: any) {
      setError(`加载需求失败：${e?.message || '未知错误'}`)
    } finally { setLoadingReqs(false) }
  }, [projectId])

  // 用例总数（统计卡片用；仅取 total，不拉全量）
  const loadCaseCount = useCallback(async () => {
    if (!projectId) return
    try {
      const r: any = await caseApi.list(undefined, projectId, undefined, 1, 1)
      const d = r?.data || r
      setCaseCount(Number(d?.total ?? (d?.items?.length ?? 0)) || 0)
    } catch { /* 统计失败不阻断 */ }
  }, [projectId])

  useEffect(() => { loadReqs(); loadCaseCount() }, [loadReqs, loadCaseCount])

  // v3.2.11: 刷新项目元信息（编辑/归档/恢复后）
  const refreshProject = useCallback(async () => {
    if (!projectId) return
    try {
      const r: any = await prodApi.getProjectDetail(projectId)
      setProject(r?.data || r)
    } catch { /* ignore */ }
  }, [projectId])

  const handleEditSave = async (name: string, description: string, version?: string) => {
    setSaving(true)
    try {
      await prodApi.updateProject(projectId!, name, description, version || '1.0')
      setEditModal(false)
      await refreshProject()
    } catch (e: any) {
      alert('保存失败：' + (e?.message || '未知错误'))
    } finally { setSaving(false) }
  }

  const handleDelete = async () => {
    if (!project) return
    if (!confirm(`确认删除项目「${project.name}」？此操作不可恢复，项目下所有需求与用例也将被清除。`)) return
    try {
      await prodApi.deleteProject(projectId!)
      navigate('/projects')
    } catch (e: any) {
      alert('删除失败：' + (e?.message || '未知错误'))
    }
  }

  const handleReactivate = async () => {
    if (!project) return
    if (!confirm(`确认恢复项目「${project.name}」为活动状态？将从知识库删除该项目条目。`)) return
    setReactivating(true)
    try {
      await prodApi.reactivateProject(projectId!)
      await refreshProject()
    } catch (e: any) {
      alert('恢复失败：' + (e?.message || '未知错误'))
    } finally { setReactivating(false) }
  }

  // v3.2.12: 需求筛选（标题搜索 + 状态 + 优先级）
  const filteredReqs = reqs.filter(r => {
    if (filterReqStatus && (r.status || '') !== filterReqStatus) return false
    if (filterReqPriority && (r.priority || '') !== filterReqPriority) return false
    if (reqSearch.trim()) {
      const q = reqSearch.toLowerCase()
      const title = (r.title || r.id || '').toLowerCase()
      if (!title.includes(q)) return false
    }
    return true
  })

  const statusInfo = (s?: string) => {
    if (s === 'archived') return { text: '已归档', cls: 'bg-slate-100 text-slate-600' }
    return { text: '进行中', cls: 'bg-green-100 text-green-700' }
  }
  const versionText = (v?: string) => (/^v/i.test(v || '') ? v : `v${v}`)

  if (loadingProject) {
    return (
      <div className="flex items-center justify-center py-20">
        <Loader2 className="w-5 h-5 animate-spin mr-2" style={{ color: 'var(--foreground-tertiary)' }} />
        <span className="text-sm" style={{ color: 'var(--foreground-tertiary)' }}>加载项目信息...</span>
      </div>
    )
  }

  if (error || !project) {
    return (
      <div className="max-w-3xl mx-auto py-16 text-center">
        <p className="text-sm mb-3" style={{ color: 'var(--danger, #dc2626)' }}>{error || '项目不存在'}</p>
        <button className="btn-secondary" onClick={() => navigate('/projects')}>返回项目管理</button>
      </div>
    )
  }

  const st = statusInfo(project.status)

  return (
    <div className="space-y-6">
      {/* 返回 + 精简标题行（详情信息收进"项目信息"标签，避免挤占空间） */}
      <div className="flex items-center gap-3">
        <button
          className="flex items-center gap-1.5 text-sm transition-colors hover:opacity-70"
          style={{ color: 'var(--foreground-tertiary)' }}
          onClick={() => navigate('/projects')}
        >
          <ArrowLeft className="w-4 h-4" /> 返回
        </button>
        <span style={{ color: 'var(--border)' }}>|</span>
        <h1 className="text-lg font-semibold m-0 truncate" style={{ color: 'var(--foreground)' }}>{project.name}</h1>
        <span className="text-xs font-mono px-2 py-0.5 rounded shrink-0" style={{ background: 'var(--background-muted)', color: 'var(--foreground-tertiary)' }}>
          {versionText(project.version)}
        </span>
        <span className={`text-xs px-2 py-0.5 rounded shrink-0 ${st.cls}`}>{st.text}</span>
      </div>

      {/* 三标签栏（弧形分段卡片样式，与系统圆角风格一致） */}
      <div className="inline-flex items-center gap-1 p-1" style={{ background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.7)' }}>
        {([
          ['info', '项目信息', Info, null],
          ['reqs', '需求管理', FileText, reqs.length],
          ['cases', '用例管理', TestTube, caseCount],
        ] as const).map(([key, label, Icon, count]) => (
          <button
            key={key}
            className="flex items-center gap-1.5 px-4 py-2 text-sm font-medium transition-all duration-150"
            style={tab === key
              ? { background: 'var(--background)', color: 'var(--brand-700)', borderRadius: 'calc(var(--radius) * 0.5)', boxShadow: 'var(--shadow-apple-sm)' }
              : { color: 'var(--foreground-tertiary)', borderRadius: 'calc(var(--radius) * 0.5)' }}
            onClick={() => setTab(key)}
          >
            <Icon className="w-4 h-4" />
            {label}
            {count != null && (
              <span className="ml-1 px-1.5 py-0.5 text-[11px] rounded-full font-mono"
                style={{ background: tab === key ? 'var(--brand-100)' : 'var(--background)', color: tab === key ? 'var(--brand-700)' : 'var(--foreground-tertiary)' }}>
                {count}
              </span>
            )}
          </button>
        ))}
      </div>

      {/* ===== 项目信息 ===== */}
      {tab === 'info' && (
        <div className="space-y-4">
          {/* 统计卡片 */}
          <div className="grid grid-cols-2 gap-4">
            <div className="card p-4">
              <div className="text-xs mb-1" style={{ color: 'var(--foreground-tertiary)' }}>需求数量</div>
              <div className="text-2xl font-semibold font-mono" style={{ color: 'var(--foreground)' }}>{reqs.length}</div>
            </div>
            <div className="card p-4">
              <div className="text-xs mb-1" style={{ color: 'var(--foreground-tertiary)' }}>用例数量</div>
              <div className="text-2xl font-semibold font-mono" style={{ color: 'var(--foreground)' }}>{caseCount}</div>
            </div>
          </div>

          {/* 项目详情 */}
          <div className="card p-5">
            <h3 className="text-sm font-semibold mb-3" style={{ color: 'var(--foreground)' }}>项目详情</h3>
            <dl className="grid grid-cols-2 gap-x-6 gap-y-3 m-0">
              <div>
                <dt className="text-xs mb-0.5" style={{ color: 'var(--foreground-tertiary)' }}>项目名称</dt>
                <dd className="text-sm m-0" style={{ color: 'var(--foreground-secondary)' }}>{project.name}</dd>
              </div>
              <div>
                <dt className="text-xs mb-0.5" style={{ color: 'var(--foreground-tertiary)' }}>版本</dt>
                <dd className="text-sm m-0 font-mono" style={{ color: 'var(--foreground-secondary)' }}>{versionText(project.version)}</dd>
              </div>
              <div>
                <dt className="text-xs mb-0.5" style={{ color: 'var(--foreground-tertiary)' }}>状态</dt>
                <dd className="text-sm m-0"><span className={`text-xs px-2 py-0.5 rounded ${st.cls}`}>{st.text}</span></dd>
              </div>
              <div>
                <dt className="text-xs mb-0.5" style={{ color: 'var(--foreground-tertiary)' }}>所属产品</dt>
                <dd className="text-sm m-0 font-mono flex items-center gap-1" style={{ color: 'var(--foreground-secondary)' }}>
                  <Layers className="w-3.5 h-3.5" />{project.product_id}
                </dd>
              </div>
              <div>
                <dt className="text-xs mb-0.5" style={{ color: 'var(--foreground-tertiary)' }}>创建时间</dt>
                <dd className="text-sm m-0 flex items-center gap-1" style={{ color: 'var(--foreground-secondary)' }}>
                  <Calendar className="w-3.5 h-3.5" />{formatDate(project.created_at)}
                </dd>
              </div>
              <div>
                <dt className="text-xs mb-0.5" style={{ color: 'var(--foreground-tertiary)' }}>上线时间</dt>
                <dd className="text-sm m-0" style={{ color: 'var(--foreground-secondary)' }}>{project.go_live_date ? formatDate(project.go_live_date) : '—'}</dd>
              </div>
              {project.archived_at && (
                <div>
                  <dt className="text-xs mb-0.5" style={{ color: 'var(--foreground-tertiary)' }}>归档时间</dt>
                  <dd className="text-sm m-0 flex items-center gap-1" style={{ color: 'var(--foreground-secondary)' }}>
                    <Archive className="w-3.5 h-3.5" />{formatDate(project.archived_at)}
                  </dd>
                </div>
              )}
              {project.archived_by && (
                <div>
                  <dt className="text-xs mb-0.5" style={{ color: 'var(--foreground-tertiary)' }}>归档人</dt>
                  <dd className="text-sm m-0" style={{ color: 'var(--foreground-secondary)' }}>{project.archived_by}</dd>
                </div>
              )}
              <div className="col-span-2">
                <dt className="text-xs mb-0.5" style={{ color: 'var(--foreground-tertiary)' }}>描述</dt>
                <dd className="text-sm m-0 whitespace-pre-wrap" style={{ color: 'var(--foreground-secondary)' }}>{project.description || '—'}</dd>
              </div>
            </dl>
          </div>

          {/* v3.2.11: 项目管理操作（由列表页迁入，含"恢复活动"） */}
          <div className="card p-5">
            <h3 className="text-sm font-semibold mb-3" style={{ color: 'var(--foreground)' }}>项目管理</h3>
            <div className="flex flex-wrap items-center gap-2">
              {project.status === 'active' && (
                <>
                  <button onClick={() => setEditModal(true)} className="btn-secondary text-sm flex items-center gap-1.5">
                    <Edit3 className="w-4 h-4" /> 编辑项目
                  </button>
                  <button onClick={() => setArchiveModal(true)} className="btn-secondary text-sm flex items-center gap-1.5">
                    <Archive className="w-4 h-4" /> 归档项目
                  </button>
                </>
              )}
              {project.status === 'archived' && (
                <button onClick={handleReactivate} disabled={reactivating} className="btn-secondary text-sm flex items-center gap-1.5">
                  {reactivating ? <Loader2 className="w-4 h-4 animate-spin" /> : <RotateCcw className="w-4 h-4" />} 恢复活动
                </button>
              )}
              <button onClick={handleDelete} className="btn-secondary text-sm flex items-center gap-1.5" style={{ color: 'var(--state-error)' }}>
                <Trash2 className="w-4 h-4" /> 删除项目
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ===== 需求管理（v3.2.12: 筛选栏 + 内容卡片，与用例页风格对齐） ===== */}
      {tab === 'reqs' && (
        <div className="space-y-6">
          {/* 筛选栏卡片：统计 | 搜索 | 状态 | 优先级 | 重置 | 上传需求(靠右) */}
          <div className="card py-3 px-5">
            <div className="flex items-center gap-3 flex-wrap">
              <Filter className="w-4 h-4 shrink-0" style={{ color: 'var(--foreground-tertiary)' }} />

              {/* 统计 */}
              <span className="text-sm shrink-0" style={{ color: 'var(--foreground-tertiary)' }}>
                共 <span className="font-mono font-medium" style={{ color: 'var(--foreground-secondary)' }}>{filteredReqs.length}</span> 条需求
              </span>

              {/* 标题搜索 */}
              <div className="flex items-center min-w-[200px] flex-1 max-w-xs" style={{ background: 'var(--background)', border: '1px solid var(--border)', borderRadius: 'calc(var(--radius) * 0.5)' }}>
                <Search className="w-3.5 h-3.5 ml-2.5" style={{ color: 'var(--foreground-tertiary)' }} />
                <input value={reqSearch} onChange={e => setReqSearch(e.target.value)}
                  placeholder="搜索需求标题..." className="w-full px-2.5 py-2 text-sm outline-none bg-transparent" />
                {reqSearch && (
                  <button onClick={() => setReqSearch('')}
                    className="px-2 hover:text-[var(--foreground-secondary)]" style={{ color: 'var(--foreground-tertiary)' }}><X className="w-3.5 h-3.5" /></button>
                )}
              </div>

              {/* 状态 */}
              <select value={filterReqStatus} onChange={e => setFilterReqStatus(e.target.value)}
                className="select-field text-sm w-auto" title="按状态筛选">
                <option value="">全部状态</option>
                {REQ_STATUS_OPTIONS.map(s => <option key={s} value={s}>{STATUS_LABELS[s] || s}</option>)}
              </select>

              {/* 优先级 */}
              <select value={filterReqPriority} onChange={e => setFilterReqPriority(e.target.value)}
                className="select-field text-sm w-auto" title="按优先级筛选">
                <option value="">全部优先级</option>
                <option value="P0">P0</option>
                <option value="P1">P1</option>
                <option value="P2">P2</option>
                <option value="P3">P3</option>
              </select>

              {/* 重置 */}
              <button
                onClick={() => { setReqSearch(''); setFilterReqStatus(''); setFilterReqPriority('') }}
                className="btn-secondary text-xs whitespace-nowrap flex items-center gap-1"
                title="清空搜索与筛选条件"
              >
                <RotateCcw className="w-3 h-3" /> 重置
              </button>

              {/* 上传需求（与用例页操作按钮一致，靠右同行） */}
              <button className="btn-primary flex items-center gap-2 ml-auto shrink-0" onClick={() => setShowUpload(true)}>
                <Upload className="w-4 h-4" />上传需求
              </button>
            </div>
          </div>

          {/* 内容卡片：与用例页容器风格一致（card 包裹 + 内嵌圆角表格） */}
          <div className="card">
            <div className="overflow-hidden" style={{ border: '1px solid var(--border)', borderRadius: 'calc(var(--radius) * 0.6)' }}>
              {loadingReqs ? (
                <div className="flex items-center justify-center py-12">
                  <Loader2 className="w-4 h-4 animate-spin mr-2" style={{ color: 'var(--foreground-tertiary)' }} />
                  <span className="text-sm" style={{ color: 'var(--foreground-tertiary)' }}>加载需求列表...</span>
                </div>
              ) : reqs.length === 0 ? (
                <div className="text-center py-12">
                  <FileText className="w-8 h-8 mx-auto mb-2" style={{ color: 'var(--foreground-quaternary)' }} />
                  <p className="text-sm m-0 mb-3" style={{ color: 'var(--foreground-tertiary)' }}>该项目下暂无需求</p>
                  <button className="btn-secondary text-sm" onClick={() => setShowUpload(true)}>上传第一个需求</button>
                </div>
              ) : filteredReqs.length === 0 ? (
                <div className="text-center py-12">
                  <Search className="w-8 h-8 mx-auto mb-2" style={{ color: 'var(--foreground-quaternary)' }} />
                  <p className="text-sm m-0" style={{ color: 'var(--foreground-tertiary)' }}>没有符合筛选条件的需求</p>
                </div>
              ) : (
                <table className="w-full">
                  <thead>
                    <tr style={{ background: 'var(--background-muted)' }}>
                      <th className="text-left text-[11px] font-semibold uppercase tracking-wider px-5 py-2.5" style={{ color: 'var(--foreground-tertiary)' }}>需求标题</th>
                      <th className="text-center text-[11px] font-semibold uppercase tracking-wider px-3 py-2.5 w-20" style={{ color: 'var(--foreground-tertiary)' }}>优先级</th>
                      <th className="text-center text-[11px] font-semibold uppercase tracking-wider px-3 py-2.5 w-28" style={{ color: 'var(--foreground-tertiary)' }}>状态</th>
                      <th className="text-right text-[11px] font-semibold uppercase tracking-wider px-5 py-2.5 w-36" style={{ color: 'var(--foreground-tertiary)' }}>创建时间</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredReqs.map(req => (
                      <tr key={req.id} className="transition-colors duration-150 cursor-pointer hover:bg-slate-50"
                        style={{ borderTop: '1px solid var(--border)' }}
                        onClick={() => navigate(`/requirements/${encodeURIComponent(req.id)}`)}>
                        <td className="px-5 py-3">
                          <div className="flex items-center gap-2 min-w-0">
                            <FileText className="w-3.5 h-3.5 flex-shrink-0" style={{ color: 'var(--foreground-tertiary)' }} />
                            <span className="text-sm font-medium truncate" style={{ color: 'var(--foreground-secondary)' }} title={req.title}>
                              {req.title || req.id}
                            </span>
                          </div>
                        </td>
                        <td className="px-3 py-3 text-center">
                          <span className={`text-xs font-semibold font-mono ${priorityBadge(req.priority)}`}>{req.priority}</span>
                        </td>
                        <td className="px-3 py-3 text-center">
                          <span className="text-[11px] px-2 py-0.5 rounded-full" style={{ background: 'var(--background-muted)', color: 'var(--foreground-secondary)' }}>
                            {STATUS_LABELS[req.status] || req.status || '-'}
                          </span>
                        </td>
                        <td className="px-5 py-3 text-right text-xs" style={{ color: 'var(--foreground-tertiary)' }}>
                          {formatDate(req.created_at)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </div>
        </div>
      )}

      {/* ===== 用例管理（复用完整测试用例页，锁定当前项目） ===== */}
      {tab === 'cases' && (
        <TestCases fixedProjectId={projectId} embedded />
      )}

      {/* v3.2.11: 项目编辑/归档弹窗 */}
      {editModal && (
        <EditModal
          name={project.name}
          description={project.description}
          version={project.version}
          saving={saving}
          onSave={handleEditSave}
          onClose={() => setEditModal(false)}
        />
      )}
      {archiveModal && (
        <ArchiveModal
          projId={project.id}
          projName={project.name}
          onClose={() => setArchiveModal(false)}
          onArchived={() => { setArchiveModal(false); refreshProject(); loadReqs() }}
        />
      )}

      {/* 上传需求弹窗 */}
      <UploadRequirementModal
        open={showUpload}
        onClose={() => setShowUpload(false)}
        productId={project.product_id}
        preselectedProjectId={projectId}
        onUploaded={() => { setShowUpload(false); loadReqs(); loadCaseCount() }}
      />
    </div>
  )
}
