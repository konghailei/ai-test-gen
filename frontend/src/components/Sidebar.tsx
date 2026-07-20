import { Link, useLocation } from 'react-router-dom'
import {
  LayoutDashboard, FileText, TestTube, Database, Play,
  ChevronLeft, ChevronRight, Sparkles, Share2,
  MessageSquare, Building2, FolderTree, FolderOpen, Puzzle, ShieldCheck
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
    <aside className={`fixed left-0 top-0 h-screen z-30 flex flex-col bg-white border-r border-slate-100 transition-all duration-300 ${collapsed ? 'w-16' : 'w-60'}`}>
      {/* Logo */}
      <div className="flex items-center gap-3 h-16 px-4 border-b border-slate-100">
        <div className="flex-shrink-0 w-8 h-8 rounded-lg bg-gradient-to-br from-primary-500 to-accent-500 flex items-center justify-center shadow-sm">
          <Sparkles className="w-4 h-4 text-white" />
        </div>
        {!collapsed && (
          <div className="overflow-hidden">
            <h1 className="text-sm font-bold text-slate-800 leading-tight">AI测试用例生成系统</h1>
            <p className="text-[10px] text-slate-400 leading-tight">测试用例自动生成</p>
          </div>
        )}
      </div>

      {/* Nav */}
      <nav className="flex-1 py-4 px-2 space-y-1 overflow-y-auto">
        {navItems(permissions).map((item) => {
          const isActive = location.pathname === item.path ||
            (item.path !== '/' && location.pathname.startsWith(item.path))
          return (
            <Link
              key={item.path}
              to={item.path}
              className={`flex items-center gap-3 px-3 py-2.5 rounded-xl text-sm font-medium transition-all duration-150 group ${
                isActive
                  ? 'bg-gradient-to-r from-primary-50 to-primary-50/50 text-primary-700 shadow-sm'
                  : 'text-slate-500 hover:bg-slate-50 hover:text-slate-700'
              }`}
              title={collapsed ? item.label : undefined}
            >
              <item.icon className={`w-5 h-5 flex-shrink-0 ${isActive ? 'text-primary-500' : 'text-slate-400 group-hover:text-slate-500'}`} />
              {!collapsed && <span>{item.label}</span>}
            </Link>
          )
        })}
      </nav>

      {/* 管理后台入口（仅管理员） */}
      {hasAdmin && (
        <div className="px-2 py-3 border-t border-slate-100">
          <Link
            to="/admin"
            className="flex items-center gap-3 px-3 py-2.5 rounded-xl text-sm font-medium text-slate-500 hover:bg-amber-50 hover:text-amber-700 transition-all duration-150"
            title={collapsed ? '管理后台' : undefined}
          >
            <ShieldCheck className="w-5 h-5 flex-shrink-0 text-amber-500" />
            {!collapsed && <span>管理后台</span>}
          </Link>
        </div>
      )}
    </aside>
  )
}
