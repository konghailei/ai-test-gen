import { useState, useEffect } from 'react'
import { Plus, Trash2, FolderTree, Loader2, Edit3, Save, X, ShieldCheck, ArrowRight, ExternalLink, Search, AlertTriangle } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { products as prodApi, apiGet, apiPut } from '../api/client'
import { useProduct } from '../store/ProductContext'
import type { Product } from '../types'
import { formatDate } from '../utils'

/** 编辑弹窗 */
function EditModal({
  title, name, description, visibleIds, users, onSave, onClose, saving
}: {
  title: string; name: string; description: string;
  visibleIds: number[]; users: any[];
  onSave: (name: string, desc: string, visible_ids: number[]) => Promise<void>;
  onClose: () => void; saving: boolean
}) {
  const [n, setN] = useState(name)
  const [d, setD] = useState(description)
  const [vids, setVids] = useState<number[]>(visibleIds || [])
  const [userSearch, setUserSearch] = useState('')

  const toggleUser = (uid: number) => {
    setVids(prev => prev.includes(uid) ? prev.filter(id => id !== uid) : [...prev, uid])
  }

  const filteredUsers = userSearch.trim()
    ? users.filter((u: any) => {
        const kw = userSearch.toLowerCase()
        return (u.username || '').toLowerCase().includes(kw) ||
               (u.display_name || '').toLowerCase().includes(kw)
      })
    : users

  return (
    <div className="fixed inset-0 z-50 bg-black/30 backdrop-blur-sm flex items-center justify-center p-4" onClick={() => !saving && onClose()}>
      <div className="bg-white rounded-2xl shadow-xl max-w-md w-full p-6 animate-slide-up max-h-[90vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
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
              className="input-field text-sm resize-none" rows={2} placeholder="可选" />
          </div>
          <div>
            <label className="label">可查看此产品的用户</label>
            <p className="text-[11px] text-slate-400 mb-1">未选择任何用户 = 所有用户可见</p>
            <div className="relative mb-2">
              <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-400" />
              <input value={userSearch} onChange={e => setUserSearch(e.target.value)}
                className="input-field text-xs pl-8 py-1.5" placeholder="搜索用户名..." />
            </div>
            <div className="max-h-32 overflow-y-auto space-y-0.5 border border-slate-200 rounded-lg p-2">
              {filteredUsers.map((u: any) => (
                <label key={u.id} className={`flex items-center gap-2 px-2 py-1 rounded text-sm cursor-pointer ${
                  vids.includes(u.id) ? 'bg-primary-50 text-primary-700' : 'hover:bg-slate-50 text-slate-600'
                }`} onClick={() => toggleUser(u.id)}>
                  <input type="checkbox" checked={vids.includes(u.id)} onChange={() => {}} className="w-4 h-4 rounded" />
                  <span className="font-medium">{u.display_name || u.username}</span>
                  <span className="text-xs text-slate-400">({u.username})</span>
                </label>
              ))}
              {!filteredUsers.length && <p className="text-xs text-slate-400 p-1">{userSearch ? '无匹配用户' : '暂无用户'}</p>}
            </div>
          </div>
        </div>
        <div className="flex justify-end gap-3 mt-5">
          <button onClick={onClose} className="btn-secondary" disabled={saving}><X className="w-4 h-4" /> 取消</button>
          <button onClick={() => onSave(n, d, vids)} disabled={!n.trim() || saving} className="btn-primary">
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />} 保存
          </button>
        </div>
      </div>
    </div>
  )
}

/** 删除确认弹窗 — 必须输入产品名称才能删除，防止误删 */
function DeleteConfirmModal({
  product, stats, onConfirm, onClose, deleting
}: {
  product: { id: string; name: string }
  stats: { projects: number; modules: number; requirements: number; test_cases: number; functional_points: number } | null
  onConfirm: (inputName: string) => Promise<void>
  onClose: () => void
  deleting: boolean
}) {
  const [inputName, setInputName] = useState('')
  const [error, setError] = useState('')

  const match = inputName.trim() === product.name
  const statsLoading = !stats

  const handleConfirm = async () => {
    if (!inputName.trim()) { setError('请输入产品名称'); return }
    if (!match) { setError(`输入与产品名称「${product.name}」不匹配`); return }
    setError('')
    await onConfirm(inputName.trim())
  }

  return (
    <div className="fixed inset-0 z-50 bg-black/40 backdrop-blur-sm flex items-center justify-center p-4" onClick={() => !deleting && onClose()}>
      <div className="bg-white rounded-2xl shadow-xl max-w-md w-full p-6 animate-slide-up" onClick={e => e.stopPropagation()}>
        <div className="flex items-center gap-3 mb-4">
          <div className="w-10 h-10 rounded-full bg-red-100 flex items-center justify-center flex-shrink-0">
            <AlertTriangle className="w-5 h-5 text-red-600" />
          </div>
          <div>
            <h2 className="text-lg font-semibold text-slate-800">删除产品</h2>
            <p className="text-xs text-red-600 mt-0.5">此操作不可恢复，请谨慎操作</p>
          </div>
        </div>

        {/* 产品信息 */}
        <div className="bg-red-50 border border-red-100 rounded-lg p-3 mb-4">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-sm font-semibold text-slate-800">{product.name}</span>
            <span className="text-[10px] font-mono text-slate-400">{product.id}</span>
          </div>
          {statsLoading ? (
            <div className="flex items-center gap-2 text-xs text-slate-500">
              <Loader2 className="w-3 h-3 animate-spin" /> 统计关联数据中...
            </div>
          ) : stats ? (
            <div className="text-xs text-slate-600 leading-relaxed">
              将级联删除以下数据：
              <div className="grid grid-cols-2 gap-x-3 gap-y-0.5 mt-1.5 ml-1">
                <span>· 项目：<b className="text-slate-800">{stats.projects}</b> 个</span>
                <span>· 模块：<b className="text-slate-800">{stats.modules}</b> 个</span>
                <span>· 需求：<b className="text-slate-800">{stats.requirements}</b> 条</span>
                <span>· 功能点：<b className="text-slate-800">{stats.functional_points}</b> 个</span>
                <span className="col-span-2">· 测试用例：<b className="text-slate-800">{stats.test_cases}</b> 条</span>
              </div>
              {(stats.requirements > 0 || stats.test_cases > 0) && (
                <div className="mt-1.5 text-[11px] text-red-600">⚠ 含需求/用例数据，删除后无法找回</div>
              )}
            </div>
          ) : null}
        </div>

        {/* 名称确认输入 */}
        <div>
          <label className="label">
            请输入产品名称 <span className="text-red-500 font-mono">「{product.name}」</span> 以确认删除
          </label>
          <input
            value={inputName}
            onChange={e => { setInputName(e.target.value); setError('') }}
            className="input-field text-sm"
            placeholder={`请输入：${product.name}`}
            autoFocus
            disabled={deleting}
            onKeyDown={e => { if (e.key === 'Enter' && match && !deleting) handleConfirm() }}
          />
          {error && <p className="text-xs text-red-600 mt-1.5">{error}</p>}
          {!error && !match && inputName && (
            <p className="text-xs text-amber-600 mt-1.5">名称不匹配，请仔细核对</p>
          )}
          {!error && match && (
            <p className="text-xs text-emerald-600 mt-1.5">✓ 名称匹配，可以删除</p>
          )}
        </div>

        <div className="flex justify-end gap-3 mt-5">
          <button onClick={onClose} className="btn-secondary" disabled={deleting}>
            <X className="w-4 h-4" /> 取消
          </button>
          <button
            onClick={handleConfirm}
            disabled={!match || deleting}
            className="btn-primary bg-red-600 hover:bg-red-700 disabled:bg-red-300"
          >
            {deleting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Trash2 className="w-4 h-4" />}
            确认删除
          </button>
        </div>
      </div>
    </div>
  )
}

export default function ProductsPage() {
  const navigate = useNavigate()
  const { products, setSelectedProduct, refreshProducts } = useProduct()
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)

  // 新建
  const [showAdd, setShowAdd] = useState(false)
  const [newName, setNewName] = useState('')
  const [newDesc, setNewDesc] = useState('')
  const [adding, setAdding] = useState(false)

  // 编辑
  const [editTarget, setEditTarget] = useState<{ id: string; name: string; description: string; visible_to_user_ids: number[] } | null>(null)
  const [users, setUsers] = useState<any[]>([])
  const [showUsers, setShowUsers] = useState(false)
  const [visibleUserIds, setVisibleUserIds] = useState<number[]>([])

  // 删除确认
  const [deleteTarget, setDeleteTarget] = useState<{ id: string; name: string } | null>(null)
  const [deleteStats, setDeleteStats] = useState<{ projects: number; modules: number; requirements: number; test_cases: number; functional_points: number } | null>(null)
  const [deleting, setDeleting] = useState(false)

  const load = () => {
    setLoading(true)
    Promise.all([refreshProducts(), apiGet('/admin/users')]).then(([, u]: any[]) => {
      setUsers(u || [])
    }).finally(() => setLoading(false))
  }
  useEffect(() => { load() }, [])

  const handleCreate = async () => {
    if (!newName.trim()) return
    setAdding(true)
    try {
      await prodApi.create(newName.trim(), newDesc.trim())
      setNewName(''); setNewDesc(''); setShowAdd(false)
      refreshProducts()
    } catch (e: any) { alert('创建失败: ' + e.message) } finally { setAdding(false) }
  }

  /** 打开删除确认弹窗：先调预检查接口拿产品名称与关联统计 */
  const openDeleteDialog = async (prod: Product) => {
    setDeleteTarget({ id: prod.id, name: prod.name })
    setDeleteStats(null)
    try {
      const res = await prodApi.deleteCheck(prod.id)
      setDeleteStats(res.stats)
      // 若返回的产品名与本地不一致，以服务端为准
      if (res.name && res.name !== prod.name) {
        setDeleteTarget({ id: res.id, name: res.name })
      }
    } catch (e: any) {
      // 预检查失败（如 404），关闭弹窗并提示
      setDeleteTarget(null)
      alert('获取删除信息失败: ' + (e?.message || '产品可能已被删除'))
    }
  }

  const handleDelete = async (inputName: string) => {
    if (!deleteTarget) return
    setDeleting(true)
    try {
      await prodApi.delete(deleteTarget.id, inputName)
      setDeleteTarget(null)
      setDeleteStats(null)
      refreshProducts()
    } catch (e: any) {
      alert('删除失败: ' + (e?.message || '未知错误'))
    } finally {
      setDeleting(false)
    }
  }

  const handleEdit = async (name: string, desc: string, visibleIds: number[]) => {
    if (!editTarget) return
    setSaving(true)
    try {
      await apiPut(`/products/products/${editTarget.id}`, { name, description: desc, visible_to_user_ids: visibleIds })
      setEditTarget(null)
      load()
    } catch (e: any) { alert('保存失败: ' + e.message) }
    setSaving(false)
  }

  /** 点击产品 → 设为当前产品 → 进入主系统 */
  const handleEnter = (product: Product) => {
    setSelectedProduct(product.id, product.name)
    navigate('/dashboard', { replace: true })
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="page-title flex items-center gap-2">
            <ShieldCheck className="w-6 h-6 text-primary-500" />
            产品管理
          </h1>
          <p className="text-sm text-slate-500 mt-1">管理员 — 产品基础信息管理</p>
        </div>
        <button onClick={() => { setShowAdd(true); setNewName(''); setNewDesc('') }} className="btn-primary">
          <Plus className="w-4 h-4" /> 新建产品
        </button>
      </div>

      {/* 新建产品弹窗 */}
      {showAdd && (
        <div className="fixed inset-0 z-50 bg-black/30 backdrop-blur-sm flex items-center justify-center p-4" onClick={() => setShowAdd(false)}>
          <div className="bg-white rounded-2xl shadow-xl max-w-sm w-full p-6 animate-slide-up" onClick={e => e.stopPropagation()}>
            <h2 className="text-lg font-semibold mb-4">新建产品</h2>
            <div className="space-y-3">
              <div>
                <label className="label">产品名称</label>
                <input value={newName} onChange={e => setNewName(e.target.value)}
                  className="input-field" placeholder="产品名称" autoFocus />
              </div>
              <div>
                <label className="label">描述</label>
                <textarea value={newDesc} onChange={e => setNewDesc(e.target.value)}
                  className="input-field text-sm resize-none" rows={2} placeholder="可选" />
              </div>
            </div>
            <div className="flex justify-end gap-3 mt-4">
              <button onClick={() => setShowAdd(false)} className="btn-secondary">取消</button>
              <button onClick={handleCreate} disabled={!newName.trim() || adding} className="btn-primary">
                {adding ? <Loader2 className="w-4 h-4 animate-spin" /> : null} 创建
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 产品列表 */}
      <div className="card p-0 overflow-hidden">
        {loading ? (
          <div className="p-6 space-y-3">{[1,2,3].map(i => <div key={i} className="skeleton h-14" />)}</div>
        ) : products.length === 0 ? (
          <div className="text-center py-16">
            <FolderTree className="w-12 h-12 mx-auto mb-3 text-slate-300" />
            <p className="text-slate-500 font-medium">暂无产品</p>
            <button onClick={() => { setShowAdd(true); setNewName(''); setNewDesc('') }} className="btn-primary mt-4">
              <Plus className="w-4 h-4" /> 新建产品
            </button>
          </div>
        ) : (
          <table className="w-full">
            <thead>
              <tr className="border-b border-slate-100 bg-slate-50/50">
                <th className="text-left text-xs font-semibold text-slate-500 uppercase tracking-wider px-5 py-3.5">名称</th>
                <th className="text-left text-xs font-semibold text-slate-500 uppercase tracking-wider px-5 py-3.5">描述</th>
                <th className="text-left text-xs font-semibold text-slate-500 uppercase tracking-wider px-5 py-3.5">可见用户</th>
                <th className="text-right text-xs font-semibold text-slate-500 uppercase tracking-wider px-5 py-3.5 w-24">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-50">
              {products.map(prod => (
                <tr key={prod.id}
                  onClick={() => handleEnter(prod)}
                  className="hover:bg-primary-50/50 transition-colors cursor-pointer group">
                  <td className="px-5 py-3">
                    <span className="text-sm font-semibold text-slate-800 group-hover:text-primary-700">{prod.name}</span>
                    <span className="ml-2 text-[10px] font-mono text-slate-400">{prod.id}</span>
                  </td>
                  <td className="px-5 py-3"><span className="text-sm text-slate-500">{prod.description || '-'}</span></td>
                  <td className="px-5 py-3">
                    {(prod.visible_to_user_ids || []).length === 0 ? (
                      <span className="text-[11px] text-emerald-600 bg-emerald-50 px-2 py-0.5 rounded-full">所有人可见</span>
                    ) : (
                      <div className="flex flex-wrap gap-1">
                        {(prod.visible_to_user_ids || []).map((uid: number) => {
                          const u = users.find((x: any) => x.id === uid)
                          return <span key={uid} className="text-[11px] bg-slate-100 text-slate-600 px-2 py-0.5 rounded-full">
                            {u?.display_name || u?.username || `#${uid}`}
                          </span>
                        })}
                      </div>
                    )}
                  </td>
                  <td className="px-5 py-3 text-right" onClick={e => e.stopPropagation()}>
                    <div className="flex items-center justify-end gap-1">
                      <button onClick={() => handleEnter(prod)}
                        className="btn-ghost p-1.5 text-primary-400 hover:text-primary-600" title="进入系统">
                        <ExternalLink className="w-3.5 h-3.5" />
                      </button>
                      <button onClick={() => setEditTarget({ id: prod.id, name: prod.name, description: prod.description, visible_to_user_ids: prod.visible_to_user_ids || [] })}
                        className="btn-ghost p-1.5 text-slate-300 hover:text-slate-600">
                        <Edit3 className="w-3.5 h-3.5" />
                      </button>
                      <button onClick={() => openDeleteDialog(prod)}
                        className="btn-ghost p-1.5 text-slate-300 hover:text-red-500">
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* 编辑弹窗 */}
      {editTarget && (
        <EditModal
          title="编辑产品"
          name={editTarget.name}
          description={editTarget.description}
          saving={saving}
          visibleIds={editTarget.visible_to_user_ids || []}
          users={users}
          onSave={handleEdit}
          onClose={() => setEditTarget(null)}
        />
      )}

      {/* 删除确认弹窗 — 必须输入产品名称才能删除 */}
      {deleteTarget && (
        <DeleteConfirmModal
          product={deleteTarget}
          stats={deleteStats}
          onConfirm={handleDelete}
          onClose={() => { if (!deleting) { setDeleteTarget(null); setDeleteStats(null) } }}
          deleting={deleting}
        />
      )}
    </div>
  )
}
