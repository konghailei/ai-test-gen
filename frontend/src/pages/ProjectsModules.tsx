import { useState, useEffect, useMemo } from 'react'
import { Plus, Trash2, FolderOpen, Puzzle, Loader2, Edit3, Save, X, FolderTree, Search } from 'lucide-react'
import { products as prodApi } from '../api/client'
import { useProduct } from '../store/ProductContext'
import type { Project, Module } from '../types'

/** 编辑弹窗 */
function EditModal({
  title, name, description, version, showVersion, onSave, onClose, saving
}: {
  title: string; name: string; description: string; version?: string; showVersion?: boolean;
  onSave: (name: string, desc: string, version?: string) => Promise<void>;
  onClose: () => void; saving: boolean
}) {
  const [n, setN] = useState(name)
  const [d, setD] = useState(description)
  const [v, setV] = useState(version || '1.0')
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ background: 'rgba(0, 0, 0, 0.32)', backdropFilter: 'blur(4px)' }}
      onClick={() => !saving && onClose()}>
      <div className="max-w-md w-full p-6 animate-slide-up"
        style={{ background: 'var(--background)', borderRadius: 'calc(var(--radius) * 1.1)', boxShadow: 'var(--shadow-apple-xl)' }}
        onClick={e => e.stopPropagation()}>
        <div className="flex items-center gap-3 mb-4">
          <Edit3 className="w-5 h-5" style={{ color: 'var(--brand-500)' }} />
          <h2 className="text-lg font-semibold" style={{ color: 'var(--foreground)' }}>{title}</h2>
        </div>
        <div className="space-y-3">
          <div>
            <label className="label">名称</label>
            <input value={n} onChange={e => setN(e.target.value)}
              className="input-field text-sm" autoFocus />
          </div>
          <div>
            <label className="label">描述</label>
            <textarea value={d} onChange={e => setD(e.target.value)}
              className="input-field text-sm resize-none" rows={3} placeholder="可选" />
          </div>
          {showVersion && (
            <div>
              <label className="label">版本号</label>
              <input value={v} onChange={e => setV(e.target.value)}
                className="input-field text-sm" placeholder="如 1.0、2.1.0" />
            </div>
          )}
        </div>
        <div className="flex justify-end gap-3 mt-5">
          <button onClick={onClose} className="btn-secondary" disabled={saving}><X className="w-4 h-4" /> 取消</button>
          <button onClick={() => onSave(n, d, showVersion ? v : undefined)} disabled={!n.trim() || saving} className="btn-primary">
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />} 保存
          </button>
        </div>
      </div>
    </div>
  )
}

/** 添加表单 */
function AddForm({
  type, onAdd, onCancel, adding
}: {
  type: 'project' | 'module'; onAdd: (name: string, desc: string, version: string) => Promise<void>;
  onCancel: () => void; adding: boolean
}) {
  const [name, setName] = useState('')
  const [desc, setDesc] = useState('')
  const [version, setVersion] = useState('1.0')
  return (
    <div className="flex items-start gap-2 mb-4 p-3 rounded-xl"
      style={{ background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.75)' }}>
      <span className="badge text-xs shrink-0 mt-1"
        style={{ background: 'rgba(0, 122, 255, 0.12)', color: 'var(--brand-500)' }}>
        {type === 'project' ? '项目' : '模块'}
      </span>
      <div className="flex-1 space-y-2">
        <input value={name} onChange={e => setName(e.target.value)}
          className="input-field text-sm py-1.5"
          placeholder={type === 'project' ? '项目名称' : '模块名称'} autoFocus />
        <input value={desc} onChange={e => setDesc(e.target.value)}
          className="input-field text-sm py-1.5" placeholder="描述（可选）" />
        {type === 'project' && (
          <input value={version} onChange={e => setVersion(e.target.value)}
            className="input-field text-sm py-1.5" placeholder="版本号，如 1.0、2.1.0" />
        )}
      </div>
      <div className="flex gap-1 shrink-0 mt-1">
        <button onClick={() => onAdd(name, desc, version)} disabled={!name.trim() || adding}
          className="btn-primary text-xs py-1.5">确定</button>
        <button onClick={onCancel} className="btn-ghost text-xs py-1.5">取消</button>
      </div>
    </div>
  )
}

export default function ProjectsModulesPage() {
  const { products, selectedProductId, selectedProduct } = useProduct()

  const [projects, setProjects] = useState<Record<string, Project[]>>({})
  const [modules, setModules] = useState<Record<string, Module[]>>({})
  const [loading, setLoading] = useState(false)

  // 标签页：'project' | 'module'
  const [activeTab, setActiveTab] = useState<'project' | 'module'>('project')

  // 搜索（两个 Tab 各自独立）
  const [projectSearch, setProjectSearch] = useState('')
  const [moduleSearch, setModuleSearch] = useState('')

  // 添加状态
  const [addTarget, setAddTarget] = useState<{ pid: string; type: 'project' | 'module' } | null>(null)
  const [adding, setAdding] = useState(false)

  // 编辑
  const [editModal, setEditModal] = useState<{
    type: 'project' | 'module'; id: string; name: string; description: string; version?: string
  } | null>(null)
  const [saving, setSaving] = useState(false)

  // 使用全局产品选择（顶部导航栏）
  const [activeProductId, setActiveProductId] = useState(selectedProductId)

  useEffect(() => {
    setActiveProductId(selectedProductId)
    if (selectedProductId) loadData(selectedProductId)
  }, [selectedProductId])

  const loadData = async (pid: string) => {
    if (!pid) return
    setLoading(true)
    try {
      const [projs, mods] = await Promise.all([
        prodApi.listProjects(pid),
        prodApi.listModules(pid),
      ])
      setProjects(p => ({ ...p, [pid]: projs || [] }))
      setModules(m => ({ ...m, [pid]: mods || [] }))
    } catch {}
    setLoading(false)
  }

  const handleAdd = async (name: string, desc: string, version: string) => {
    if (!addTarget || !name.trim()) return
    setAdding(true)
    try {
      const pid = addTarget.pid
      if (addTarget.type === 'project') {
        await prodApi.createProject(pid, name.trim(), desc.trim(), version.trim() || '1.0')
      } else {
        await prodApi.createModule(pid, name.trim(), desc.trim())
      }
      await loadData(pid)
      setAddTarget(null)
    } catch (e: any) { alert('创建失败: ' + e.message) } finally { setAdding(false) }
  }

  const handleDelete = async (type: string, id: string, pid: string) => {
    const label = type === 'project' ? '项目' : '模块'
    if (!confirm(`确认删除此${label}？此操作不可恢复。`)) return
    try {
      if (type === 'project') await prodApi.deleteProject(id)
      else await prodApi.deleteModule(id)
      await loadData(pid)
    } catch (e: any) { alert('删除失败: ' + e.message) }
  }

  const handleEditSave = async (name: string, desc: string, version?: string) => {
    if (!editModal || !activeProductId) return
    setSaving(true)
    try {
      if (editModal.type === 'module') {
        await prodApi.updateModule(editModal.id, name.trim(), desc.trim())
      } else {
        await prodApi.updateProject(editModal.id, name.trim(), desc.trim(), version?.trim() || '1.0')
      }
      await loadData(activeProductId)
      setEditModal(null)
    } catch (e: any) { alert('保存失败: ' + e.message) }
    setSaving(false)
  }

  // 搜索过滤
  const filteredProjects = useMemo(() => {
    const all = projects[activeProductId || ''] || []
    if (!projectSearch.trim()) return all
    const q = projectSearch.toLowerCase()
    return all.filter(p =>
      p.name.toLowerCase().includes(q) ||
      (p.description || '').toLowerCase().includes(q) ||
      (p.version || '').toLowerCase().includes(q)
    )
  }, [projects, activeProductId, projectSearch])

  const filteredModules = useMemo(() => {
    const all = modules[activeProductId || ''] || []
    if (!moduleSearch.trim()) return all
    const q = moduleSearch.toLowerCase()
    return all.filter(m =>
      m.name.toLowerCase().includes(q) ||
      (m.description || '').toLowerCase().includes(q)
    )
  }, [modules, activeProductId, moduleSearch])

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="page-title flex items-center gap-2">
            <FolderTree className="w-6 h-6" style={{ color: 'var(--brand-500)' }} />
            项目与模块管理
          </h1>
          <p className="text-sm mt-1" style={{ color: 'var(--foreground-tertiary)' }}>管理员 — 选择产品后管理其项目和模块</p>
        </div>
      </div>

      {!activeProductId ? (
        <div className="text-center py-16" style={{ color: 'var(--foreground-quaternary)' }}>
          <FolderTree className="w-12 h-12 mx-auto mb-3 opacity-50" />
          <p className="font-medium">请先选择一个产品</p>
          <p className="text-sm mt-1">选择产品后查看和管理其项目和模块</p>
        </div>
      ) : (
        <div className="card">
          <div className="flex items-center justify-between mb-4">
            <h2 className="section-title flex items-center gap-2">
              <FolderTree className="w-5 h-5" style={{ color: 'var(--brand-500)' }} />
              {selectedProduct?.name || activeProductId}
              <span className="text-sm font-normal" style={{ color: 'var(--foreground-quaternary)' }}>— 项目与模块</span>
            </h2>
          </div>

          {/* 标签页 */}
          <div className="flex items-center gap-1 mb-4 border-b" style={{ borderColor: 'var(--border)' }}>
            <button
              onClick={() => setActiveTab('project')}
              className="flex items-center gap-2 px-4 py-2 text-sm font-medium border-b-2 transition-all"
              style={activeTab === 'project'
                ? { borderColor: 'var(--brand-500)', color: 'var(--brand-500)' }
                : { borderColor: 'transparent', color: 'var(--foreground-quaternary)' }
              }
            >
              <FolderOpen className="w-4 h-4" />
              项目
              <span className="text-xs font-normal" style={{ color: 'var(--foreground-quaternary)' }}>
                ({(projects[activeProductId] || []).length})
              </span>
            </button>
            <button
              onClick={() => setActiveTab('module')}
              className="flex items-center gap-2 px-4 py-2 text-sm font-medium border-b-2 transition-all"
              style={activeTab === 'module'
                ? { borderColor: 'var(--brand-500)', color: 'var(--brand-500)' }
                : { borderColor: 'transparent', color: 'var(--foreground-quaternary)' }
              }
            >
              <Puzzle className="w-4 h-4" />
              模块
              <span className="text-xs font-normal" style={{ color: 'var(--foreground-quaternary)' }}>
                ({(modules[activeProductId] || []).length})
              </span>
            </button>
          </div>

          {/* 搜索 + 添加按钮 */}
          <div className="flex items-center gap-2 mb-4">
            <div className="relative flex-1">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4" style={{ color: 'var(--foreground-quaternary)' }} />
              <input
                value={activeTab === 'project' ? projectSearch : moduleSearch}
                onChange={e => activeTab === 'project' ? setProjectSearch(e.target.value) : setModuleSearch(e.target.value)}
                className="input-field text-sm py-1.5 pl-9"
                placeholder={activeTab === 'project' ? '搜索项目名称、描述或版本号...' : '搜索模块名称或描述...'}
              />
            </div>
            <button
              onClick={() => setAddTarget({ pid: activeProductId, type: activeTab })}
              className="btn-primary text-xs whitespace-nowrap"
            >
              <Plus className="w-3 h-3" /> {activeTab === 'project' ? '添加项目' : '添加模块'}
            </button>
          </div>

          {/* 添加表单 */}
          {addTarget && (
            <AddForm
              type={addTarget.type}
              adding={adding}
              onAdd={handleAdd}
              onCancel={() => setAddTarget(null)}
            />
          )}

          {loading ? (
            <div className="flex items-center justify-center py-10"><Loader2 className="w-6 h-6 animate-spin" style={{ color: 'var(--foreground-quaternary)' }} /></div>
          ) : activeTab === 'project' ? (
            /* 项目列表 */
            filteredProjects.length === 0 ? (
              <p className="text-xs py-8 text-center rounded-xl"
                style={{ color: 'var(--foreground-quaternary)', background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.75)' }}>
                {projectSearch ? '未找到匹配的项目' : '暂无项目'}
              </p>
            ) : (
              <div className="space-y-1.5">
                {filteredProjects.map(proj => (
                  <div key={proj.id} className="flex items-center justify-between p-3 rounded-xl transition-colors group border border-transparent"
                    style={{ borderRadius: 'calc(var(--radius) * 0.75)' }}>
                    <div className="flex items-center gap-2.5 min-w-0">
                      <FolderOpen className="w-4 h-4 flex-shrink-0" style={{ color: 'var(--state-warning)' }} />
                      <div className="min-w-0">
                        <div className="flex items-center gap-2">
                          <p className="text-sm font-medium truncate" style={{ color: 'var(--foreground-secondary)' }}>{proj.name}</p>
                          {proj.version && (
                            <span className="text-xs px-1.5 py-0.5 rounded font-mono"
                              style={{ background: 'rgba(0, 122, 255, 0.08)', color: 'var(--brand-500)' }}>
                              v{proj.version}
                            </span>
                          )}
                        </div>
                        <p className="text-xs truncate" style={{ color: 'var(--foreground-quaternary)' }}>{proj.description || proj.id}</p>
                      </div>
                    </div>
                    <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity flex-shrink-0">
                      <button onClick={() => setEditModal({ type: 'project', id: proj.id, name: proj.name, description: proj.description, version: proj.version })}
                        className="btn-ghost p-1">
                        <Edit3 className="w-3.5 h-3.5" />
                      </button>
                      <button onClick={() => handleDelete('project', proj.id, activeProductId)}
                        className="btn-ghost p-1">
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )
          ) : (
            /* 模块列表 */
            filteredModules.length === 0 ? (
              <p className="text-xs py-8 text-center rounded-xl"
                style={{ color: 'var(--foreground-quaternary)', background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.75)' }}>
                {moduleSearch ? '未找到匹配的模块' : '暂无模块'}
              </p>
            ) : (
              <div className="space-y-1.5">
                {filteredModules.map(mod => (
                  <div key={mod.id} className="flex items-center justify-between p-3 rounded-xl transition-colors group border border-transparent"
                    style={{ borderRadius: 'calc(var(--radius) * 0.75)' }}>
                    <div className="flex items-center gap-2.5 min-w-0">
                      <Puzzle className="w-4 h-4 flex-shrink-0" style={{ color: 'var(--state-success)' }} />
                      <div className="min-w-0">
                        <p className="text-sm font-medium truncate" style={{ color: 'var(--foreground-secondary)' }}>{mod.name}</p>
                        <p className="text-xs truncate" style={{ color: 'var(--foreground-quaternary)' }}>{mod.description || mod.id}</p>
                      </div>
                    </div>
                    <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                      <button onClick={() => setEditModal({ type: 'module', id: mod.id, name: mod.name, description: mod.description })}
                        className="btn-ghost p-1">
                        <Edit3 className="w-3 h-3" />
                      </button>
                      <button onClick={() => handleDelete('module', mod.id, activeProductId)}
                        className="btn-ghost p-1">
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )
          )}
        </div>
      )}

      {/* 编辑弹窗 */}
      {editModal && (
      <EditModal
        title={editModal.type === 'project' ? '编辑项目' : '编辑模块'}
        name={editModal.name}
        description={editModal.description}
        version={editModal.version}
        showVersion={editModal.type === 'project'}
        saving={saving}
        onSave={handleEditSave}
        onClose={() => setEditModal(null)}
      />
      )}
    </div>
  )
}
