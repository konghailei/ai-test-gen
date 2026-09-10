import { useState, useEffect, useCallback } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { ArrowLeft, FileText, TestTube, Loader2, Archive, Layers, Calendar } from 'lucide-react'
import { products as prodApi, requirements as reqApi, testCases as caseApi } from '../api/client'
import type { Project } from '../types'
import { formatDate, priorityBadge } from '../utils'

/**
 * 项目详情页（v3.2.7）
 *
 * 从"项目卡片内折叠展开"改为独立页面，解决折叠框空间局促、交互不清的问题。
 * 双标签：需求管理 / 用例管理 —— 一个项目的完整交付物在一页内可见。
 */
export default function ProjectDetail() {
  const { id: projectId } = useParams<{ id: string }>()
  const navigate = useNavigate()

  const [tab, setTab] = useState<'reqs' | 'cases'>('reqs')
  const [project, setProject] = useState<Project | null>(null)
  const [loadingProject, setLoadingProject] = useState(true)
  const [reqs, setReqs] = useState<any[]>([])
  const [cases, setCases] = useState<any[]>([])
  const [loadingReqs, setLoadingReqs] = useState(false)
  const [loadingCases, setLoadingCases] = useState(false)
  const [error, setError] = useState('')

  // 加载项目元信息
  useEffect(() => {
    if (!projectId) return
    let cancelled = false
    setLoadingProject(true)
    prodApi.getProjectDetail(projectId)
      .then((r: any) => {
        if (cancelled) return
        setProject((r?.data || r) as Project)
      })
      .catch((e: any) => { if (!cancelled) setError(e?.message || '加载项目失败') })
      .finally(() => { if (!cancelled) setLoadingProject(false) })
    return () => { cancelled = true }
  }, [projectId])

  // 加载需求列表
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

  // 加载用例列表（切到用例标签时懒加载）
  const loadCases = useCallback(async () => {
    if (!projectId) return
    setLoadingCases(true)
    try {
      const r: any = await caseApi.list(undefined, projectId, undefined, 1, 100)
      const d = r?.data || r
      setCases(d.items || [])
    } catch (e: any) {
      setError(`加载用例失败：${e?.message || '未知错误'}`)
    } finally { setLoadingCases(false) }
  }, [projectId])

  useEffect(() => { loadReqs() }, [loadReqs])
  useEffect(() => { if (tab === 'cases' && cases.length === 0) loadCases() }, [tab, loadCases, cases.length])

  const statusLabel = (s?: string) => {
    if (s === 'archived') return { text: '已归档', cls: 'bg-slate-100 text-slate-600' }
    if (s === 'pending_archive') return { text: '待归档', cls: 'bg-amber-100 text-amber-700' }
    return { text: '进行中', cls: 'bg-green-100 text-green-700' }
  }

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

  const st = statusLabel(project.status)

  return (
    <div className="max-w-6xl mx-auto px-6 py-6">
      {/* 顶部：返回 + 项目信息卡 */}
      <button
        className="flex items-center gap-1.5 text-sm mb-4 transition-colors hover:opacity-70"
        style={{ color: 'var(--foreground-tertiary)' }}
        onClick={() => navigate('/projects')}
      >
        <ArrowLeft className="w-4 h-4" /> 返回项目管理
      </button>

      <div className="card p-5 mb-5">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="flex items-center gap-2.5 mb-1.5">
              <h1 className="text-xl font-semibold m-0" style={{ color: 'var(--foreground)' }}>{project.name}</h1>
              <span className="text-xs font-mono px-2 py-0.5 rounded" style={{ background: 'var(--background-muted)', color: 'var(--foreground-tertiary)' }}>
                {/^v/i.test(project.version || '') ? project.version : `v${project.version}`}
              </span>
              <span className={`text-xs px-2 py-0.5 rounded ${st.cls}`}>{st.text}</span>
            </div>
            {project.description && (
              <p className="text-sm m-0 mb-2" style={{ color: 'var(--foreground-secondary)' }}>{project.description}</p>
            )}
            <div className="flex items-center gap-4 text-xs" style={{ color: 'var(--foreground-tertiary)' }}>
              <span className="flex items-center gap-1"><Layers className="w-3.5 h-3.5" /> {project.product_id}</span>
              <span className="flex items-center gap-1"><Calendar className="w-3.5 h-3.5" /> 创建 {formatDate(project.created_at)}</span>
              {project.go_live_date && <span>上线 {formatDate(project.go_live_date)}</span>}
              {project.archived_at && <span className="flex items-center gap-1"><Archive className="w-3.5 h-3.5" /> 归档 {formatDate(project.archived_at)}</span>}
            </div>
          </div>
        </div>
      </div>

      {/* 双标签栏 */}
      <div className="flex items-center gap-1 mb-4" style={{ borderBottom: '1px solid var(--border)' }}>
        {([
          ['reqs', '需求管理', FileText, reqs.length],
          ['cases', '用例管理', TestTube, cases.length],
        ] as const).map(([key, label, Icon, count]) => (
          <button
            key={key}
            className="flex items-center gap-1.5 px-4 py-2.5 text-sm font-medium transition-colors duration-150"
            style={{
              borderBottom: tab === key ? '2px solid var(--brand-500)' : '2px solid transparent',
              color: tab === key ? 'var(--brand-600)' : 'var(--foreground-tertiary)',
            }}
            onClick={() => setTab(key)}
          >
            <Icon className="w-4 h-4" />
            {label}
            <span className="ml-1 px-1.5 py-0.5 text-[11px] rounded-full font-mono"
              style={{ background: 'var(--background-muted)', color: 'var(--foreground-tertiary)' }}>
              {count}
            </span>
          </button>
        ))}
        <div className="flex-1" />
        <button
          className="text-xs py-1.5 px-3 rounded transition-colors hover:bg-slate-100 mb-1"
          style={{ color: 'var(--foreground-tertiary)' }}
          onClick={() => navigate(`/testcases?project_id=${projectId}`)}
        >
          在用例管理中打开 →
        </button>
      </div>

      {/* 标签内容 */}
      <div className="card overflow-hidden">
        {tab === 'reqs' ? (
          loadingReqs ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="w-4 h-4 animate-spin mr-2" style={{ color: 'var(--foreground-tertiary)' }} />
              <span className="text-sm" style={{ color: 'var(--foreground-tertiary)' }}>加载需求列表...</span>
            </div>
          ) : reqs.length === 0 ? (
            <div className="text-center py-12">
              <FileText className="w-8 h-8 mx-auto mb-2" style={{ color: 'var(--foreground-quaternary)' }} />
              <p className="text-sm m-0" style={{ color: 'var(--foreground-tertiary)' }}>该项目下暂无需求</p>
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
          )
        ) : (
          loadingCases ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="w-4 h-4 animate-spin mr-2" style={{ color: 'var(--foreground-tertiary)' }} />
              <span className="text-sm" style={{ color: 'var(--foreground-tertiary)' }}>加载用例列表...</span>
            </div>
          ) : cases.length === 0 ? (
            <div className="text-center py-12">
              <TestTube className="w-8 h-8 mx-auto mb-2" style={{ color: 'var(--foreground-quaternary)' }} />
              <p className="text-sm m-0" style={{ color: 'var(--foreground-tertiary)' }}>该项目下暂无用例</p>
            </div>
          ) : (
            <table className="w-full">
              <thead>
                <tr style={{ background: 'var(--background-muted)' }}>
                  <th className="text-left text-[11px] font-semibold uppercase tracking-wider px-5 py-2.5" style={{ color: 'var(--foreground-tertiary)' }}>用例标题</th>
                  <th className="text-center text-[11px] font-semibold uppercase tracking-wider px-3 py-2.5 w-28" style={{ color: 'var(--foreground-tertiary)' }}>类型</th>
                  <th className="text-center text-[11px] font-semibold uppercase tracking-wider px-3 py-2.5 w-28" style={{ color: 'var(--foreground-tertiary)' }}>状态</th>
                  <th className="text-center text-[11px] font-semibold uppercase tracking-wider px-3 py-2.5 w-24" style={{ color: 'var(--foreground-tertiary)' }}>置信度</th>
                </tr>
              </thead>
              <tbody>
                {cases.map(tc => (
                  <tr key={tc.id} className="transition-colors duration-150 cursor-pointer hover:bg-slate-50"
                    style={{ borderTop: '1px solid var(--border)' }}
                    onClick={() => navigate(`/testcases?project_id=${projectId}`)}>
                    <td className="px-5 py-3">
                      <span className="text-sm block truncate max-w-xl" style={{ color: 'var(--foreground-secondary)' }} title={tc.title}>
                        {tc.title || tc.id}
                      </span>
                    </td>
                    <td className="px-3 py-3 text-center">
                      <span className="text-xs font-mono" style={{ color: 'var(--foreground-tertiary)' }}>{tc.type}</span>
                    </td>
                    <td className="px-3 py-3 text-center">
                      <span className={`text-xs font-semibold ${tc.status === 'approved' ? 'text-green-600' : 'text-amber-500'}`}>{tc.status}</span>
                    </td>
                    <td className="px-3 py-3 text-center text-xs font-mono" style={{ color: 'var(--foreground-tertiary)' }}>
                      {tc.confidence != null ? Number(tc.confidence).toFixed(3) : '-'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )
        )}
      </div>
    </div>
  )
}
