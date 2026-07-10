import { useState, useEffect } from 'react'
import { Plus, Trash2, FolderOpen, Puzzle, Loader2, Edit3, Save, X, FolderTree } from 'lucide-react'
import { products as prodApi } from '../api/client'
import { useProduct } from '../store/ProductContext'
import type { Project, Module } from '../types'

/** 编辑弹窗 */
function EditModal({
  title, name, description, onSave, onClose, saving
}: {
  title: string; name: string; description: string;
  onSave: (name: string, desc: string) => Promise<void>;
  onClose: () => void; saving: boolean
}) {
  const [n, setN] = useState(name)
  const [d, setD] = useState(description)
  return (
    <div className="fixed inset-0 z-50 bg-black/30 backdrop-blur-sm flex items-center justify-center p-4" onClick={() => !saving && onClose()}>
      <div className="bg-white rounded-2xl shadow-xl max-w-md w-full p-6 animate-slide-up" onClick={e => e.stopPropagation()}>
        <div className="flex items-center gap-3 mb-4">
          <Edit3 className="w-5 h-5 text-primary-500" />
          <h2 className="text-lg font-semibold text-slate-800">{title}</h2>
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
        </div>
        <div className="flex justify-end gap-3 mt-5">
          <button onClick={onClose} className="btn-secondary" disabled={saving}><X className="w-4 h-4" /> 取消</button>
          <button onClick={() => onSave(n, d)} disabled={!n.trim() || saving} className="btn-primary">
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
  type: 'project' | 'module'; onAdd: (name: string, desc: string) => Promise<void>;
  onCancel: () => void; adding: boolean
}) {
  const [name, setName] = useState('')
  const [desc, setDesc] = useState('')
  return (
    <div className="flex items-start gap-2 mb-4 p-3 bg-slate-50 rounded-xl">
      <span className="badge bg-primary-100 text-primary-600 text-xs shrink-0 mt-1">
        {type === 'project' ? '项目' : '模块'}
      </span>
      <div className="flex-1 space-y-2">
        <input value={name} onChange={e => setName(e.target.value)}
          className="input-field text-sm py-1.5"
          placeholder={type === 'project' ? '项目名称' : '模块名称'} autoFocus />
        <input value={desc} onChange={e => setDesc(e.target.value)}
          className="input-field text-sm py-1.5" placeholder="描述（可选）" />
      </div>
      <div className="flex gap-1 shrink-0 mt-1">
        <button onClick={() => onAdd(name, desc)} disabled={!name.trim() || adding}
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

  // 添加状态
  const [addTarget, setAddTarget] = useState<{ pid: string; type: 'project' | 'module' } | null>(null)
  const [adding, setAdding] = useState(false)

  // 编辑
  const [editModal, setEditModal] = useState<{
    type: 'project' | 'module'; id: string; name: string; description: string
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

  const handleAdd = async (name: string, desc: string) => {
    if (!addTarget || !name.trim()) return
    setAdding(true)
    try {
      const pid = addTarget.pid
      if (addTarget.type === 'project') {
        await prodApi.createProject(pid, name.trim(), desc.trim())
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

  const handleEditSave = async (name: string, desc: string) => {
    if (!editModal || !activeProductId) return
    setSaving(true)
    try {
      if (editModal.type === 'module') {
        await prodApi.updateModule(editModal.id, name.trim(), desc.trim())
      }
      // 项目编辑暂不支持（后端未提供 update project API）
      await loadData(activeProductId)
      setEditModal(null)
    } catch (e: any) { alert('保存失败: ' + e.message) }
    setSaving(false)
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="page-title flex items-center gap-2">
            <FolderTree className="w-6 h-6 text-primary-500" />
            项目与模块管理
          </h1>
          <p className="text-sm text-slate-500 mt-1">管理员 — 选择产品后管理其项目和模块</p>
        </div>
      </div>

      {!activeProductId ? (
        <div className="text-center py-16 text-slate-400">
          <FolderTree className="w-12 h-12 mx-auto mb-3 opacity-50" />
          <p className="font-medium">请先选择一个产品</p>
          <p className="text-sm mt-1">选择产品后查看和管理其项目和模块</p>
        </div>
      ) : (
        <div className="card">
          <div className="flex items-center justify-between mb-4">
            <h2 className="section-title flex items-center gap-2">
              <FolderTree className="w-5 h-5 text-primary-500" />
              {selectedProduct?.name || activeProductId}
              <span className="text-sm font-normal text-slate-400">— 项目与模块</span>
            </h2>
            <div className="flex items-center gap-2">
              <button onClick={() => setAddTarget({ pid: activeProductId, type: 'project' })}
                className="btn-ghost text-xs"><Plus className="w-3 h-3" /> 添加项目</button>
              <button onClick={() => setAddTarget({ pid: activeProductId, type: 'module' })}
                className="btn-ghost text-xs"><Plus className="w-3 h-3" /> 添加模块</button>
            </div>
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
            <div className="flex items-center justify-center py-10"><Loader2 className="w-6 h-6 animate-spin text-slate-400" /></div>
          ) : (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              {/* 项目列表 */}
              <div>
                <h3 className="text-sm font-semibold text-slate-600 flex items-center gap-2 mb-3">
                  <FolderOpen className="w-4 h-4 text-amber-500" />
                  项目 <span className="text-xs text-slate-400 font-normal">({(projects[activeProductId] || []).length})</span>
                </h3>
                {(projects[activeProductId] || []).length === 0 ? (
                  <p className="text-xs text-slate-400 py-4 text-center bg-slate-50 rounded-xl">暂无项目</p>
                ) : (
                  <div className="space-y-1.5">
                    {(projects[activeProductId] || []).map(proj => (
                      <div key={proj.id} className="flex items-center justify-between p-2.5 rounded-xl hover:bg-amber-50/50 transition-colors group">
                        <div className="flex items-center gap-2.5 min-w-0">
                          <FolderOpen className="w-4 h-4 text-amber-500 flex-shrink-0" />
                          <div className="min-w-0">
                            <p className="text-sm font-medium text-slate-700 truncate">{proj.name}</p>
                            <p className="text-xs text-slate-400 truncate">{proj.description || proj.id}</p>
                          </div>
                        </div>
                        <button onClick={() => handleDelete('project', proj.id, activeProductId)}
                          className="btn-ghost p-1 text-slate-300 opacity-0 group-hover:opacity-100 hover:text-red-500 flex-shrink-0">
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {/* 模块列表 */}
              <div>
                <h3 className="text-sm font-semibold text-slate-600 flex items-center gap-2 mb-3">
                  <Puzzle className="w-4 h-4 text-emerald-500" />
                  模块 <span className="text-xs text-slate-400 font-normal">({(modules[activeProductId] || []).length})</span>
                </h3>
                {(modules[activeProductId] || []).length === 0 ? (
                  <p className="text-xs text-slate-400 py-4 text-center bg-slate-50 rounded-xl">暂无模块</p>
                ) : (
                  <div className="space-y-1.5">
                    {(modules[activeProductId] || []).map(mod => (
                      <div key={mod.id} className="flex items-center justify-between p-2.5 rounded-xl hover:bg-emerald-50/50 transition-colors group">
                        <div className="flex items-center gap-2.5 min-w-0">
                          <Puzzle className="w-4 h-4 text-emerald-500 flex-shrink-0" />
                          <div className="min-w-0">
                            <p className="text-sm font-medium text-slate-700 truncate">{mod.name}</p>
                            <p className="text-xs text-slate-400 truncate">{mod.description || mod.id}</p>
                          </div>
                        </div>
                        <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                          <button onClick={() => setEditModal({ type: 'module', id: mod.id, name: mod.name, description: mod.description })}
                            className="btn-ghost p-1 text-slate-300 hover:text-slate-600">
                            <Edit3 className="w-3 h-3" />
                          </button>
                          <button onClick={() => handleDelete('module', mod.id, activeProductId)}
                            className="btn-ghost p-1 text-slate-300 hover:text-red-500">
                            <Trash2 className="w-3 h-3" />
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      )}

      {/* 编辑弹窗 */}
      {editModal && (
        <EditModal
          title={editModal.type === 'project' ? '编辑项目' : '编辑模块'}
          name={editModal.name}
          description={editModal.description}
          saving={saving}
          onSave={handleEditSave}
          onClose={() => setEditModal(null)}
        />
      )}
    </div>
  )
}
