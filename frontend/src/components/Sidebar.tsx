import { Link, useLocation } from 'react-router-dom'
import {
  LayoutDashboard, FileText, TestTube, Database,
  Sparkles, Share2,
  MessageSquare, FolderOpen, Puzzle, ShieldCheck
} from 'lucide-react'
import { useAuth } from '../auth'

function navItems(permissions: string[]) {
  const has = (p: string) => permissions.length === 0 || permissions.includes(p)
  const items = [
    { key: 'dashboard', path: '/dashboard', label: '仪表盘', icon: LayoutDashboard },
    { key: 'project_management', path: '/projects', label: '项目管理', icon: FolderOpen },
    { key: 'module_management', path: '/modules', label: '模块管理', icon: Puzzle },
    { key: 'requirements', path: '/requirements', label: '需求管理', icon: FileText },
    { key: 'testcases', path: '/testcases', label: '测试用例', icon: TestTube },
    { key: 'dependency_graph', path: '/dependency-graph', label: '依赖图谱', icon: Share2 },
    { key: 'knowledge', path: '/knowledge', label: '知识库', icon: Database },
    { key: 'knowledge_qa', path: '/knowledge-qa', label: '知识问答', icon: MessageSquare },
  ]
  // 兼容旧权限：projects_modules 同时允许访问项目管理/模块管理
  return items.filter(i => has(i.key) || (i.key === 'project_management' && has('projects_modules')) || (i.key === 'module_management' && has('projects_modules')))
}

export default function Sidebar({ collapsed, onToggle }: { collapsed: boolean; onToggle: () => void }) {
  const location = useLocation()
  const { user } = useAuth()
  const permissions: string[] = user?.permissions || []
  const hasAdmin = permissions.some(p => p === 'admin' || p.startsWith('admin_'))

  return (
    <aside
      className="fixed left-0 top-0 h-screen z-30 flex flex-col transition-all duration-300"
      style={{
        width: collapsed ? 64 : 240,
        background: 'var(--sidebar-bg)',
        borderRight: '1px solid var(--sidebar-border)',
      }}
    >
      {/* Logo */}
      <div
        className="flex items-center gap-2.5 flex-shrink-0"
        style={{ height: 64, padding: collapsed ? '0 20px' : '0 16px', borderBottom: '1px solid var(--sidebar-border)' }}
      >
        <div
          className="flex-shrink-0 flex items-center justify-center"
          style={{
            width: 32, height: 32,
            borderRadius: 'calc(var(--radius) * 0.5)',
            background: 'var(--sidebar-primary)',
          }}
        >
          <Sparkles className="w-4 h-4 text-white" />
        </div>
        {!collapsed && (
          <div className="overflow-hidden">
            <h1 className="text-[15px] font-semibold leading-tight" style={{ color: 'var(--sidebar-foreground)', letterSpacing: '-0.01em' }}>
              AI测试生成
            </h1>
            <p className="text-[11px] leading-tight" style={{ color: 'var(--foreground-tertiary)' }}>
              测试用例自动生成
            </p>
          </div>
        )}
      </div>

      {/* Nav */}
      <nav className="flex-1 overflow-y-auto no-scrollbar" style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 2 }}>
        {navItems(permissions).map((item) => {
          const isActive = location.pathname === item.path ||
            (item.path !== '/' && location.pathname.startsWith(item.path))
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

      {/* 管理后台入口（仅管理员） */}
      {hasAdmin && (
        <div style={{ padding: '8px 12px', borderTop: '1px solid var(--sidebar-border)' }}>
          <Link
            to="/admin"
            title={collapsed ? '管理后台' : undefined}
            className="flex items-center gap-2.5 transition-all duration-150"
            style={{
              height: 40,
              padding: collapsed ? '0 10px' : '0 12px',
              borderRadius: 'calc(var(--radius) * 0.5)',
              justifyContent: collapsed ? 'center' : 'flex-start',
              fontSize: 13,
              color: 'var(--sidebar-foreground)',
            }}
            onMouseEnter={(e) => { e.currentTarget.style.background = 'var(--sidebar-accent)' }}
            onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent' }}
          >
            <ShieldCheck className="w-[18px] h-[18px] flex-shrink-0" style={{ color: 'var(--state-warning)' }} />
            {!collapsed && <span className="whitespace-nowrap">管理后台</span>}
          </Link>
        </div>
      )}
    </aside>
  )
}
