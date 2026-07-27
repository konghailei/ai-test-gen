import { useState } from 'react'
import { Outlet, useNavigate } from 'react-router-dom'
import AdminSidebar from './AdminSidebar'
import { useAuth } from '../auth'
import { Menu, LogOut, ChevronDown, ShieldCheck, Key, X, Loader2, Save } from 'lucide-react'

export default function AdminLayout() {
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  const [collapsed, setCollapsed] = useState(false)
  const [showUserMenu, setShowUserMenu] = useState(false)
  const [showPwdModal, setShowPwdModal] = useState(false)
  const [pwdCurrent, setPwdCurrent] = useState('')
  const [pwdNew, setPwdNew] = useState('')
  const [pwdSaving, setPwdSaving] = useState(false)

  const handleChangePassword = async () => {
    if (!pwdCurrent || !pwdNew) return
    setPwdSaving(true)
    try {
      const token = localStorage.getItem('aitestgen_token')
      const res = await fetch('/api/auth/change-password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
        body: JSON.stringify({ current_password: pwdCurrent, new_password: pwdNew }),
      })
      const json = await res.json()
      if (!res.ok) throw new Error(json.detail || '修改失败')
      alert('密码修改成功，请重新登录')
      logout()
      navigate('/login', { replace: true })
    } catch (e: any) {
      alert(e.message || '修改失败')
    } finally {
      setPwdSaving(false)
      setPwdCurrent('')
      setPwdNew('')
      setShowPwdModal(false)
    }
  }

  const handleLogout = () => {
    logout()
    navigate('/login', { replace: true })
  }

  const sidebarWidth = collapsed ? 64 : 240

  return (
    <div className="h-screen overflow-hidden flex" style={{ background: 'var(--background-muted)' }}>
      <AdminSidebar collapsed={collapsed} />

      {/* Main area */}
      <div className="flex-1 flex flex-col overflow-hidden min-w-0" style={{ marginLeft: sidebarWidth, transition: 'margin-left 0.3s ease' }}>
        {/* Top header — Apple 风格 64px */}
        <header
          className="flex items-center gap-4 flex-shrink-0"
          style={{
            height: 64,
            background: 'var(--background)',
            borderBottom: '1px solid var(--border)',
            padding: '0 32px',
          }}
        >
          <button
            onClick={() => setCollapsed(!collapsed)}
            className="flex items-center justify-center transition-colors"
            style={{ width: 40, height: 40, borderRadius: 'calc(var(--radius) * 0.5)', color: 'var(--foreground-secondary)' }}
            onMouseEnter={(e) => { e.currentTarget.style.background = 'var(--background-muted)' }}
            onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent' }}
            title="折叠菜单"
          >
            <Menu className="w-5 h-5" />
          </button>

          <div className="flex items-center gap-2">
            <ShieldCheck className="w-5 h-5" style={{ color: 'var(--brand-500)' }} />
            <span className="text-[15px] font-semibold" style={{ color: 'var(--foreground)' }}>管理后台</span>
          </div>

          {/* 用户区 */}
          <div className="relative ml-auto">
            <button
              onClick={() => setShowUserMenu(!showUserMenu)}
              className="flex items-center gap-2 transition-colors"
              style={{ height: 40, padding: '4px 8px 4px 4px', borderRadius: 'calc(var(--radius) * 0.5)' }}
              onMouseEnter={(e) => { e.currentTarget.style.background = 'var(--background-muted)' }}
              onMouseLeave={(e) => { if (!showUserMenu) e.currentTarget.style.background = 'transparent' }}
            >
              <div
                className="flex items-center justify-center text-white font-semibold flex-shrink-0"
                style={{ width: 32, height: 32, borderRadius: '50%', background: 'var(--brand-500)', fontSize: 13 }}
              >
                {user?.display_name?.charAt(0) || user?.username?.charAt(0) || 'A'}
              </div>
              <div className="hidden sm:block text-left">
                <p className="text-[13px] font-medium leading-tight" style={{ color: 'var(--foreground)' }}>
                  {user?.display_name || user?.username || '用户'}
                </p>
                <p className="text-[10px] leading-tight" style={{ color: 'var(--foreground-tertiary)' }}>
                  {user?.role === 'admin' ? '管理员' : user?.role || '用户'}
                </p>
              </div>
              <ChevronDown className="w-4 h-4" style={{ color: 'var(--foreground-tertiary)' }} />
            </button>

            {showUserMenu && (
              <>
                <div className="fixed inset-0 z-10" onClick={() => setShowUserMenu(false)} />
                <div
                  className="absolute right-0 top-full z-20 animate-scale-in"
                  style={{
                    marginTop: 4,
                    width: 224,
                    background: 'var(--background)',
                    borderRadius: 'calc(var(--radius) * 0.6)',
                    boxShadow: 'var(--shadow-apple-xl)',
                    border: '1px solid var(--border)',
                    padding: '6px',
                  }}
                >
                  <div className="px-3 py-2" style={{ borderBottom: '1px solid var(--border)' }}>
                    <p className="text-sm font-medium" style={{ color: 'var(--foreground)' }}>{user?.display_name || user?.username}</p>
                    <p className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>{user?.email || ''}</p>
                  </div>
                  <div style={{ borderTop: '1px solid var(--border)', marginTop: 4, paddingTop: 4 }}>
                    <button
                      onClick={() => { setShowUserMenu(false); setShowPwdModal(true) }}
                      className="flex items-center gap-2.5 w-full transition-colors"
                      style={{ padding: '8px 12px', borderRadius: 'calc(var(--radius) * 0.4)', fontSize: 13, color: 'var(--foreground-secondary)' }}
                      onMouseEnter={(e) => { e.currentTarget.style.background = 'var(--background-muted)' }}
                      onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent' }}
                    >
                      <Key className="w-4 h-4" />
                      修改密码
                    </button>
                    <button
                      onClick={handleLogout}
                      className="flex items-center gap-2.5 w-full transition-colors"
                      style={{ padding: '8px 12px', borderRadius: 'calc(var(--radius) * 0.4)', fontSize: 13, color: 'var(--state-error)' }}
                      onMouseEnter={(e) => { e.currentTarget.style.background = 'var(--state-error-surface)' }}
                      onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent' }}
                    >
                      <LogOut className="w-4 h-4" />
                      退出登录
                    </button>
                  </div>
                </div>
              </>
            )}
          </div>
        </header>

        {/* 内容区 */}
        <main className="flex-1 overflow-y-auto animate-fade-in">
          <div style={{ padding: 32, minHeight: 'calc(100vh - 64px)' }}>
            <Outlet />
          </div>
        </main>
      </div>

      {/* 修改密码弹窗 */}
      {showPwdModal && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center p-4 animate-fade-in"
          style={{ background: 'rgba(0, 0, 0, 0.3)', backdropFilter: 'blur(4px)' }}
          onClick={() => !pwdSaving && setShowPwdModal(false)}
        >
          <div
            className="animate-slide-up"
            style={{ background: 'var(--background)', borderRadius: 'var(--radius)', boxShadow: 'var(--shadow-apple-2xl)', maxWidth: 384, width: '100%', padding: 24 }}
            onClick={e => e.stopPropagation()}
          >
            <div className="flex items-center gap-3 mb-4">
              <Key className="w-5 h-5" style={{ color: 'var(--brand-500)' }} />
              <h2 className="text-lg font-semibold" style={{ color: 'var(--foreground)' }}>修改密码</h2>
            </div>
            <div className="space-y-3">
              <div>
                <label className="label">当前密码</label>
                <input value={pwdCurrent} onChange={e => setPwdCurrent(e.target.value)}
                  type="password" className="input-field text-sm" autoFocus />
              </div>
              <div>
                <label className="label">新密码</label>
                <input value={pwdNew} onChange={e => setPwdNew(e.target.value)}
                  type="password" className="input-field text-sm" />
              </div>
            </div>
            <div className="flex justify-end gap-3 mt-5">
              <button onClick={() => { setShowPwdModal(false); setPwdCurrent(''); setPwdNew('') }}
                className="btn-secondary" disabled={pwdSaving}><X className="w-4 h-4" /> 取消</button>
              <button onClick={handleChangePassword}
                disabled={!pwdCurrent || !pwdNew || pwdSaving} className="btn-primary">
                {pwdSaving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />} 确认修改
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
