import { useState, useEffect } from 'react'
import { Outlet, useNavigate } from 'react-router-dom'
import Sidebar from './Sidebar'
import { health, requirements as reqApi } from '../api/client'
import { useAuth } from '../auth'
import { useProduct } from '../store/ProductContext'
import { Menu, LogOut, User as UserIcon, Settings, ChevronDown, Building2, ShieldCheck, Key, X, Loader2, Save, Package, BookOpen, ListTodo } from 'lucide-react'

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

  return (
    <div className="min-h-screen">
      <Sidebar collapsed={collapsed} onToggle={() => setCollapsed(!collapsed)} />

      {/* Top bar */}
      <header className={`fixed top-0 right-0 z-20 h-16 bg-white/80 backdrop-blur-md border-b border-slate-100 transition-all duration-300 flex items-center justify-between px-6 ${collapsed ? 'left-16' : 'left-60'}`}>
        <div className="flex items-center gap-3">
          <button onClick={() => setCollapsed(!collapsed)} className="btn-ghost p-1.5 -ml-1.5">
            <Menu className="w-5 h-5" />
          </button>

          {/* 产品选择下拉框 */}
          <div className="relative">
            <button
              onClick={() => setShowProductMenu(!showProductMenu)}
              className="flex items-center gap-2 px-3 py-1.5 rounded-lg border border-slate-200 bg-white hover:bg-slate-50 hover:border-primary-300 transition-all text-sm"
              title="切换产品"
            >
              {productsLoading ? (
                <Loader2 className="w-4 h-4 text-slate-400 animate-spin" />
              ) : (
                <Building2 className="w-4 h-4 text-primary-500" />
              )}
              <span className={`truncate max-w-[120px] ${selectedProductName ? 'text-slate-700 font-medium' : 'text-slate-400'}`}>
                {selectedProductName || '未选择产品'}
              </span>
              <ChevronDown className={`w-3.5 h-3.5 text-slate-400 transition-transform ${showProductMenu ? 'rotate-180' : ''}`} />
            </button>

            {showProductMenu && (
              <>
                <div className="fixed inset-0 z-10" onClick={() => setShowProductMenu(false)} />
                <div className="absolute left-0 top-full mt-1 z-20 w-60 bg-white rounded-xl shadow-lg border border-slate-100 py-1.5 animate-fade-in">
                  <div className="px-3 py-1.5 text-[11px] text-slate-400 uppercase tracking-wider font-semibold">选择产品</div>
                  <div className="max-h-60 overflow-y-auto">
                    {products.length === 0 ? (
                      <p className="px-3 py-4 text-sm text-slate-400 text-center">
                        {productsLoading ? '加载中...' : '暂无产品'}
                      </p>
                    ) : (
                      products.map(p => (
                        <button
                          key={p.id}
                          onClick={() => {
                            setSelectedProduct(p.id, p.name)
                            setShowProductMenu(false)
                            // 切换产品后刷新页面，保证所有数据重新加载
                            window.location.reload()
                          }}
                          className={`flex items-center gap-2.5 w-full px-3 py-2 text-sm transition-all ${
                            selectedProductId === p.id
                              ? 'bg-primary-50 text-primary-700 font-medium'
                              : 'text-slate-600 hover:bg-slate-50'
                          }`}
                        >
                          <Package className={`w-4 h-4 flex-shrink-0 ${selectedProductId === p.id ? 'text-primary-500' : 'text-slate-400'}`} />
                          <span className="truncate">{p.name}</span>
                          {selectedProductId === p.id && (
                            <span className="ml-auto w-2 h-2 rounded-full bg-primary-500 flex-shrink-0" />
                          )}
                        </button>
                      ))
                    )}
                  </div>
                  {hasProductAdmin && (
                    <div className="border-t border-slate-100 pt-1 mt-0.5">
                      <button
                        onClick={() => { navigate('/admin/products'); setShowProductMenu(false) }}
                        className="flex items-center gap-2 w-full px-3 py-2 text-sm text-slate-500 hover:bg-slate-50 rounded-lg transition-all"
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

          <div className="flex items-center gap-2 text-xs text-slate-400">
            {status.version && <span className="px-2 py-0.5 bg-slate-100 rounded-md font-mono">v{status.version}</span>}
            <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md ${status.llm_configured ? 'bg-success-50 text-success-600' : 'bg-warning-50 text-warning-600'}`}>
              <span className={`w-1.5 h-1.5 rounded-full ${status.llm_configured ? 'bg-success-500' : 'bg-warning-500'}`} />
              LLM {status.llm_configured ? '已配置' : '未配置'}
            </span>
            {status.kb_empty && (
              <span className="px-2 py-0.5 bg-blue-50 text-blue-600 rounded-md">冷启动模式</span>
            )}
          </div>
        </div>

        {/* 右侧：任务管理 + 用户手册 + 用户信息 */}
        <div className="flex items-center gap-1">
          {/* v2.14: 任务管理按钮（带进行中任务数角标） */}
          <button
            onClick={() => navigate('/tasks')}
            className="relative flex items-center gap-1.5 px-3 py-1.5 rounded-xl hover:bg-slate-100 transition-all text-sm text-slate-600"
            title="任务管理"
          >
            <ListTodo className="w-4 h-4" />
            <span className="hidden sm:inline">任务管理</span>
            {runningTaskCount > 0 && (
              <span className="absolute -top-1 -right-1 min-w-[18px] h-[18px] px-1 flex items-center justify-center bg-primary-500 text-white text-[10px] font-medium rounded-full">
                {runningTaskCount > 99 ? '99+' : runningTaskCount}
              </span>
            )}
          </button>

          <button
            onClick={() => navigate('/manual')}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl hover:bg-slate-100 transition-all text-sm text-slate-600"
            title="用户手册"
          >
            <BookOpen className="w-4 h-4" />
            <span className="hidden sm:inline">用户手册</span>
          </button>

          {/* 用户信息 */}
        <div className="relative">
          <button
            onClick={() => setShowUserMenu(!showUserMenu)}
            className="flex items-center gap-2.5 px-3 py-1.5 rounded-xl hover:bg-slate-100 transition-all"
          >
            <div className="w-8 h-8 rounded-full bg-gradient-to-br from-primary-400 to-accent-400 flex items-center justify-center text-white text-sm font-bold shadow-sm">
              {user?.display_name?.charAt(0) || user?.username?.charAt(0) || 'A'}
            </div>
            <div className="hidden sm:block text-left">
              <p className="text-sm font-medium text-slate-700 leading-tight">{user?.display_name || user?.username || '用户'}</p>
              <p className="text-[10px] text-slate-400 leading-tight">{user?.role === 'admin' ? '管理员' : user?.role || '用户'}</p>
            </div>
          </button>

          {/* 用户下拉菜单 */}
          {showUserMenu && (
            <>
              <div className="fixed inset-0 z-10" onClick={() => setShowUserMenu(false)} />
              <div className="absolute right-0 top-full mt-1 z-20 w-56 bg-white rounded-xl shadow-lg border border-slate-100 py-1.5 animate-fade-in">
                <div className="px-4 py-2 border-b border-slate-100">
                  <p className="text-sm font-medium text-slate-700">{user?.display_name || user?.username}</p>
                  <p className="text-xs text-slate-400">{user?.email || ''}</p>
                </div>
                <div className="px-1 py-1">
                  {hasAdmin && (
                    <button
                      onClick={() => { navigate('/admin'); setShowUserMenu(false) }}
                      className="flex items-center gap-2.5 w-full px-3 py-2 text-sm text-amber-600 hover:bg-amber-50 rounded-lg transition-all"
                    >
                      <ShieldCheck className="w-4 h-4" />
                      管理后台
                    </button>
                  )}
                  <button
                    onClick={() => { navigate('/settings'); setShowUserMenu(false) }}
                    className="flex items-center gap-2.5 w-full px-3 py-2 text-sm text-slate-600 hover:bg-slate-50 rounded-lg transition-all"
                  >
                    <Settings className="w-4 h-4" />
                    系统设置
                  </button>
                </div>
                <div className="border-t border-slate-100 px-1 pt-1">
                  <button
                    onClick={() => { setShowUserMenu(false); setShowPwdModal(true) }}
                    className="flex items-center gap-2.5 w-full px-3 py-2 text-sm text-slate-600 hover:bg-slate-50 rounded-lg transition-all"
                  >
                    <Key className="w-4 h-4" />
                    修改密码
                  </button>
                  <button
                    onClick={handleLogout}
                    className="flex items-center gap-2.5 w-full px-3 py-2 text-sm text-red-600 hover:bg-red-50 rounded-lg transition-all"
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

      {/* 修改密码弹窗 */}
      {showPwdModal && (
        <div className="fixed inset-0 z-50 bg-black/30 backdrop-blur-sm flex items-center justify-center p-4" onClick={() => !pwdSaving && setShowPwdModal(false)}>
          <div className="bg-white rounded-2xl shadow-xl max-w-sm w-full p-6 animate-slide-up" onClick={e => e.stopPropagation()}>
            <div className="flex items-center gap-3 mb-4">
              <Key className="w-5 h-5 text-primary-500" />
              <h2 className="text-lg font-semibold text-slate-800">修改密码</h2>
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

      {/* Main content */}
      <main className={`pt-16 transition-all duration-300 ${collapsed ? 'ml-16' : 'ml-60'}`}>
        <div className="p-6 min-h-[calc(100vh-4rem)] animate-fade-in">
          <Outlet />
        </div>
      </main>
    </div>
  )
}
