import { useState, useEffect } from 'react'
import { Plus, Loader2, Trash2, Edit3, Save, X, Users } from 'lucide-react'
import { apiGet, apiPost, apiPut, apiDelete } from '../api/client'

interface Role { id: number; name: string; label: string }
interface User {
  id: number; username: string; display_name: string; email: string
  is_active: boolean; role_id: number | null; role_name: string
}

function EditModal({
  user, roles, onSave, onClose, saving
}: {
  user: User | null; roles: Role[];
  onSave: (data: any) => Promise<void>;
  onClose: () => void; saving: boolean
}) {
  const [displayName, setDisplayName] = useState(user?.display_name || '')
  const [email, setEmail] = useState(user?.email || '')
  const [roleId, setRoleId] = useState(user?.role_id || 0)
  const [isActive, setIsActive] = useState(user?.is_active ?? true)
  const [password, setPassword] = useState('')

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" style={{ background: 'rgba(0, 0, 0, 0.32)', backdropFilter: 'blur(4px)' }} onClick={() => !saving && onClose()}>
      <div className="max-w-sm w-full p-6 animate-slide-up" style={{ background: 'var(--background)', borderRadius: 'calc(var(--radius) * 1.1)', boxShadow: 'var(--shadow-apple-xl)' }} onClick={e => e.stopPropagation()}>
        <div className="flex items-center gap-3 mb-4">
          <Edit3 className="w-5 h-5 text-[var(--brand-500)]" />
          <h2 className="text-lg font-semibold text-[var(--foreground)]">{user ? '编辑用户' : '新建用户'}</h2>
        </div>
        <div className="space-y-3">
          {!user && (
            <div>
              <label className="label">用户名</label>
              <input className="input-field text-sm" id="username" placeholder="用户名" />
            </div>
          )}
          <div>
            <label className="label">显示名称</label>
            <input value={displayName} onChange={e => setDisplayName(e.target.value)}
              className="input-field text-sm" />
          </div>
          <div>
            <label className="label">邮箱</label>
            <input value={email} onChange={e => setEmail(e.target.value)}
              className="input-field text-sm" placeholder="可选" />
          </div>
          <div>
            <label className="label">角色</label>
            <select value={roleId} onChange={e => setRoleId(Number(e.target.value))}
              className="input-field text-sm">
              <option value={0}>无角色</option>
              {roles.map(r => <option key={r.id} value={r.id}>{r.label || r.name}</option>)}
            </select>
          </div>
          <div>
            <label className="label">密码{user ? '（留空不修改）' : ''}</label>
            <input value={password} onChange={e => setPassword(e.target.value)}
              type="password" className="input-field text-sm" placeholder={user ? '留空不修改' : '请输入密码'} />
          </div>
          {user && (
            <label className="flex items-center gap-2 text-sm cursor-pointer pt-1">
              <input type="checkbox" checked={isActive}
                onChange={e => setIsActive(e.target.checked)}
                className="w-4 h-4 rounded border-[var(--input)] text-[var(--brand-600)]" />
              启用账户
            </label>
          )}
        </div>
        <div className="flex justify-end gap-3 mt-5">
          <button onClick={onClose} className="btn-secondary" disabled={saving}><X className="w-4 h-4" /> 取消</button>
          <button onClick={() => onSave({
            display_name: displayName, email, role_id: roleId || null,
            is_active: isActive, password: password || undefined,
            username: (document.getElementById('username') as HTMLInputElement)?.value || '',
          })} disabled={saving} className="btn-primary">
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />} 保存
          </button>
        </div>
      </div>
    </div>
  )
}

export default function UserManagement() {
  const [users, setUsers] = useState<User[]>([])
  const [roles, setRoles] = useState<Role[]>([])
  const [loading, setLoading] = useState(true)

  // 新建
  const [showAdd, setShowAdd] = useState(false)
  const [adding, setAdding] = useState(false)

  // 编辑
  const [editTarget, setEditTarget] = useState<User | null>(null)
  const [saving, setSaving] = useState(false)

  const load = async () => {
    setLoading(true)
    try {
      const [u, r] = await Promise.all([apiGet('/admin/users'), apiGet('/admin/roles')])
      setUsers(Array.isArray(u) ? u : [])
      setRoles(Array.isArray(r) ? r : [])
    } catch (e: any) { console.error(e) }
    setLoading(false)
  }
  useEffect(() => { load() }, [])

  const handleCreate = async (data: any) => {
    if (!data.username.trim() || !data.password) return
    setAdding(true)
    try {
      await apiPost('/admin/users', {
        username: data.username, password: data.password,
        display_name: data.display_name, email: data.email,
        role_id: data.role_id || null,
      })
      setShowAdd(false)
      load()
    } catch (e: any) { alert('创建失败: ' + e.message) } finally { setAdding(false) }
  }

  const handleEdit = async (data: any) => {
    if (!editTarget) return
    setSaving(true)
    try {
      const body: any = {
        display_name: data.display_name, email: data.email,
        role_id: data.role_id || null, is_active: data.is_active,
      }
      if (data.password) body.password = data.password
      await apiPut(`/admin/users/${editTarget.id}`, body)
      setEditTarget(null)
      load()
    } catch (e: any) { alert('保存失败: ' + e.message) } finally { setSaving(false) }
  }

  const handleDelete = async (u: User) => {
    if (u.username === 'admin') return alert('系统管理员用户不可删除')
    if (!confirm(`确定删除用户「${u.display_name || u.username}」吗？`)) return
    try {
      await apiDelete(`/admin/users/${u.id}`)
      load()
    } catch (e: any) { alert('删除失败: ' + e.message) }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="page-title flex items-center gap-2">
            <Users className="w-6 h-6 text-[var(--brand-500)]" />
            用户管理
          </h1>
          <p className="text-sm text-[var(--foreground-tertiary)] mt-1">管理员 — 系统用户创建与管理</p>
        </div>
        <button onClick={() => setShowAdd(true)} className="btn-primary">
          <Plus className="w-4 h-4" /> 新建用户
        </button>
      </div>

      {/* 新建 */}
      {showAdd && (
        <EditModal user={null} roles={roles} onSave={handleCreate}
          onClose={() => setShowAdd(false)} saving={adding} />
      )}

      {/* 编辑 */}
      {editTarget && (
        <EditModal user={editTarget} roles={roles} onSave={handleEdit}
          onClose={() => setEditTarget(null)} saving={saving} />
      )}

      {/* 列表 */}
      <div className="card p-0 overflow-hidden">
        {loading ? (
          <div className="p-6 space-y-3">{[1,2].map(i => <div key={i} className="skeleton h-14" />)}</div>
        ) : (
          <table className="w-full">
            <thead>
              <tr className="border-b border-[var(--border)] bg-[var(--background-muted)]">
                <th className="text-left text-xs font-semibold text-[var(--foreground-tertiary)] uppercase tracking-wider px-5 py-3.5">用户名</th>
                <th className="text-left text-xs font-semibold text-[var(--foreground-tertiary)] uppercase tracking-wider px-5 py-3.5">显示名称</th>
                <th className="text-left text-xs font-semibold text-[var(--foreground-tertiary)] uppercase tracking-wider px-5 py-3.5">邮箱</th>
                <th className="text-left text-xs font-semibold text-[var(--foreground-tertiary)] uppercase tracking-wider px-5 py-3.5">角色</th>
                <th className="text-center text-xs font-semibold text-[var(--foreground-tertiary)] uppercase tracking-wider px-5 py-3.5 w-20">状态</th>
                <th className="text-right text-xs font-semibold text-[var(--foreground-tertiary)] uppercase tracking-wider px-5 py-3.5 w-20">操作</th>
              </tr>
            </thead>
            <tbody>
              {users.map(u => (
                <tr key={u.id} className="border-b border-[var(--border)] hover:bg-[var(--background-muted)] transition-colors">
                  <td className="px-5 py-3.5">
                    <span className="text-sm font-semibold text-[var(--foreground)]">{u.username}</span>
                  </td>
                  <td className="px-5 py-3.5 text-sm text-[var(--foreground-secondary)]">{u.display_name || '-'}</td>
                  <td className="px-5 py-3.5 text-sm text-[var(--foreground-tertiary)]">{u.email || '-'}</td>
                  <td className="px-5 py-3.5">
                    <span className="text-xs bg-[var(--background-muted)] text-[var(--foreground-secondary)] px-2 py-0.5 rounded-full">
                      {u.role_name || '无'}
                    </span>
                  </td>
                  <td className="px-5 py-3.5 text-center">
                    <span className={`text-[11px] px-1.5 py-0.5 rounded-full ${
                      u.is_active ? 'bg-[var(--state-success-surface)] text-[var(--state-success)]' : 'bg-[var(--state-error-surface)] text-[var(--state-error)]'
                    }`}>{u.is_active ? '启用' : '禁用'}</span>
                  </td>
                  <td className="px-5 py-3.5">
                    <div className="flex justify-end gap-1">
                      <button onClick={() => setEditTarget(u)}
                        className="p-1.5 rounded-[calc(var(--radius)*0.5)] text-[var(--foreground-tertiary)] hover:text-[var(--brand-600)] hover:bg-[rgba(0,122,255,0.08)] transition-colors"
                        title="编辑"><Edit3 className="w-4 h-4" /></button>
                      <button onClick={() => handleDelete(u)}
                        className={`p-1.5 rounded-[calc(var(--radius)*0.5)] transition-colors ${
                          u.username === 'admin' ? 'text-[var(--foreground-quaternary)] cursor-not-allowed' : 'text-[var(--foreground-tertiary)] hover:text-[var(--state-error)] hover:bg-[var(--state-error-surface)]'
                        }`}
                        title={u.username === 'admin' ? '系统管理员不可删除' : '删除'}>
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
              {!users.length && (
                <tr><td colSpan={6} className="px-5 py-16 text-center text-sm text-[var(--foreground-tertiary)]">暂无用户</td></tr>
              )}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
