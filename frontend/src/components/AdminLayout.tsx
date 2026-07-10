import { useState } from 'react'
import { Outlet, useNavigate } from 'react-router-dom'
import AdminSidebar from './AdminSidebar'
import { useAuth } from '../auth'
import { Menu, LogOut } from 'lucide-react'

export default function AdminLayout() {
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  const [collapsed, setCollapsed] = useState(false)

  const handleLogout = () => {
    logout()
    navigate('/login', { replace: true })
  }

  return (
    <div className="min-h-screen bg-slate-50">
      <AdminSidebar collapsed={collapsed} />

      {/* Top bar — 浅色风格，与主系统一致 */}
      <header className={`fixed top-0 right-0 z-20 h-16 bg-white/80 backdrop-blur-md border-b border-slate-100 transition-all duration-300 flex items-center justify-between px-6 ${collapsed ? 'left-16' : 'left-60'}`}>
        <div className="flex items-center gap-3">
          <button onClick={() => setCollapsed(!collapsed)} className="btn-ghost p-1.5 -ml-1.5">
            <Menu className="w-5 h-5" />
          </button>
          <span className="text-sm font-medium text-slate-600">管理后台</span>
        </div>

        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2">
            <div className="w-7 h-7 rounded-full bg-gradient-to-br from-primary-400 to-accent-400 flex items-center justify-center text-white text-xs font-bold">
              {user?.display_name?.charAt(0) || user?.username?.charAt(0) || 'A'}
            </div>
            <span className="text-sm text-slate-500 hidden sm:inline">{user?.display_name || user?.username}</span>
            <button
              onClick={handleLogout}
              className="p-1.5 text-slate-400 hover:text-red-500 hover:bg-red-50 rounded-lg transition-all"
              title="退出登录"
            >
              <LogOut className="w-4 h-4" />
            </button>
          </div>
        </div>
      </header>

      {/* Main content — 与主系统一致的 p-6 */}
      <main className={`pt-16 transition-all duration-300 ${collapsed ? 'ml-16' : 'ml-60'}`}>
        <div className="p-6 min-h-[calc(100vh-4rem)] animate-fade-in">
          <Outlet />
        </div>
      </main>
    </div>
  )
}
