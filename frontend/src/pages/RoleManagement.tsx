import { useState, useEffect } from 'react'
import { Plus, Loader2, Trash2, Edit3, Save, X, Shield, Check } from 'lucide-react'
import { apiGet, apiPost, apiPut, apiDelete } from '../api/client'

interface Menu { key: string; label: string }
interface Role { id: number; name: string; label: string; menus: string[] }

function EditModal({
  role, menus, onSave, onClose, saving
}: {
  role: Role | null; menus: Menu[];
  onSave: (name: string, label: string, menus: string[]) => Promise<void>;
  onClose: () => void; saving: boolean
}) {
  const [label, setLabel] = useState(role?.label || '')
  const [selected, setSelected] = useState<string[]>(role?.menus || [])

  const toggle = (key: string) => {
    setSelected(prev => prev.includes(key) ? prev.filter(k => k !== key) : [...prev, key])
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" style={{ background: 'rgba(0, 0, 0, 0.32)', backdropFilter: 'blur(4px)' }} onClick={() => !saving && onClose()}>
      <div className="max-w-lg w-full p-6 animate-slide-up" style={{ background: 'var(--background)', borderRadius: 'calc(var(--radius) * 1.1)', boxShadow: 'var(--shadow-apple-xl)' }} onClick={e => e.stopPropagation()}>
        <div className="flex items-center gap-3 mb-4">
          <Edit3 className="w-5 h-5 text-[var(--state-warning)]" />
          <h2 className="text-lg font-semibold text-[var(--foreground)]">{role ? '编辑角色' : '新建角色'}</h2>
        </div>
        <div className="space-y-4">
          <div>
            <label className="label">显示名称</label>
            <input value={label} onChange={e => setLabel(e.target.value)}
              className="input-field text-sm" placeholder="如: 管理员" autoFocus />
          </div>
          <div>
            <label className="label">菜单权限</label>
            <div className="grid grid-cols-2 gap-2 mt-2">
              {menus.map(m => (
                <label key={m.key}
                  className={`flex items-center gap-2.5 px-3 py-2 rounded-[calc(var(--radius)*0.5)] border text-sm cursor-pointer transition-colors ${
                    selected.includes(m.key)
                      ? 'border-[var(--state-warning)] bg-[var(--state-warning-surface)] text-[var(--state-warning)]'
                      : 'border-[var(--border)] text-[var(--foreground-secondary)] hover:bg-[var(--background-muted)]'
                  }`}
                  onClick={() => toggle(m.key)}>
                  <div className={`w-4 h-4 rounded border-2 flex items-center justify-center flex-shrink-0 ${
                    selected.includes(m.key) ? 'bg-[var(--state-warning)] border-[var(--state-warning)]' : 'border-[var(--input)]'
                  }`}>
                    {selected.includes(m.key) && <Check className="w-3 h-3 text-white" />}
                  </div>
                  {m.label}
                </label>
              ))}
            </div>
          </div>
        </div>
        <div className="flex justify-end gap-3 mt-5">
          <button onClick={onClose} className="btn-secondary" disabled={saving}><X className="w-4 h-4" /> 取消</button>
          <button onClick={() => onSave(role?.name || '', label, selected)} disabled={!label.trim() || saving}
            className="btn-primary">
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />} 保存
          </button>
        </div>
      </div>
    </div>
  )
}

export default function RoleManagement() {
  const [roles, setRoles] = useState<Role[]>([])
  const [menus, setMenus] = useState<Menu[]>([])
  const [loading, setLoading] = useState(true)

  // 新建
  const [showAdd, setShowAdd] = useState(false)
  const [newName, setNewName] = useState('')
  const [newLabel, setNewLabel] = useState('')
  const [newMenus, setNewMenus] = useState<string[]>([])
  const [adding, setAdding] = useState(false)

  // 编辑
  const [editTarget, setEditTarget] = useState<Role | null>(null)
  const [saving, setSaving] = useState(false)

  const load = async () => {
    setLoading(true)
    try {
      const [r, m] = await Promise.all([apiGet('/admin/roles'), apiGet('/admin/menus')])
      setRoles(Array.isArray(r) ? r : [])
      setMenus(Array.isArray(m) ? m : [])
    } catch (e: any) { console.error(e) }
    setLoading(false)
  }
  useEffect(() => { load() }, [])

  const toggleNewMenu = (key: string) => {
    setNewMenus(prev => prev.includes(key) ? prev.filter(k => k !== key) : [...prev, key])
  }

  const handleCreate = async () => {
    if (!newName.trim() || !newLabel.trim()) return
    setAdding(true)
    try {
      await apiPost('/admin/roles', { name: newName.trim(), label: newLabel.trim(), menus: newMenus })
      setNewName(''); setNewLabel(''); setNewMenus([]); setShowAdd(false)
      load()
    } catch (e: any) { alert('创建失败: ' + e.message) } finally { setAdding(false) }
  }

  const handleDelete = async (role: Role) => {
    if (role.name === 'admin') return alert('系统管理员角色不可删除')
    if (!confirm(`确定删除角色「${role.label || role.name}」吗？`)) return
    try {
      await apiDelete(`/admin/roles/${role.id}`)
      load()
    } catch (e: any) { alert('删除失败: ' + e.message) }
  }

  const handleEdit = async (name: string, label: string, menus: string[]) => {
    if (!editTarget) return
    setSaving(true)
    try {
      await apiPut(`/admin/roles/${editTarget.id}`, { label, menus })
      setEditTarget(null)
      load()
    } catch (e: any) { alert('保存失败: ' + e.message) } finally { setSaving(false) }
  }

  const getMenuLabel = (key: string) => menus.find(m => m.key === key)?.label || key

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="page-title flex items-center gap-2">
            <Shield className="w-6 h-6 text-[var(--state-warning)]" />
            角色管理
          </h1>
          <p className="text-sm text-[var(--foreground-tertiary)] mt-1">管理员 — 角色及菜单权限管理</p>
        </div>
        <button onClick={() => { setShowAdd(true); setNewName(''); setNewLabel(''); setNewMenus([]) }}
          className="btn-primary">
          <Plus className="w-4 h-4" /> 新建角色
        </button>
      </div>

      {/* 新建 */}
      {showAdd && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4" style={{ background: 'rgba(0, 0, 0, 0.32)', backdropFilter: 'blur(4px)' }} onClick={() => setShowAdd(false)}>
          <div className="max-w-lg w-full p-6 animate-slide-up" style={{ background: 'var(--background)', borderRadius: 'calc(var(--radius) * 1.1)', boxShadow: 'var(--shadow-apple-xl)' }} onClick={e => e.stopPropagation()}>
            <h2 className="text-lg font-semibold mb-4 text-[var(--foreground)]">新建角色</h2>
            <div className="space-y-4">
              <div>
                <label className="label">角色标识</label>
                <input value={newName} onChange={e => setNewName(e.target.value)}
                  className="input-field" placeholder="如: editor" />
              </div>
              <div>
                <label className="label">显示名称</label>
                <input value={newLabel} onChange={e => setNewLabel(e.target.value)}
                  className="input-field" placeholder="如: 编辑者" />
              </div>
              <div>
                <label className="label">菜单权限</label>
                <div className="grid grid-cols-2 gap-2 mt-2">
                  {menus.map(m => (
                    <label key={m.key}
                      className={`flex items-center gap-2.5 px-3 py-2 rounded-[calc(var(--radius)*0.5)] border text-sm cursor-pointer ${
                        newMenus.includes(m.key) ? 'border-[var(--state-warning)] bg-[var(--state-warning-surface)] text-[var(--state-warning)]' : 'border-[var(--border)] text-[var(--foreground-secondary)] hover:bg-[var(--background-muted)]'
                      }`}
                      onClick={() => toggleNewMenu(m.key)}>
                      <div className={`w-4 h-4 rounded border-2 flex items-center justify-center ${
                        newMenus.includes(m.key) ? 'bg-[var(--state-warning)] border-[var(--state-warning)]' : 'border-[var(--input)]'
                      }`}>
                        {newMenus.includes(m.key) && <Check className="w-3 h-3 text-white" />}
                      </div>
                      {m.label}
                    </label>
                  ))}
                </div>
              </div>
            </div>
            <div className="flex justify-end gap-3 mt-4">
              <button onClick={() => setShowAdd(false)} className="btn-secondary">取消</button>
              <button onClick={handleCreate} disabled={!newName.trim() || !newLabel.trim() || adding} className="btn-primary">
                {adding ? <Loader2 className="w-4 h-4 animate-spin" /> : null} 创建
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 编辑弹窗 */}
      {editTarget && (
        <EditModal role={editTarget} menus={menus}
          onSave={(_, label, menus) => handleEdit('_', label, menus)}
          onClose={() => setEditTarget(null)} saving={saving} />
      )}

      {/* 列表 */}
      <div className="card p-0 overflow-hidden">
        {loading ? (
          <div className="p-6 space-y-3">{[1,2].map(i => <div key={i} className="skeleton h-20" />)}</div>
        ) : (
          <table className="w-full">
            <thead>
              <tr className="border-b border-[var(--border)] bg-[var(--background-muted)]">
                <th className="text-left text-xs font-semibold text-[var(--foreground-tertiary)] uppercase tracking-wider px-5 py-3.5">角色</th>
                <th className="text-left text-xs font-semibold text-[var(--foreground-tertiary)] uppercase tracking-wider px-5 py-3.5">权限</th>
                <th className="text-right text-xs font-semibold text-[var(--foreground-tertiary)] uppercase tracking-wider px-5 py-3.5 w-24">操作</th>
              </tr>
            </thead>
            <tbody>
              {roles.map(r => (
                <tr key={r.id} className="border-b border-[var(--border)] hover:bg-[var(--background-muted)] transition-colors">
                  <td className="px-5 py-4">
                    <div className="flex items-center gap-3">
                      <div className="w-9 h-9 rounded-[calc(var(--radius)*0.5)] bg-[var(--state-warning-surface)] flex items-center justify-center flex-shrink-0">
                        <Shield className="w-5 h-5 text-[var(--state-warning)]" />
                      </div>
                      <div>
                        <p className="text-sm font-semibold text-[var(--foreground)]">{r.label || r.name}</p>
                        <p className="text-xs text-[var(--foreground-tertiary)]">{r.name}</p>
                      </div>
                    </div>
                  </td>
                  <td className="px-5 py-4">
                    <div className="flex flex-wrap gap-1">
                      {(r.menus || []).map(m => (
                        <span key={m} className="text-[11px] bg-[var(--background-muted)] text-[var(--foreground-secondary)] px-2 py-0.5 rounded-full">
                          {getMenuLabel(m)}
                        </span>
                      ))}
                    </div>
                  </td>
                  <td className="px-5 py-4">
                    <div className="flex justify-end gap-1">
                      <button onClick={() => r.name !== 'admin' && setEditTarget(r)}
                        className={`p-1.5 rounded-[calc(var(--radius)*0.5)] transition-colors ${
                          r.name === 'admin' ? 'text-[var(--foreground-quaternary)] cursor-not-allowed' : 'text-[var(--foreground-tertiary)] hover:text-[var(--brand-600)] hover:bg-[rgba(0,122,255,0.08)]'
                        }`}
                        title={r.name === 'admin' ? '系统管理员角色不可编辑' : '编辑'}><Edit3 className="w-4 h-4" /></button>
                      <button onClick={() => handleDelete(r)}
                        className={`p-1.5 rounded-[calc(var(--radius)*0.5)] transition-colors ${
                          r.name === 'admin' ? 'text-[var(--foreground-quaternary)] cursor-not-allowed' : 'text-[var(--foreground-tertiary)] hover:text-[var(--state-error)] hover:bg-[var(--state-error-surface)]'
                        }`} title={r.name === 'admin' ? '系统管理员角色不可删除' : '删除'}>
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
              {!roles.length && (
                <tr><td colSpan={3} className="px-5 py-12 text-center text-sm text-[var(--foreground-tertiary)]">暂无角色</td></tr>
              )}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
