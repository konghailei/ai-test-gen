import { useState, useEffect, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { Plus, Trash2, FolderOpen, Loader2, Edit3, Save, X, Search, FileText, TestTube } from 'lucide-react'
import { products as prodApi } from '../api/client'
import { useProduct } from '../store/ProductContext'
import type { Project } from '../types'

/** 编辑弹窗 */
function EditModal({
  name, description, version, onSave, onClose, saving
}: {
  name: string; description: string; version?: string;
  onSave: (name: string, desc: string, version?: string) => Promise<void>;
  onClose: () => void; saving: boolean
}) {
  const [n, setN] = useState(name)
  const [d, setD] = useState(description)
  const [v, setV] = useState(version || '1.0')
  return (
    <div className="fixed inset-0 z-50 bg-black/30 backdrop-blur-sm flex items-center justify-center p-4" onClick={() => !saving && onClose()}>
      <div className="bg-white rounded-2xl shadow-xl max-w-md w-full p-6 animate-slide-up" onClick={e => e.stopPropagation()}>
        <div className="flex items-center gap-3 mb-4">
          <Edit3 className="w-5 h-5 text-primary-500" />
          <h2 className="text-lg font-semibold text-slate-800">编辑项目</h2>
        </div>
        <div className="space-y-3">
          <div>
            <label className="label">名称</label>
            <input value={n} onChange={e => setN(e.target.value)} className="input-field text-sm" autoFocus />
          </div>
          <div>
            <label className="label">描述</label>
            <textarea value={d} onChange={e => setD(e.target.value)} className="input-field text-sm resize-none" rows={3} placeholder="可选" />
          </div>
          <div>
            <label className="label">版本号</label>
            <input value={v} onChange={e => setV(e.target.value)} className="input-field text-sm" placeholder="如 1.0、2.1.0" />
          </div>
        </div>
        <div className="flex justify-end gap-3 mt-5">
          <button onClick={onClose} className="btn-secondary" disabled={saving}><X className="w-4 h-4" /> 取消</button>
          <button onClick={() => onSave(n, d, v)} disabled={!n.trim() || saving} className="btn-primary">
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />} 保存
          </button>
        </div>
      </div>
    </div>
  )
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
    <div className="flex items-start gap-2 mb-4 p-3 bg-slate-50 rounded-xl">
      <span className="badge bg-primary-100 text-primary-600 text-xs shrink-0 mt-1">项目</span>
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

  const [projects, setProjects] = useState<Record<string, Project[]>>({})
  const [loading, setLoading] = useState(false)
  const [search, setSearch] = useState('')
  const [showAdd, setShowAdd] = useState(false)
  const [adding, setAdding] = useState(false)
  const [editModal, setEditModal] = useState<{ id: string; name: string; description: string; version?: string } | null>(null)
  const [saving, setSaving] = useState(false)
  const [activeProductId, setActiveProductId] = useState(selectedProductId)

  useEffect(() => {
    setActiveProductId(selectedProductId)
    if (selectedProductId) loadData(selectedProductId)
  }, [selectedProductId])

  const loadData = async (pid: string) => {
    if (!pid) return
    setLoading(true)
    try {
      const projs = await prodApi.listProjects(pid)
      setProjects(p => ({ ...p, [pid]: projs || [] }))
    } catch {} finally { setLoading(false) }
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

  const handleDelete = async (id: string) => {
    if (!confirm('确认删除此项目？此操作不可恢复，关联需求与用例也将被清除。')) return
    try {
      await prodApi.deleteProject(id)
      if (activeProductId) await loadData(activeProductId)
    } catch (e: any) { alert('删除失败: ' + e.message) }
  }

  const handleEditSave = async (name: string, desc: string, version?: string) => {
    if (!editModal) return
    setSaving(true)
    try {
      await prodApi.updateProject(editModal.id, name.trim(), desc.trim(), version?.trim() || '1.0')
      if (activeProductId) await loadData(activeProductId)
      setEditModal(null)
    } catch (e: any) { alert('保存失败: ' + e.message) } finally { setSaving(false) }
  }

  const filtered = useMemo(() => {
    const all = projects[activeProductId || ''] || []
    if (!search.trim()) return all
    const q = search.toLowerCase()
    return all.filter(p =>
      p.name.toLowerCase().includes(q) ||
      (p.description || '').toLowerCase().includes(q) ||
      (p.version || '').toLowerCase().includes(q)
    )
  }, [projects, activeProductId, search])

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="page-title flex items-center gap-2">
            <FolderOpen className="w-6 h-6 text-primary-500" />
            项目管理
          </h1>
          <p className="text-sm text-slate-500 mt-1">管理产品下的项目/版本 — 点击项目可跳转至对应的需求与用例页面</p>
        </div>
      </div>

      {!activeProductId ? (
        <div className="text-center py-16 text-slate-400">
          <FolderOpen className="w-12 h-12 mx-auto mb-3 opacity-50" />
          <p className="font-medium">请先选择一个产品</p>
          <p className="text-sm mt-1">选择产品后查看和管理其项目</p>
        </div>
      ) : (
        <div className="card">
          <div className="flex items-center justify-between mb-4">
            <h2 className="section-title flex items-center gap-2">
              <FolderOpen className="w-5 h-5 text-primary-500" />
              {selectedProduct?.name || activeProductId}
              <span className="text-sm font-normal text-slate-400">— 项目列表</span>
            </h2>
          </div>

          <div className="flex items-center gap-2 mb-4">
            <div className="relative flex-1">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
              <input
                value={search}
                onChange={e => setSearch(e.target.value)}
                className="input-field text-sm py-1.5 pl-9"
                placeholder="搜索项目名称、描述或版本号..."
              />
            </div>
            <button onClick={() => setShowAdd(true)} className="btn-primary text-xs whitespace-nowrap">
              <Plus className="w-3 h-3" /> 添加项目
            </button>
          </div>

          {showAdd && (
            <AddForm onAdd={handleAdd} onCancel={() => setShowAdd(false)} adding={adding} />
          )}

          {loading ? (
            <div className="flex items-center justify-center py-10"><Loader2 className="w-6 h-6 animate-spin text-slate-400" /></div>
          ) : filtered.length === 0 ? (
            <p className="text-xs text-slate-400 py-8 text-center bg-slate-50 rounded-xl">
              {search ? '未找到匹配的项目' : '暂无项目'}
            </p>
          ) : (
            <div className="space-y-1.5">
              {filtered.map(proj => (
                <div key={proj.id} className="flex items-center justify-between p-3 rounded-xl hover:bg-amber-50/50 transition-colors group border border-transparent hover:border-amber-100">
                  <div className="flex items-center gap-2.5 min-w-0 flex-1">
                    <FolderOpen className="w-4 h-4 text-amber-500 flex-shrink-0" />
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <p className="text-sm font-medium text-slate-700 truncate">{proj.name}</p>
                        {proj.version && (
                          <span className="text-xs px-1.5 py-0.5 bg-blue-50 text-blue-600 rounded font-mono">
                            v{proj.version}
                          </span>
                        )}
                      </div>
                      <p className="text-xs text-slate-400 truncate">{proj.description || '无描述'}</p>
                    </div>
                  </div>
                  <div className="flex items-center gap-1 flex-shrink-0">
                    {/* 跳转到该项目下的需求页面 */}
                    <button
                      onClick={() => navigate(`/requirements?project_id=${encodeURIComponent(proj.id)}`)}
                      className="btn-ghost p-1.5 text-slate-400 hover:text-primary-600 hover:bg-primary-50"
                      title="查看该项目下的需求"
                    >
                      <FileText className="w-3.5 h-3.5" />
                    </button>
                    {/* 跳转到该项目下的用例页面 */}
                    <button
                      onClick={() => navigate(`/testcases?project_id=${encodeURIComponent(proj.id)}`)}
                      className="btn-ghost p-1.5 text-slate-400 hover:text-emerald-600 hover:bg-emerald-50"
                      title="查看该项目下的测试用例"
                    >
                      <TestTube className="w-3.5 h-3.5" />
                    </button>
                    <button onClick={() => setEditModal({ id: proj.id, name: proj.name, description: proj.description, version: proj.version })}
                      className="btn-ghost p-1 text-slate-300 hover:text-slate-600">
                      <Edit3 className="w-3.5 h-3.5" />
                    </button>
                    <button onClick={() => handleDelete(proj.id)}
                      className="btn-ghost p-1 text-slate-300 hover:text-red-500">
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {editModal && (
        <EditModal
          name={editModal.name}
          description={editModal.description}
          version={editModal.version}
          saving={saving}
          onSave={handleEditSave}
          onClose={() => setEditModal(null)}
        />
      )}
    </div>
  )
}
