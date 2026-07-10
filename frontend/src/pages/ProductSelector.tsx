import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Building2, Plus, Trash2, Edit3, Loader2, Check, ChevronRight,
  LogOut, User as UserIcon
} from 'lucide-react'
import { products as prodApi } from '../api/client'
import { useAuth } from '../auth'
import { useProduct } from '../store/ProductContext'
import type { Product } from '../types'

export default function ProductSelector() {
  const { user, logout } = useAuth()
  const { setSelectedProduct, products: ctxProducts } = useProduct()
  const navigate = useNavigate()

  const [products, setProducts] = useState<Product[]>(ctxProducts.length > 0 ? ctxProducts : [])
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const [newDesc, setNewDesc] = useState('')
  const [saving, setSaving] = useState(false)
  const [editId, setEditId] = useState<string | null>(null)
  const [editName, setEditName] = useState('')
  const [editDesc, setEditDesc] = useState('')
  const isAdmin = (user?.permissions || []).includes('admin')

  const load = async () => {
    setLoading(true)
    try {
      const result = await prodApi.list()
      const list = Array.isArray(result) ? result : ((result as any)?.data ?? [])
      setProducts(Array.isArray(list) ? list : [])
    } catch { }
    setLoading(false)
  }

  useEffect(() => { load() }, [])

  const selectProduct = (p: Product) => {
    setSelectedProduct(p.id, p.name)
    navigate('/', { replace: true })
  }

  const handleCreate = async () => {
    if (!newName.trim()) return
    setSaving(true)
    try {
      await prodApi.create(newName.trim(), newDesc.trim())
      setNewName('')
      setNewDesc('')
      setCreating(false)
      await load()
    } catch (e: any) { alert('创建失败: ' + (e?.message || '')) }
    setSaving(false)
  }

  const handleEdit = async () => {
    if (!editId || !editName.trim()) return
    setSaving(true)
    try {
      // Use PUT /api/products/products/{id}
      const token = localStorage.getItem('aitestgen_token')
      await fetch(`/api/products/products/${editId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
        body: JSON.stringify({ name: editName.trim(), description: editDesc.trim() }),
      })
      setEditId(null)
      await load()
    } catch (e: any) { alert('编辑失败: ' + (e?.message || '')) }
    setSaving(false)
  }

  const handleDelete = async (p: Product) => {
    if (!confirm(`确认删除产品「${p.name}」？此操作不可恢复。`)) return
    try { await prodApi.delete(p.id); await load() }
    catch (e: any) { alert('删除失败: ' + (e?.message || '')) }
  }

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-blue-50 flex items-center justify-center p-6">
      <div className="w-full max-w-lg">
        {/* Header */}
        <div className="text-center mb-8">
          <div className="inline-flex items-center justify-center w-16 h-16 rounded-2xl bg-gradient-to-br from-primary-500 to-accent-500 shadow-lg mb-4">
            <Building2 className="w-8 h-8 text-white" />
          </div>
          <h1 className="text-2xl font-bold text-slate-800">选择产品</h1>
          <p className="text-sm text-slate-500 mt-2">
            {isAdmin ? '选择已有产品或创建新产品后进入系统' : '选择你要操作的产品'}
          </p>
        </div>

        {/* User info */}
        <div className="flex items-center justify-between mb-4 px-1">
          <div className="flex items-center gap-2 text-sm text-slate-500">
            <UserIcon className="w-4 h-4" />
            <span>{user?.display_name || user?.username}</span>
            {isAdmin && <span className="text-xs bg-primary-50 text-primary-600 px-1.5 py-0.5 rounded">管理员</span>}
          </div>
          <button onClick={() => { logout(); navigate('/login', { replace: true }) }}
            className="text-xs text-slate-400 hover:text-red-500 flex items-center gap-1">
            <LogOut className="w-3 h-3" /> 退出
          </button>
        </div>

        {/* Product list */}
        <div className="bg-white rounded-2xl shadow-lg border border-slate-100 overflow-hidden">
          {loading ? (
            <div className="flex items-center justify-center py-16">
              <Loader2 className="w-8 h-8 animate-spin text-primary-400" />
            </div>
          ) : products.length === 0 ? (
            <div className="text-center py-16 px-6">
              <Building2 className="w-12 h-12 mx-auto mb-3 text-slate-300" />
              <p className="text-slate-500 font-medium">暂无产品</p>
              <p className="text-sm text-slate-400 mt-1">
                {isAdmin ? '点击下方按钮创建第一个产品' : '请联系管理员创建产品'}
              </p>
            </div>
          ) : (
            <div className="divide-y divide-slate-50">
              {products.map(p => (
                <div key={p.id} className="group flex items-center justify-between px-5 py-4 hover:bg-slate-50 transition-colors">
                  {editId === p.id ? (
                    // Edit mode
                    <div className="flex-1 flex items-center gap-2">
                      <input value={editName} onChange={e => setEditName(e.target.value)}
                        className="flex-1 input-field text-sm py-1" autoFocus />
                      <button onClick={handleEdit} disabled={saving} className="btn-primary text-xs py-1.5">
                        {saving ? <Loader2 className="w-3 h-3 animate-spin" /> : '保存'}
                      </button>
                      <button onClick={() => setEditId(null)} className="btn-ghost text-xs py-1.5">取消</button>
                    </div>
                  ) : (
                    <>
                      <div className="flex-1 min-w-0 cursor-pointer" onClick={() => selectProduct(p)}>
                        <p className="text-sm font-semibold text-slate-700">{p.name}</p>
                        {p.description && <p className="text-xs text-slate-400 mt-0.5 truncate">{p.description}</p>}
                      </div>
                      <div className="flex items-center gap-1">
                        {isAdmin && (
                          <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
                            <button onClick={(e) => { e.stopPropagation(); setEditId(p.id); setEditName(p.name); setEditDesc(p.description || '') }}
                              className="btn-ghost p-1.5 text-slate-300 hover:text-slate-600">
                              <Edit3 className="w-3.5 h-3.5" />
                            </button>
                            <button onClick={(e) => { e.stopPropagation(); handleDelete(p) }}
                              className="btn-ghost p-1.5 text-slate-300 hover:text-red-500">
                              <Trash2 className="w-3.5 h-3.5" />
                            </button>
                          </div>
                        )}
                        <button onClick={() => selectProduct(p)} className="btn-ghost p-1.5 text-slate-300 group-hover:text-primary-500">
                          <ChevronRight className="w-5 h-5" />
                        </button>
                      </div>
                    </>
                  )}
                </div>
              ))}
            </div>
          )}

          {/* Create new product (admin only) */}
          {isAdmin && (
            <div className="border-t border-slate-100 px-5 py-3 bg-slate-50/50">
              {creating ? (
                <div className="space-y-2">
                  <input value={newName} onChange={e => setNewName(e.target.value)}
                    className="w-full input-field text-sm py-1.5" placeholder="产品名称" autoFocus />
                  <input value={newDesc} onChange={e => setNewDesc(e.target.value)}
                    className="w-full input-field text-sm py-1.5" placeholder="描述（可选）" />
                  <div className="flex justify-end gap-2">
                    <button onClick={() => setCreating(false)} className="btn-ghost text-xs">取消</button>
                    <button onClick={handleCreate} disabled={!newName.trim() || saving}
                      className="btn-primary text-xs">{saving ? <Loader2 className="w-3 h-3 animate-spin" /> : '创建'}</button>
                  </div>
                </div>
              ) : (
                <button onClick={() => setCreating(true)}
                  className="w-full flex items-center justify-center gap-1.5 py-2 text-sm text-primary-500 hover:bg-primary-50 rounded-lg transition-colors">
                  <Plus className="w-4 h-4" /> 创建新产品
                </button>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
