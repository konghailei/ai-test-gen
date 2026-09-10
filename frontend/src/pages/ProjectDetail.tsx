import { useState, useEffect, useCallback } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { ArrowLeft, FileText, TestTube, Loader2, Layers, Calendar, Archive, Upload, Info } from 'lucide-react'
import { products as prodApi, requirements as reqApi, testCases as caseApi } from '../api/client'
import type { Project } from '../types'
import { formatDate, priorityBadge } from '../utils'
import TestCases from './TestCases'
import UploadRequirementModal from '../components/UploadRequirementModal'

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

  const statusInfo = (s?: string) => {
    if (s === 'archived') return { text: '已归档', cls: 'bg-slate-100 text-slate-600' }
    if (s === 'pending_archive') return { text: '待归档', cls: 'bg-amber-100 text-amber-700' }
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

      {/* 三标签栏（样式对齐系统 RequirementDetail：border-b-2 + brand-700） */}
      <div className="flex items-center gap-1" style={{ borderBottom: '1px solid var(--border)' }}>
        {([
          ['info', '项目信息', Info, null],
          ['reqs', '需求管理', FileText, reqs.length],
          ['cases', '用例管理', TestTube, caseCount],
        ] as const).map(([key, label, Icon, count]) => (
          <button
            key={key}
            className="flex items-center gap-1.5 px-4 py-2.5 text-sm font-medium border-b-2 transition-all duration-150"
            style={tab === key
              ? { borderColor: 'var(--brand-500)', color: 'var(--brand-700)' }
              : { borderColor: 'transparent', color: 'var(--foreground-tertiary)' }}
            onClick={() => setTab(key)}
          >
            <Icon className="w-4 h-4" />
            {label}
            {count != null && (
              <span className="ml-1 px-1.5 py-0.5 text-[11px] rounded-full font-mono"
                style={{ background: 'var(--background-muted)', color: 'var(--foreground-tertiary)' }}>
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
        </div>
      )}

      {/* ===== 需求管理 ===== */}
      {tab === 'reqs' && (
        <div className="space-y-3">
          {/* 工具栏：需求统计 + 上传按钮 同行，避免按钮孤立 */}
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3 text-sm" style={{ color: 'var(--foreground-tertiary)' }}>
              <span>共 <span className="font-mono font-medium" style={{ color: 'var(--foreground-secondary)' }}>{reqs.length}</span> 条需求</span>
            </div>
            <button className="btn-primary flex items-center gap-2" onClick={() => setShowUpload(true)}>
              <Upload className="w-4 h-4" />上传需求
            </button>
          </div>
          <div className="card overflow-hidden">
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
                  {reqs.map(req => (
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
                        <span className="text-xs" style={{ color: 'var(--foreground-secondary)' }}>{req.status || '-'}</span>
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
      )}

      {/* ===== 用例管理（复用完整测试用例页，锁定当前项目） ===== */}
      {tab === 'cases' && (
        <TestCases fixedProjectId={projectId} embedded />
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
