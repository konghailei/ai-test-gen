import { Link, useLocation } from 'react-router-dom'
import { useAuth } from '../auth'
import {
  Settings, Shield, Package, FileText, Users, UserCog, LayoutDashboard, Database, Activity, ArrowLeft, Sparkles,
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
    { key: 'admin_logs', path: '/admin/logs', label: '日志查看', icon: Activity },
    { key: 'admin_settings', path: '/admin/settings', label: '系统设置', icon: Settings },
  ]
  return items.filter(i => has(i.key))
}

export default function AdminSidebar({ collapsed }: { collapsed: boolean }) {
  const location = useLocation()
  const { user } = useAuth()
  const permissions: string[] = user?.permissions || []

  return (
    <aside
      className="fixed left-0 top-0 h-screen z-30 flex flex-col transition-all duration-300"
      style={{
        width: collapsed ? 64 : 240,
        background: 'var(--sidebar-bg)',
        borderRight: '1px solid var(--sidebar-border)',
      }}
    >
      {/* Logo — 可点击返回主系统 */}
      <Link
        to="/dashboard"
        title={collapsed ? '返回主系统' : undefined}
        className="flex items-center gap-2.5 flex-shrink-0 transition-all duration-150"
        style={{
          height: 64,
          padding: collapsed ? '0 20px' : '0 16px',
          borderBottom: '1px solid var(--sidebar-border)',
          textDecoration: 'none',
        }}
        onMouseEnter={(e) => { e.currentTarget.style.background = 'var(--sidebar-accent)' }}
        onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent' }}
      >
        <div
          className="flex-shrink-0 flex items-center justify-center"
          style={{
            width: 32, height: 32,
            borderRadius: 'calc(var(--radius) * 0.5)',
            background: 'var(--sidebar-primary)',
          }}
        >
          <Shield className="w-4 h-4 text-white" />
        </div>
        {!collapsed && (
          <div className="overflow-hidden flex-1 min-w-0">
            <h1 className="text-[15px] font-semibold leading-tight" style={{ color: 'var(--sidebar-foreground)', letterSpacing: '-0.01em' }}>
              管理后台
            </h1>
            <p className="text-[11px] leading-tight flex items-center gap-1" style={{ color: 'var(--foreground-tertiary)' }}>
              <ArrowLeft className="w-3 h-3" /> 返回主系统
            </p>
          </div>
        )}
      </Link>

      {/* Nav */}
      <nav className="flex-1 overflow-y-auto no-scrollbar" style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 2 }}>
        {navItems(permissions).map((item) => {
          const isActive = location.pathname === item.path ||
            (item.path !== '/admin' && location.pathname.startsWith(item.path))
          return (
            <Link
              key={item.path}
              to={item.path}
              title={collapsed ? item.label : undefined}
              className="flex items-center gap-2.5 transition-all duration-150"
              style={{
                height: 40,
                padding: collapsed ? '0 10px' : '0 12px',
                borderRadius: 'calc(var(--radius) * 0.5)',
                justifyContent: collapsed ? 'center' : 'flex-start',
                fontSize: 13,
                fontWeight: isActive ? 500 : 400,
                color: isActive ? 'var(--sidebar-primary-foreground)' : 'var(--sidebar-foreground)',
                background: isActive ? 'var(--sidebar-primary)' : 'transparent',
              }}
              onMouseEnter={(e) => {
                if (!isActive) e.currentTarget.style.background = 'var(--sidebar-accent)'
              }}
              onMouseLeave={(e) => {
                if (!isActive) e.currentTarget.style.background = 'transparent'
              }}
            >
              <item.icon className="w-[18px] h-[18px] flex-shrink-0" />
              {!collapsed && <span className="whitespace-nowrap">{item.label}</span>}
            </Link>
          )
        })}
      </nav>

    </aside>
  )
}
