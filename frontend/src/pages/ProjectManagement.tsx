import { useState, useEffect, useMemo, useCallback } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  Plus, FolderOpen, Loader2, Search,
  RotateCcw, ChevronRight,
} from 'lucide-react'
import { products as prodApi } from '../api/client'
import { useProduct } from '../store/ProductContext'
import type { Project } from '../types'
import UploadRequirementModal from '../components/UploadRequirementModal'

// v2.45 修复：本地时区日期格式化，避免 slice(0,10) 在带时区 ISO 字符串上截取到 UTC 日期
function formatDateOnly(iso: string | undefined | null): string {
  if (!iso) return ''
  try {
    const d = new Date(iso)
    if (isNaN(d.getTime())) return String(iso).slice(0, 10)
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  } catch {
    return String(iso).slice(0, 10)
  }
}

// 项目状态标签配置（二状态：active → archived）
// v2.8.53: 对齐设计稿配色 — 活跃使用 emerald 绿色系，归档使用 slate 灰色系
const PROJECT_STATUS_STYLES: Record<string, { label: string; bg: string; text: string; iconBg: string; iconColor: string }> = {
  active: { label: '活跃', bg: 'bg-emerald-50', text: 'text-emerald-700', iconBg: 'bg-blue-50', iconColor: 'text-blue-500' },
  archived: { label: '已归档', bg: 'bg-slate-100', text: 'text-slate-500', iconBg: 'bg-slate-100', iconColor: 'text-slate-400' },
}

/** 添加表单 */
function AddForm({ onAdd, onCancel, adding }: {
  onAdd: (name: string, desc: string, version: string) => Promise<void>;
  onCancel: () => void; adding: boolean
}) {
  const [name, setName] = useState('')
  const [desc, setDesc] = useState('')
  const [version, setVersion] = useState('1.0')
  return (
    <div className="flex items-start gap-2 mb-4 p-3" style={{ background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.6)' }}>
      <span className="badge text-xs shrink-0 mt-1" style={{ background: 'rgba(0, 122, 255, 0.12)', color: 'var(--brand-500)' }}>项目</span>
      <div className="flex-1 space-y-2">
        <input value={name} onChange={e => setName(e.target.value)} className="input-field text-sm py-1.5" placeholder="项目名称" autoFocus />
        <input value={desc} onChange={e => setDesc(e.target.value)} className="input-field text-sm py-1.5" placeholder="描述（可选）" />
        <input value={version} onChange={e => setVersion(e.target.value)} className="input-field text-sm py-1.5" placeholder="版本号，如 1.0、2.1.0" />
      </div>
      <div className="flex gap-1 shrink-0 mt-1">
        <button onClick={() => onAdd(name, desc, version)} disabled={!name.trim() || adding} className="btn-primary text-xs py-1.5">确定</button>
        <button onClick={onCancel} className="btn-ghost text-xs py-1.5">取消</button>
      </div>
    </div>
  )
}

export default function ProjectManagementPage() {
  const { products, selectedProductId, selectedProduct } = useProduct()
  const navigate = useNavigate()
  // 读取 URL query 参数（支持 ?upload=true&project_id=xxx 直接打开上传弹窗）
  const [searchParams, setSearchParams] = useSearchParams()

  const [projects, setProjects] = useState<Record<string, Project[]>>({})
  const [loading, setLoading] = useState(false)
  const [search, setSearch] = useState('')
  const [showAdd, setShowAdd] = useState(false)
  const [adding, setAdding] = useState(false)
  const [activeProductId, setActiveProductId] = useState(selectedProductId)
  const [toast, setToast] = useState<{ type: 'success' | 'error' | 'warning'; msg: string } | null>(null)

  // v2.8.52: 条件筛选
  const [filterProjStatus, setFilterProjStatus] = useState('')   // 项目状态: ''/active/archived
  const [filterGoLiveStart, setFilterGoLiveStart] = useState('')  // v3.2.9 上线时间范围-起
  const [filterGoLiveEnd, setFilterGoLiveEnd] = useState('')      // v3.2.9 上线时间范围-止

  // 上传需求弹窗状态
  const [showUpload, setShowUpload] = useState(false)
  // 上传弹窗预选的项目 ID（从项目卡片点击「上传需求文档」时携带）
  const [uploadProjectId, setUploadProjectId] = useState('')

  useEffect(() => {
    setActiveProductId(selectedProductId)
    // v3.2.11: 切换产品时关闭 AddForm
    setShowAdd(false)
    if (selectedProductId) loadData(selectedProductId)
  }, [selectedProductId])

  // 读取 URL query：?upload=true&project_id=xxx 时自动打开上传弹窗（支持外部直接链接）
  useEffect(() => {
    const qUpload = searchParams.get('upload') === 'true'
    const qProj = searchParams.get('project_id') || ''
    if (qUpload) {
      setUploadProjectId(qProj)
      setShowUpload(true)
      // 清掉 query 参数，避免后续刷新重复打开
      setSearchParams({}, { replace: true })
    }
  }, [searchParams])

  // v3.2.11: AddForm 支持 Esc 关闭
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setShowAdd(false) }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [showAdd])

  // toast 自动消失
  useEffect(() => {
    if (!toast) return
    const t = setTimeout(() => setToast(null), 3500)
    return () => clearTimeout(t)
  }, [toast])

  const loadData = async (pid: string) => {
    if (!pid) return
    setLoading(true)
    try {
      const projs = await prodApi.listProjects(pid)
      setProjects(p => ({ ...p, [pid]: projs || [] }))
    } catch (e: any) {
      // v2.45 修复：列表加载失败时弹出 alert 提示，避免用户误以为"无项目"；
      // 导航导致的主动 abort / 登录过期跳转（v3.1.2）属正常流程，不弹窗
      if (!e?._aborted && !e?._unauthorized) alert('加载项目列表失败：' + (e?.message || '网络错误'))
    } finally { setLoading(false) }
  }






  const handleAdd = async (name: string, desc: string, version: string) => {
    if (!activeProductId || !name.trim()) return
    setAdding(true)
    try {
      await prodApi.createProject(activeProductId, name.trim(), desc.trim(), version.trim() || '1.0')
      await loadData(activeProductId)
      setShowAdd(false)
    } catch (e: any) { alert('创建失败: ' + e.message) } finally { setAdding(false) }
  }



  // v2.46 切换项目展开/收起
  // v3.2.7: 点击项目卡片不再原地展开，改为跳转项目详情页（含需求/用例双标签）
  const toggleExpand = (projectId: string) => {
    navigate(`/projects/${encodeURIComponent(projectId)}`)
  }

  // v3.2.10 项目搜索（仅项目名称/版本）+ 条件筛选
  const filtered = useMemo(() => {
    const all = projects[activeProductId || ''] || []
    let result = all
    // 项目状态筛选
    if (filterProjStatus) {
      result = result.filter(p => (p.status || 'active') === filterProjStatus)
    }
    // 搜索关键词（仅项目名称与版本，不含需求标题）
    if (search.trim()) {
      const q = search.toLowerCase()
      result = result.filter(p =>
        p.name.toLowerCase().includes(q) ||
        (p.version || '').toLowerCase().includes(q)
      )
    }
    // v3.2.9 上线时间范围筛选（按 YYYY-MM-DD 比较，未设上线时间的项目在启用范围筛选时排除）
    if (filterGoLiveStart || filterGoLiveEnd) {
      result = result.filter(p => {
        const d = (p.go_live_date || '').slice(0, 10)
        if (!d) return false
        if (filterGoLiveStart && d < filterGoLiveStart) return false
        if (filterGoLiveEnd && d > filterGoLiveEnd) return false
        return true
      })
    }
    return result
  }, [projects, activeProductId, search, filterProjStatus, filterGoLiveStart, filterGoLiveEnd])

  return (
    <div className="space-y-6">
      {/* toast 通知 */}
      {toast && (
        <div
          className="fixed top-4 right-4 z-50 px-4 py-3 text-sm shadow-lg animate-slide-up"
          style={{
            background: 'var(--background)',
            borderRadius: 'calc(var(--radius) * 0.6)',
            borderLeft: `3px solid var(--state-${toast.type === 'success' ? 'success' : toast.type === 'warning' ? 'warning' : 'error'})`,
            boxShadow: 'var(--shadow-apple-md)',
            color: 'var(--foreground)',
            maxWidth: '420px',
          }}
        >
          {toast.msg}
        </div>
      )}

      <div className="flex items-center justify-between">
        <div>
          <h1 className="page-title flex items-center gap-2">
            <FolderOpen className="w-6 h-6" style={{ color: 'var(--brand-500)' }} />
            项目管理
          </h1>
          <p className="text-sm mt-1" style={{ color: 'var(--foreground-tertiary)' }}>
            管理产品下的项目与需求 — 点击项目进入详情页查看需求与用例，支持按状态/优先级筛选
          </p>
        </div>
      </div>

      {!activeProductId ? (
        <div className="text-center py-16" style={{ color: 'var(--foreground-tertiary)' }}>
          <FolderOpen className="w-12 h-12 mx-auto mb-3 opacity-50" />
          <p className="font-medium">请先选择一个产品</p>
          <p className="text-sm mt-1">选择产品后查看和管理其项目</p>
        </div>
      ) : (
        <div className="card">
          <div className="flex items-center justify-between mb-4">
            <h2 className="section-title flex items-center gap-2">
              <FolderOpen className="w-5 h-5" style={{ color: 'var(--brand-500)' }} />
              {selectedProduct?.name || activeProductId}
              <span className="text-sm font-normal" style={{ color: 'var(--foreground-tertiary)' }}>— 项目列表</span>
            </h2>
          </div>

          <div className="flex items-center gap-2 mb-4 flex-wrap">
            <div className="relative flex-1 min-w-[200px]">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4" style={{ color: 'var(--foreground-tertiary)' }} />
              <input
                value={search}
                onChange={e => setSearch(e.target.value)}
                className="input-field text-sm py-1.5 pl-9"
                placeholder="搜索项目名称或版本..."
              />
            </div>
            {/* v2.8.52: 项目状态筛选 */}
            <select
              value={filterProjStatus}
              onChange={e => setFilterProjStatus(e.target.value)}
              className="input-field text-sm py-1.5 w-auto"
              title="筛选项目状态"
            >
              <option value="">全部状态</option>
              <option value="active">活跃</option>
              <option value="archived">已归档</option>
            </select>
            {/* v3.2.9: 上线时间范围检索 */}
            <div className="flex items-center gap-1.5">
              <span className="text-xs whitespace-nowrap" style={{ color: 'var(--foreground-tertiary)' }}>上线时间</span>
              <input
                type="date"
                value={filterGoLiveStart}
                onChange={e => setFilterGoLiveStart(e.target.value)}
                className="input-field text-sm py-1.5 w-auto"
                title="上线时间（起）"
              />
              <span className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>~</span>
              <input
                type="date"
                value={filterGoLiveEnd}
                onChange={e => setFilterGoLiveEnd(e.target.value)}
                className="input-field text-sm py-1.5 w-auto"
                title="上线时间（止）"
              />
            </div>
            {/* v3.2.10: 重置搜索与筛选条件 */}
            <button
              onClick={() => { setSearch(''); setFilterProjStatus(''); setFilterGoLiveStart(''); setFilterGoLiveEnd('') }}
              className="btn-secondary text-xs whitespace-nowrap flex items-center gap-1"
              title="清空搜索与筛选条件"
            >
              <RotateCcw className="w-3 h-3" /> 重置
            </button>
            <button onClick={() => setShowAdd(true)} className="btn-primary text-xs whitespace-nowrap">
              <Plus className="w-3 h-3" /> 添加项目
            </button>
          </div>

          {showAdd && (
            <AddForm onAdd={handleAdd} onCancel={() => setShowAdd(false)} adding={adding} />
          )}

          {loading ? (
            <div className="flex items-center justify-center py-10"><Loader2 className="w-6 h-6 animate-spin" style={{ color: 'var(--foreground-tertiary)' }} /></div>
          ) : filtered.length === 0 ? (
            <p className="text-xs py-8 text-center" style={{ background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.6)', color: 'var(--foreground-tertiary)' }}>
              {search ? '未找到匹配的项目或需求' : '暂无项目'}
            </p>
          ) : (
            <div className="overflow-hidden" style={{ border: '1px solid var(--border)', borderRadius: 'calc(var(--radius) * 0.6)' }}>
              <table className="w-full">
                <thead>
                  <tr style={{ background: 'var(--background-muted)' }}>
                    <th className="text-left text-[11px] font-semibold uppercase tracking-wider px-4 py-2.5" style={{ color: 'var(--foreground-tertiary)' }}>项目名称</th>
                    <th className="text-center text-[11px] font-semibold uppercase tracking-wider px-3 py-2.5 w-24" style={{ color: 'var(--foreground-tertiary)' }}>版本</th>
                    <th className="text-center text-[11px] font-semibold uppercase tracking-wider px-3 py-2.5 w-24" style={{ color: 'var(--foreground-tertiary)' }}>状态</th>
                    <th className="text-center text-[11px] font-semibold uppercase tracking-wider px-3 py-2.5 w-36" style={{ color: 'var(--foreground-tertiary)' }}>上线时间</th>
                    <th className="text-right text-[11px] font-semibold uppercase tracking-wider px-4 py-2.5 w-40" style={{ color: 'var(--foreground-tertiary)' }}>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map(proj => {
                    const statusStyle = PROJECT_STATUS_STYLES[proj.status || 'active'] || PROJECT_STATUS_STYLES.active
                    return (
                      <tr
                        key={proj.id}
                        className="transition-colors duration-150 cursor-pointer hover:bg-slate-50"
                        style={{ borderTop: '1px solid var(--border)' }}
                        onClick={() => toggleExpand(proj.id)}
                      >
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-2 min-w-0">
                            <div className={`w-6 h-6 rounded-md flex items-center justify-center flex-shrink-0 ${statusStyle.iconBg}`}>
                              <FolderOpen className={`w-3.5 h-3.5 ${statusStyle.iconColor}`} />
                            </div>
                            <span className="text-sm font-medium truncate" style={{ color: 'var(--foreground-secondary)' }}>{proj.name}</span>
                          </div>
                        </td>
                        <td className="px-3 py-3 text-center">
                          {proj.version ? (
                            <span className="text-[11px] px-2 py-0.5 font-mono font-medium" style={{ background: 'rgba(0, 122, 255, 0.08)', color: 'var(--brand-500)', borderRadius: 'calc(var(--radius) * 0.25)' }}>
                              v{proj.version}
                            </span>
                          ) : <span className="text-xs" style={{ color: 'var(--foreground-quaternary)' }}>—</span>}
                        </td>
                        <td className="px-3 py-3 text-center">
                          <span className={`text-[11px] px-2 py-0.5 font-medium ${statusStyle.bg} ${statusStyle.text}`} style={{ borderRadius: '9999px' }}>
                            {statusStyle.label}
                          </span>
                        </td>
                        <td className="px-3 py-3 text-center text-xs" style={{ color: 'var(--foreground-tertiary)' }}>
                          {proj.go_live_date ? formatDateOnly(proj.go_live_date) : '—'}
                        </td>
                        <td className="px-4 py-3">
                          <div className="flex items-center justify-end">
                            {/* v3.2.11: 操作列精简为单一"详情"入口，项目级操作（归档/恢复/编辑/删除）移至项目详情页 */}
                            <button
                              onClick={(e) => { e.stopPropagation(); navigate(`/projects/${encodeURIComponent(proj.id)}`) }}
                              className="btn-secondary text-xs py-1.5 px-3 flex items-center gap-1"
                              title="查看项目详情"
                            >
                              详情 <ChevronRight className="w-3 h-3" />
                            </button>
                          </div>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}



      {/* 上传需求弹窗：上传成功后刷新对应项目的需求列表 */}
      <UploadRequirementModal
        open={showUpload}
        onClose={() => setShowUpload(false)}
        productId={activeProductId}
        preselectedProjectId={uploadProjectId}
        onUploaded={(reqId) => {
          setToast({ type: 'success', msg: `需求已上传成功（${reqId}）` })
        }}
      />

    </div>
  )
}
