import { useState, useEffect } from 'react'
import { Outlet, useNavigate } from 'react-router-dom'
import Sidebar from './Sidebar'
import { health, requirements as reqApi } from '../api/client'
import { useAuth } from '../auth'
import { useProduct } from '../store/ProductContext'
import { Menu, LogOut, User as UserIcon, Settings, ChevronDown, Building2, ShieldCheck, Key, X, Loader2, Save, Package, BookOpen, ListTodo, Bell } from 'lucide-react'

export default function Layout() {
  const { user, isLoggedIn, logout } = useAuth()
  const { selectedProductName, products, selectedProductId, setSelectedProduct, refreshProducts, loading: productsLoading } = useProduct()
  const navigate = useNavigate()
  const [collapsed, setCollapsed] = useState(false)
  const [status, setStatus] = useState({ version: '', llm_configured: false, kb_empty: false })
  const [showUserMenu, setShowUserMenu] = useState(false)
  const [showProductMenu, setShowProductMenu] = useState(false)
  const hasAdmin = (user?.permissions || []).some(p => p === 'admin' || p.startsWith('admin_'))
  const hasProductAdmin = (user?.permissions || []).includes('admin_products')
  const [showPwdModal, setShowPwdModal] = useState(false)
  const [pwdCurrent, setPwdCurrent] = useState('')
  const [pwdNew, setPwdNew] = useState('')
  const [pwdSaving, setPwdSaving] = useState(false)
  const [runningTaskCount, setRunningTaskCount] = useState(0)

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

  useEffect(() => {
    health().then((h) => setStatus({
      version: h.version || '',
      llm_configured: h.llm_configured,
      kb_empty: h.kb_empty,
    })).catch(() => {})
    refreshProducts()
  }, [])

  // v2.14: 轮询进行中任务数（角标显示）；v2.20: 只显示未结束任务（running+pending），failed/completed 不计入
  useEffect(() => {
    const pollRunningCount = async () => {
      try {
        const res = await reqApi.listTasks({ status: 'running,pending', limit: 1 })
        setRunningTaskCount(res.total || 0)
      } catch { /* 忽略 */ }
    }
    pollRunningCount()
    const timer = setInterval(pollRunningCount, 5000)
    return () => clearInterval(timer)
  }, [])

  const handleLogout = () => {
    logout()
    navigate('/login', { replace: true })
  }

  if (!isLoggedIn) return null

  const sidebarWidth = collapsed ? 64 : 240

  return (
    <div className="h-screen overflow-hidden flex" style={{ background: 'var(--background-muted)' }}>
      <Sidebar collapsed={collapsed} onToggle={() => setCollapsed(!collapsed)} />

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
          {/* 折叠按钮 */}
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

          {/* 产品选择器 */}
          <div className="relative">
            <button
              onClick={() => setShowProductMenu(!showProductMenu)}
              className="flex items-center gap-2 transition-all"
              style={{
                height: 40,
                padding: '0 14px',
                border: '1px solid var(--input)',
                borderRadius: 'calc(var(--radius) * 0.6)',
                background: 'var(--background)',
              }}
              onMouseEnter={(e) => { if (!showProductMenu) e.currentTarget.style.borderColor = 'var(--ring)' }}
              onMouseLeave={(e) => { if (!showProductMenu) e.currentTarget.style.borderColor = 'var(--input)' }}
              title="切换产品"
            >
              {productsLoading ? (
                <Loader2 className="w-4 h-4 animate-spin" style={{ color: 'var(--foreground-tertiary)' }} />
              ) : (
                <Building2 className="w-4 h-4" style={{ color: 'var(--brand-500)' }} />
              )}
              <span
                className="truncate max-w-[120px]"
                style={{
                  fontSize: 14,
                  fontWeight: 600,
                  color: selectedProductName ? 'var(--foreground)' : 'var(--foreground-tertiary)',
                }}
              >
                {selectedProductName || '未选择产品'}
              </span>
              <ChevronDown
                className="w-3.5 h-3.5 transition-transform"
                style={{ color: 'var(--foreground-tertiary)', transform: showProductMenu ? 'rotate(180deg)' : 'none' }}
              />
            </button>

            {showProductMenu && (
              <>
                <div className="fixed inset-0 z-10" onClick={() => setShowProductMenu(false)} />
                <div
                  className="absolute left-0 top-full z-20 animate-scale-in"
                  style={{
                    marginTop: 4,
                    width: 240,
                    background: 'var(--background)',
                    borderRadius: 'calc(var(--radius) * 0.6)',
                    boxShadow: 'var(--shadow-apple-xl)',
                    border: '1px solid var(--border)',
                    padding: '6px',
                  }}
                >
                  <div className="px-3 py-1.5 text-[11px] uppercase tracking-wider font-semibold" style={{ color: 'var(--foreground-tertiary)' }}>
                    选择产品
                  </div>
                  <div className="max-h-60 overflow-y-auto">
                    {products.length === 0 ? (
                      <p className="px-3 py-4 text-sm text-center" style={{ color: 'var(--foreground-tertiary)' }}>
                        {productsLoading ? '加载中...' : '暂无产品'}
                      </p>
                    ) : (
                      products.map(p => (
                        <button
                          key={p.id}
                          onClick={() => {
                            setSelectedProduct(p.id, p.name)
                            setShowProductMenu(false)
                            window.location.reload()
                          }}
                          className="flex items-center gap-2.5 w-full transition-all"
                          style={{
                            padding: '8px 12px',
                            borderRadius: 'calc(var(--radius) * 0.4)',
                            fontSize: 13,
                            fontWeight: selectedProductId === p.id ? 500 : 400,
                            color: selectedProductId === p.id ? 'var(--brand-500)' : 'var(--foreground)',
                            background: selectedProductId === p.id ? 'var(--brand-50, #e8f2ff)' : 'transparent',
                          }}
                          onMouseEnter={(e) => { if (selectedProductId !== p.id) e.currentTarget.style.background = 'var(--background-muted)' }}
                          onMouseLeave={(e) => { if (selectedProductId !== p.id) e.currentTarget.style.background = 'transparent' }}
                        >
                          <Package className="w-4 h-4 flex-shrink-0" style={{ color: selectedProductId === p.id ? 'var(--brand-500)' : 'var(--foreground-tertiary)' }} />
                          <span className="truncate">{p.name}</span>
                          {selectedProductId === p.id && (
                            <span className="ml-auto w-2 h-2 rounded-full flex-shrink-0" style={{ background: 'var(--brand-500)' }} />
                          )}
                        </button>
                      ))
                    )}
                  </div>
                  {hasProductAdmin && (
                    <div style={{ borderTop: '1px solid var(--border)', marginTop: 4, paddingTop: 4 }}>
                      <button
                        onClick={() => { navigate('/admin/products'); setShowProductMenu(false) }}
                        className="flex items-center gap-2 w-full transition-all"
                        style={{
                          padding: '8px 12px',
                          borderRadius: 'calc(var(--radius) * 0.4)',
                          fontSize: 13,
                          color: 'var(--foreground-secondary)',
                        }}
                        onMouseEnter={(e) => { e.currentTarget.style.background = 'var(--background-muted)' }}
                        onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent' }}
                      >
                        <Settings className="w-4 h-4" />
                        管理产品
                      </button>
                    </div>
                  )}
                </div>
              </>
            )}
          </div>

          {/* 系统信息 */}
          <div className="flex items-center gap-2 flex-shrink-0">
            {status.version && (
              <span
                className="font-mono"
                style={{
                  fontSize: 12, fontWeight: 600,
                  padding: '4px 10px',
                  borderRadius: 'calc(var(--radius) * 0.4)',
                  background: 'var(--background-muted)',
                  color: 'var(--foreground-tertiary)',
                }}
              >
                v{status.version}
              </span>
            )}
            <span
              className="inline-flex items-center gap-1.5"
              style={{
                fontSize: 12, fontWeight: 500,
                padding: '4px 10px',
                height: 28,
                borderRadius: 'calc(var(--radius) * 0.4)',
                background: status.llm_configured ? 'var(--state-success-surface)' : 'var(--state-warning-surface)',
                color: status.llm_configured ? 'var(--state-success)' : 'var(--state-warning)',
              }}
            >
              <span className="w-1.5 h-1.5 rounded-full" style={{ background: status.llm_configured ? 'var(--state-success)' : 'var(--state-warning)' }} />
              LLM {status.llm_configured ? '已配置' : '未配置'}
            </span>
            {status.kb_empty && (
              <span
                className="inline-flex items-center"
                style={{
                  fontSize: 12, fontWeight: 500,
                  padding: '4px 10px',
                  height: 28,
                  borderRadius: 'calc(var(--radius) * 0.4)',
                  background: '#e8f2ff',
                  color: 'var(--brand-500)',
                }}
              >
                冷启动模式
              </span>
            )}
          </div>

          {/* 右侧操作区 */}
          <div className="flex items-center gap-2 ml-auto flex-shrink-0">
            {/* 任务管理（带角标） */}
            <button
              onClick={() => navigate('/tasks')}
              className="relative flex items-center gap-1.5 transition-colors"
              style={{ height: 40, padding: '0 12px', borderRadius: 'calc(var(--radius) * 0.5)', fontSize: 13, color: 'var(--foreground-secondary)' }}
              onMouseEnter={(e) => { e.currentTarget.style.background = 'var(--background-muted)' }}
              onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent' }}
              title="任务管理"
            >
              <ListTodo className="w-[18px] h-[18px]" />
              <span className="hidden lg:inline">任务管理</span>
              {runningTaskCount > 0 && (
                <span
                  className="absolute flex items-center justify-center text-white font-medium"
                  style={{
                    top: 4, right: 4,
                    minWidth: 18, height: 18, padding: '0 4px',
                    fontSize: 10,
                    borderRadius: 9,
                    background: 'var(--state-error)',
                  }}
                >
                  {runningTaskCount > 99 ? '99+' : runningTaskCount}
                </span>
              )}
            </button>

            <button
              onClick={() => navigate('/manual')}
              className="flex items-center gap-1.5 transition-colors"
              style={{ height: 40, padding: '0 12px', borderRadius: 'calc(var(--radius) * 0.5)', fontSize: 13, color: 'var(--foreground-secondary)' }}
              onMouseEnter={(e) => { e.currentTarget.style.background = 'var(--background-muted)' }}
              onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent' }}
              title="用户手册"
            >
              <BookOpen className="w-[18px] h-[18px]" />
              <span className="hidden lg:inline">用户手册</span>
            </button>

            {/* 用户区 */}
            <div className="relative">
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
                    <div className="py-1">
                      {hasAdmin && (
                        <button
                          onClick={() => { navigate('/admin'); setShowUserMenu(false) }}
                          className="flex items-center gap-2.5 w-full transition-colors"
                          style={{ padding: '8px 12px', borderRadius: 'calc(var(--radius) * 0.4)', fontSize: 13, color: 'var(--state-warning)' }}
                          onMouseEnter={(e) => { e.currentTarget.style.background = 'var(--state-warning-surface)' }}
                          onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent' }}
                        >
                          <ShieldCheck className="w-4 h-4" />
                          管理后台
                        </button>
                      )}
                      <button
                        onClick={() => { navigate('/admin/settings'); setShowUserMenu(false) }}
                        className="flex items-center gap-2.5 w-full transition-colors"
                        style={{ padding: '8px 12px', borderRadius: 'calc(var(--radius) * 0.4)', fontSize: 13, color: 'var(--foreground-secondary)' }}
                        onMouseEnter={(e) => { e.currentTarget.style.background = 'var(--background-muted)' }}
                        onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent' }}
                      >
                        <Settings className="w-4 h-4" />
                        系统设置
                      </button>
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
