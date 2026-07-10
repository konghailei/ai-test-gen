import { Link, useLocation } from 'react-router-dom'
import { useAuth } from '../auth'
import {
  Settings, Shield, Package, FileText, Users, UserCog, LayoutDashboard, Database,
} from 'lucide-react'

function navItems(permissions: string[]) {
  const has = (p: string) => permissions.length === 0 || permissions.includes(p)
  const items = [
    { key: 'admin_dashboard', path: '/admin', label: '首页', icon: LayoutDashboard },
    { key: 'admin_products', path: '/admin/products', label: '产品管理', icon: Package },
    { key: 'admin_prompts', path: '/admin/prompts', label: '提示词管理', icon: FileText },
    { key: 'admin_kb', path: '/admin/knowledge', label: '知识库', icon: Database },
    { key: 'admin_users', path: '/admin/users', label: '用户管理', icon: Users },
    { key: 'admin_roles', path: '/admin/roles', label: '角色管理', icon: UserCog },
    { key: 'admin_settings', path: '/admin/settings', label: '系统设置', icon: Settings },
  ]
  return items.filter(i => has(i.key))
}

export default function AdminSidebar({ collapsed }: { collapsed: boolean }) {
  const location = useLocation()
  const { user } = useAuth()
  const permissions: string[] = user?.permissions || []

  return (
    <aside className={`fixed left-0 top-0 h-screen z-30 flex flex-col bg-white border-r border-slate-100 transition-all duration-300 ${collapsed ? 'w-16' : 'w-60'}`}>
      {/* Logo */}
      <div className="flex items-center gap-3 h-16 px-4 border-b border-slate-100">
        <div className="flex-shrink-0 w-8 h-8 rounded-lg bg-gradient-to-br from-amber-400 to-amber-600 flex items-center justify-center shadow-sm">
          <Shield className="w-4 h-4 text-white" />
        </div>
        {!collapsed && (
          <div className="overflow-hidden">
            <h1 className="text-sm font-bold text-slate-800 leading-tight">管理后台</h1>
            <p className="text-[10px] text-slate-400 leading-tight">系统管理</p>
          </div>
        )}
      </div>

      {/* Nav */}
      <nav className="flex-1 py-4 px-2 space-y-1 overflow-y-auto">
        {navItems(permissions).map((item) => {
          const isActive = location.pathname === item.path ||
            (item.path !== '/admin' && location.pathname.startsWith(item.path))
          return (
            <Link
              key={item.path}
              to={item.path}
              className={`flex items-center gap-3 px-3 py-2.5 rounded-xl text-sm font-medium transition-all duration-150 ${
                isActive
                  ? 'bg-gradient-to-r from-amber-50 to-amber-50/50 text-amber-700 shadow-sm'
                  : 'text-slate-500 hover:bg-slate-50 hover:text-slate-700'
              }`}
              title={collapsed ? item.label : undefined}
            >
              <item.icon className={`w-5 h-5 flex-shrink-0 ${isActive ? 'text-amber-500' : 'text-slate-400'}`} />
              {!collapsed && <span>{item.label}</span>}
            </Link>
          )
        })}
      </nav>
    </aside>
  )
}
