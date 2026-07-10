import { Routes, Route, Navigate } from 'react-router-dom'
import { AuthProvider, useAuth } from './auth'
import { ProductProvider, useProduct } from './store/ProductContext'
import Layout from './components/Layout'
import AdminLayout from './components/AdminLayout'
import Login from './pages/Login'
import Dashboard from './pages/Dashboard'
import Requirements from './pages/Requirements'
import RequirementDetail from './pages/RequirementDetail'
import RequirementClarify from './pages/RequirementClarify'
import Generate from './pages/Generate'
import TestCases from './pages/TestCases'
import KnowledgeBase from './pages/KnowledgeBase'
import DependencyGraph from './pages/DependencyGraph'
import ProjectsModules from './pages/ProjectsModules'
import KnowledgeQA from './pages/KnowledgeQA'
import Settings from './pages/Settings'
import Products from './pages/Products'
import PromptManager from './pages/PromptManager'
import UserManagement from './pages/UserManagement'
import RoleManagement from './pages/RoleManagement'
import AdminDashboard from './pages/AdminDashboard'
import AdminKnowledgeBase from './pages/AdminKnowledgeBase'

/** 路由守卫：未登录 → 登录页 */
function AuthGuard({ children }: { children: React.ReactNode }) {
  const { isLoggedIn } = useAuth()
  if (!isLoggedIn) return <Navigate to="/login" replace />
  return <>{children}</>
}

function AdminGuard({ children }: { children: React.ReactNode }) {
  const { user } = useAuth()
  const hasAdmin = (user?.permissions || []).some(p => p === 'admin' || p.startsWith('admin_'))
  if (!hasAdmin) return <Navigate to="/dashboard" replace />
  return <>{children}</>
}

function AdminHomeGuard({ children }: { children: React.ReactNode }) {
  const { user } = useAuth()
  if (!user?.permissions?.length) return <Navigate to="/login" replace />
  return <>{children}</>
}

function RedirectBasedOnRole() {
  const { user } = useAuth()
  const hasAdmin = (user?.permissions || []).some(p => p === 'admin' || p.startsWith('admin_'))
  return <Navigate to={hasAdmin ? '/admin' : '/dashboard'} replace />
}

/** 路由守卫：已登录但未选产品 → 管理员可去产品管理页，普通用户放行 */
function ProductGuard({ children }: { children: React.ReactNode }) {
  const { selectedProductId } = useProduct()
  const { user } = useAuth()
  const isAdmin = (user?.permissions || []).some(p => p === 'admin' || p.startsWith('admin_'))
  if (!selectedProductId && isAdmin) return <Navigate to="/admin" replace />
  return <>{children}</>
}

export default function App() {
  return (
    <AuthProvider>
      <ProductProvider>
      <Routes>
        {/* 登录页 */}
        <Route path="/login" element={<Login />} />

        {/* 旧选择页 → 重定向到管理后台 */}
        <Route path="/select-product" element={<Navigate to="/admin/products" replace />} />

        {/* 登录后默认 → 仪表盘 */}
        <Route path="/" element={<Navigate to="/admin" replace />} />

        {/* 主系统（需选择产品后进入） */}
        <Route element={
          <AuthGuard>
            <ProductGuard>
              <Layout />
            </ProductGuard>
          </AuthGuard>
        }>
          <Route path="/dashboard" element={<Dashboard />} />
          <Route path="/requirements" element={<Requirements />} />
          <Route path="/requirements/:id" element={<RequirementDetail />} />
          <Route path="/requirements/:id/clarify" element={<RequirementClarify />} />
          <Route path="/requirements/:id/generate" element={<Generate />} />
          <Route path="/generate" element={<Generate />} />
          <Route path="/testcases" element={<TestCases />} />
          <Route path="/knowledge" element={<KnowledgeBase />} />
          <Route path="/knowledge-qa" element={<KnowledgeQA />} />
          <Route path="/dependency-graph" element={<DependencyGraph />} />
          <Route path="/projects-modules" element={<ProjectsModules />} />
          <Route path="/settings" element={<Navigate to="/admin/settings" replace />} />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Route>

        {/* 管理后台（不需要选择产品） */}
        <Route path="/admin" element={
          <AuthGuard>
            <AdminHomeGuard>
              <AdminLayout />
            </AdminHomeGuard>
          </AuthGuard>
        }>
          <Route index element={<AdminDashboard />} />
          <Route path="products" element={<AdminGuard><Products /></AdminGuard>} />
          <Route path="prompts" element={<AdminGuard><PromptManager /></AdminGuard>} />
          <Route path="knowledge" element={<AdminGuard><AdminKnowledgeBase /></AdminGuard>} />
          <Route path="users" element={<AdminGuard><UserManagement /></AdminGuard>} />
          <Route path="roles" element={<AdminGuard><RoleManagement /></AdminGuard>} />
          <Route path="settings" element={<AdminGuard><Settings /></AdminGuard>} />
          <Route path="*" element={<Navigate to="/admin" replace />} />
        </Route>
      </Routes>
      </ProductProvider>
    </AuthProvider>
  )
}
