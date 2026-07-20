import { useState, useEffect, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { Plus, Trash2, Puzzle, Loader2, Edit3, Save, X, Search, Share2 } from 'lucide-react'
import { products as prodApi } from '../api/client'
import { useProduct } from '../store/ProductContext'
import type { Module } from '../types'

/** 编辑弹窗 */
function EditModal({
  name, description, onSave, onClose, saving
}: {
  name: string; description: string;
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
          <h2 className="text-lg font-semibold text-slate-800">编辑模块</h2>
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
function AddForm({ onAdd, onCancel, adding }: {
  onAdd: (name: string, desc: string) => Promise<void>;
  onCancel: () => void; adding: boolean
}) {
  const [name, setName] = useState('')
  const [desc, setDesc] = useState('')
  return (
    <div className="flex items-start gap-2 mb-4 p-3 bg-slate-50 rounded-xl">
      <span className="badge bg-primary-100 text-primary-600 text-xs shrink-0 mt-1">模块</span>
      <div className="flex-1 space-y-2">
        <input value={name} onChange={e => setName(e.target.value)} className="input-field text-sm py-1.5" placeholder="模块名称" autoFocus />
        <input value={desc} onChange={e => setDesc(e.target.value)} className="input-field text-sm py-1.5" placeholder="描述（可选）" />
      </div>
      <div className="flex gap-1 shrink-0 mt-1">
        <button onClick={() => onAdd(name, desc)} disabled={!name.trim() || adding} className="btn-primary text-xs py-1.5">确定</button>
        <button onClick={onCancel} className="btn-ghost text-xs py-1.5">取消</button>
      </div>
    </div>
  )
}

export default function ModuleManagementPage() {
  const { selectedProductId, selectedProduct } = useProduct()
  const navigate = useNavigate()

  const [modules, setModules] = useState<Record<string, Module[]>>({})
  const [loading, setLoading] = useState(false)
  const [search, setSearch] = useState('')
  const [showAdd, setShowAdd] = useState(false)
  const [adding, setAdding] = useState(false)
  const [editModal, setEditModal] = useState<{ id: string; name: string; description: string } | null>(null)
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
      const mods = await prodApi.listModules(pid)
      setModules(m => ({ ...m, [pid]: mods || [] }))
    } catch {} finally { setLoading(false) }
  }

  const handleAdd = async (name: string, desc: string) => {
    if (!activeProductId || !name.trim()) return
    setAdding(true)
    try {
      await prodApi.createModule(activeProductId, name.trim(), desc.trim())
      await loadData(activeProductId)
      setShowAdd(false)
    } catch (e: any) { alert('创建失败: ' + e.message) } finally { setAdding(false) }
  }

  const handleDelete = async (id: string) => {
    if (!confirm('确认删除此模块？关联的依赖边将一并清除。')) return
    try {
      await prodApi.deleteModule(id)
      if (activeProductId) await loadData(activeProductId)
    } catch (e: any) { alert('删除失败: ' + e.message) }
  }

  const handleEditSave = async (name: string, desc: string) => {
    if (!editModal) return
    setSaving(true)
    try {
      await prodApi.updateModule(editModal.id, name.trim(), desc.trim())
      if (activeProductId) await loadData(activeProductId)
      setEditModal(null)
    } catch (e: any) { alert('保存失败: ' + e.message) } finally { setSaving(false) }
  }

  const filtered = useMemo(() => {
    const all = modules[activeProductId || ''] || []
    if (!search.trim()) return all
    const q = search.toLowerCase()
    return all.filter(m =>
      m.name.toLowerCase().includes(q) ||
      (m.description || '').toLowerCase().includes(q)
    )
  }, [modules, activeProductId, search])

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="page-title flex items-center gap-2">
            <Puzzle className="w-6 h-6 text-primary-500" />
            模块管理
          </h1>
          <p className="text-sm text-slate-500 mt-1">管理产品下的模块 — 点击模块可跳转至依赖图谱</p>
        </div>
      </div>

      {!activeProductId ? (
        <div className="text-center py-16 text-slate-400">
          <Puzzle className="w-12 h-12 mx-auto mb-3 opacity-50" />
          <p className="font-medium">请先选择一个产品</p>
          <p className="text-sm mt-1">选择产品后查看和管理其模块</p>
        </div>
      ) : (
        <div className="card">
          <div className="flex items-center justify-between mb-4">
            <h2 className="section-title flex items-center gap-2">
              <Puzzle className="w-5 h-5 text-primary-500" />
              {selectedProduct?.name || activeProductId}
              <span className="text-sm font-normal text-slate-400">— 模块列表</span>
            </h2>
          </div>

          <div className="flex items-center gap-2 mb-4">
            <div className="relative flex-1">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
              <input
                value={search}
                onChange={e => setSearch(e.target.value)}
                className="input-field text-sm py-1.5 pl-9"
                placeholder="搜索模块名称或描述..."
              />
            </div>
            <button onClick={() => setShowAdd(true)} className="btn-primary text-xs whitespace-nowrap">
              <Plus className="w-3 h-3" /> 添加模块
            </button>
          </div>

          {showAdd && (
            <AddForm onAdd={handleAdd} onCancel={() => setShowAdd(false)} adding={adding} />
          )}

          {loading ? (
            <div className="flex items-center justify-center py-10"><Loader2 className="w-6 h-6 animate-spin text-slate-400" /></div>
          ) : filtered.length === 0 ? (
            <p className="text-xs text-slate-400 py-8 text-center bg-slate-50 rounded-xl">
              {search ? '未找到匹配的模块' : '暂无模块'}
            </p>
          ) : (
            <div className="space-y-1.5">
              {filtered.map(mod => (
                <div key={mod.id} className="flex items-center justify-between p-3 rounded-xl hover:bg-emerald-50/50 transition-colors group border border-transparent hover:border-emerald-100">
                  <div className="flex items-center gap-2.5 min-w-0 flex-1">
                    <Puzzle className="w-4 h-4 text-emerald-500 flex-shrink-0" />
                    <div className="min-w-0">
                      <p className="text-sm font-medium text-slate-700 truncate">{mod.name}</p>
                      <p className="text-xs text-slate-400 truncate">{mod.description || mod.id}</p>
                    </div>
                  </div>
                  <div className="flex items-center gap-1 flex-shrink-0">
                    {/* 跳转到依赖图谱，并定位到该模块 */}
                    <button
                      onClick={() => navigate(`/dependency-graph?module_id=${encodeURIComponent(mod.id)}`)}
                      className="btn-ghost p-1.5 text-slate-400 hover:text-violet-600 hover:bg-violet-50"
                      title="在依赖图谱中查看此模块"
                    >
                      <Share2 className="w-3.5 h-3.5" />
                    </button>
                    <button onClick={() => setEditModal({ id: mod.id, name: mod.name, description: mod.description })}
                      className="btn-ghost p-1 text-slate-300 hover:text-slate-600">
                      <Edit3 className="w-3.5 h-3.5" />
                    </button>
                    <button onClick={() => handleDelete(mod.id)}
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
          saving={saving}
          onSave={handleEditSave}
          onClose={() => setEditModal(null)}
        />
      )}
    </div>
  )
}
