import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Sparkles, Key, Eye, EyeOff, Loader2, AlertCircle, CheckCircle2 } from 'lucide-react'
import { useAuth } from '../auth'

const BASE = '/api'

export default function Login() {
  const { login } = useAuth()
  const navigate = useNavigate()
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('888888')
  const [showPwd, setShowPwd] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!username.trim() || !password.trim()) {
      setError('请输入用户名和密码')
      return
    }
    setLoading(true)
    setError('')
    try {
      const res = await fetch(`${BASE}/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: username.trim(), password }),
      })
      const json = await res.json()
      if (!json.success) {
        setError(json.message || '登录失败')
        return
      }
      login(json.data.token, json.data.user)
      navigate('/', { replace: true })
    } catch (e: any) {
      setError('网络错误，请检查服务是否运行')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-slate-900 via-primary-900 to-slate-900 p-4">
      <div className="w-full max-w-md">
        {/* Logo */}
        <div className="text-center mb-8">
          <div className="inline-flex items-center justify-center w-16 h-16 rounded-2xl bg-gradient-to-br from-primary-400 to-accent-400 shadow-lg shadow-primary-500/25 mb-4">
            <Sparkles className="w-8 h-8 text-white" />
          </div>
          <h1 className="text-2xl font-bold text-white">AI-Test-Gen</h1>
          <p className="text-sm text-slate-400 mt-1">AI 驱动测试用例自动生成系统</p>
        </div>

        {/* Card */}
        <div className="bg-white/95 backdrop-blur-md rounded-2xl shadow-2xl shadow-black/20 p-8">
          <h2 className="text-lg font-semibold text-slate-800 mb-1">登录</h2>
          <p className="text-sm text-slate-500 mb-6">请输入您的账户信息</p>

          <form onSubmit={handleSubmit} className="space-y-5">
            {/* 用户名 */}
            <div>
              <label className="block text-sm font-medium text-slate-700 mb-1.5">用户名</label>
              <input
                value={username}
                onChange={e => setUsername(e.target.value)}
                className="input-field"
                placeholder="请输入用户名"
                autoFocus
                disabled={loading}
              />
            </div>

            {/* 密码 */}
            <div>
              <label className="block text-sm font-medium text-slate-700 mb-1.5">密码</label>
              <div className="relative">
                <input
                  type={showPwd ? 'text' : 'password'}
                  value={password}
                  onChange={e => setPassword(e.target.value)}
                  className="input-field pr-11"
                  placeholder="请输入密码"
                  disabled={loading}
                />
                <button
                  type="button"
                  onClick={() => setShowPwd(!showPwd)}
                  className="absolute right-3.5 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600"
                >
                  {showPwd ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                </button>
              </div>
            </div>

            {/* 错误提示 */}
            {error && (
              <div className="flex items-start gap-2.5 p-3 rounded-xl bg-red-50 border border-red-100">
                <AlertCircle className="w-4 h-4 text-red-500 mt-0.5 flex-shrink-0" />
                <span className="text-sm text-red-700">{error}</span>
              </div>
            )}

            {/* 提交 */}
            <button type="submit" disabled={loading} className="btn-primary w-full justify-center py-3">
              {loading ? (
                <><Loader2 className="w-4 h-4 animate-spin" /> 登录中...</>
              ) : (
                <><Key className="w-4 h-4" /> 登录</>
              )}
            </button>
          </form>

          {/* 默认提示 */}
          <div className="mt-6 p-3 rounded-xl bg-slate-50 border border-slate-100">
            <p className="text-xs text-slate-400 text-center">
              默认管理员：admin / 888888（首次启动自动创建）
            </p>
          </div>
        </div>

        {/* Footer */}
        <p className="text-center text-xs text-slate-500 mt-6">
          AI-Test-Gen v1.0.0 — 安全审计版本
        </p>
      </div>
    </div>
  )
}
