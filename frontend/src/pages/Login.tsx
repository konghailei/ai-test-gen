import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { Sparkles, Brain, FileText, BookOpen, GitBranch, User, Lock, Eye, EyeOff, LogIn, Loader2, AlertCircle } from 'lucide-react'
import { useAuth } from '../auth'
import { health } from '../api/client'

const BASE = '/api'

export default function Login() {
  const { login } = useAuth()
  const navigate = useNavigate()
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('888888')
  const [showPwd, setShowPwd] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  // 版本号统一取自后端 /health 接口（app.__version__），避免硬编码导致与其它页面不一致
  const [version, setVersion] = useState('')

  useEffect(() => {
    health()
      .then((h) => setVersion(h.version || ''))
      .catch(() => {})
  }, [])

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
    <div className="flex h-screen overflow-hidden" style={{ fontFamily: 'var(--font-sans)' }}>
      {/* 左侧品牌展示区 */}
      <div
        className="relative overflow-hidden hidden md:flex flex-col justify-center"
        style={{
          flex: 1,
          padding: '64px 56px',
          background: 'linear-gradient(135deg, var(--brand-800) 0%, var(--brand-600) 45%, var(--brand-500) 100%)',
        }}
      >
        {/* 装饰光晕 */}
        <div
          style={{
            position: 'absolute', top: '-20%', right: '-10%',
            width: 500, height: 500, borderRadius: '50%',
            background: 'radial-gradient(circle, rgba(46, 141, 255, 0.3), transparent 70%)',
            pointerEvents: 'none',
          }}
        />
        <div
          style={{
            position: 'absolute', bottom: '-15%', left: '-5%',
            width: 400, height: 400, borderRadius: '50%',
            background: 'radial-gradient(circle, rgba(88, 86, 214, 0.2), transparent 70%)',
            pointerEvents: 'none',
          }}
        />

        <div className="relative z-10" style={{ maxWidth: 440 }}>
          {/* Logo */}
          <div className="flex items-center gap-3 mb-12">
            <div
              className="flex items-center justify-center flex-shrink-0"
              style={{
                width: 44, height: 44,
                borderRadius: 'calc(var(--radius) * 0.6)',
                background: 'rgba(255, 255, 255, 0.15)',
                backdropFilter: 'blur(10px)',
              }}
            >
              <Sparkles className="w-6 h-6 text-white" />
            </div>
            <span className="text-white font-semibold" style={{ fontSize: 18, letterSpacing: '-0.01em' }}>
              AI测试生成
            </span>
          </div>

          {/* 标语 */}
          <h1 className="text-white m-0 mb-4" style={{ fontSize: 40, fontWeight: 700, lineHeight: 1.15, letterSpacing: '-0.03em' }}>
            AI 驱动的<br />测试用例自动生成
          </h1>
          <p className="text-white m-0 mb-12" style={{ fontSize: 16, lineHeight: 1.6, opacity: 0.75 }}>
            基于大语言模型与知识库的智能测试平台，从需求分析到用例生成，全流程自动化覆盖。
          </p>

          {/* 特色列表 */}
          <div className="flex flex-col gap-5">
            {[
              { icon: Brain, text: '智能需求分析与澄清问答' },
              { icon: FileText, text: '一键生成结构化测试用例' },
              { icon: BookOpen, text: '知识库持续学习与积累' },
              { icon: GitBranch, text: '需求依赖图谱可视化' },
            ].map((f, i) => (
              <div key={i} className="flex items-center gap-3.5">
                <div
                  className="flex items-center justify-center flex-shrink-0"
                  style={{
                    width: 36, height: 36,
                    borderRadius: 'calc(var(--radius) * 0.4)',
                    background: 'rgba(255, 255, 255, 0.12)',
                  }}
                >
                  <f.icon className="w-[18px] h-[18px] text-white" />
                </div>
                <span className="text-white font-medium" style={{ fontSize: 14, opacity: 0.85 }}>{f.text}</span>
              </div>
            ))}
          </div>
        </div>

        <div className="absolute z-10" style={{ bottom: 32, left: 56, fontSize: 12, color: 'rgba(255, 255, 255, 0.5)' }}>
          AI测试用例生成系统{version ? ` v${version}` : ''}
        </div>
      </div>

      {/* 右侧登录表单区 */}
      <div
        className="flex flex-col justify-center overflow-y-auto"
        style={{
          width: 520,
          flexShrink: 0,
          padding: '64px 56px',
          background: 'var(--background)',
        }}
      >
        <div style={{ marginBottom: 40 }}>
          <h2 className="m-0 mb-2" style={{ fontSize: 26, fontWeight: 700, color: 'var(--foreground)', letterSpacing: '-0.02em', lineHeight: 1.2 }}>
            欢迎回来
          </h2>
          <p className="m-0" style={{ fontSize: 14, color: 'var(--foreground-tertiary)', lineHeight: 1.5 }}>
            请输入您的账户信息以继续
          </p>
        </div>

        <form onSubmit={handleSubmit} className="flex flex-col gap-5">
          {/* 用户名 */}
          <div className="flex flex-col gap-2">
            <label className="font-semibold" style={{ fontSize: 13, color: 'var(--foreground)' }} htmlFor="email-input">用户名</label>
            <div
              className="flex items-center gap-2.5 transition-all"
              style={{
                height: 48,
                padding: '0 16px',
                border: '1px solid var(--input)',
                borderRadius: 'calc(var(--radius) * 0.6)',
                background: 'var(--background)',
              }}
            >
              <User className="w-[18px] h-[18px] flex-shrink-0" style={{ color: 'var(--foreground-tertiary)' }} />
              <input
                id="email-input"
                value={username}
                onChange={e => setUsername(e.target.value)}
                type="text"
                placeholder="请输入用户名"
                autoFocus
                disabled={loading}
                className="flex-1 min-w-0 border-0 outline-none bg-transparent"
                style={{ color: 'var(--foreground)', fontSize: 14, fontFamily: 'var(--font-sans)' }}
              />
            </div>
          </div>

          {/* 密码 */}
          <div className="flex flex-col gap-2">
            <label className="font-semibold" style={{ fontSize: 13, color: 'var(--foreground)' }} htmlFor="password-input">密码</label>
            <div
              className="flex items-center gap-2.5 transition-all"
              style={{
                height: 48,
                padding: '0 16px',
                border: '1px solid var(--input)',
                borderRadius: 'calc(var(--radius) * 0.6)',
                background: 'var(--background)',
              }}
            >
              <Lock className="w-[18px] h-[18px] flex-shrink-0" style={{ color: 'var(--foreground-tertiary)' }} />
              <input
                id="password-input"
                value={password}
                onChange={e => setPassword(e.target.value)}
                type={showPwd ? 'text' : 'password'}
                placeholder="请输入密码"
                disabled={loading}
                className="flex-1 min-w-0 border-0 outline-none bg-transparent"
                style={{ color: 'var(--foreground)', fontSize: 14, fontFamily: 'var(--font-sans)' }}
              />
              <button
                type="button"
                onClick={() => setShowPwd(!showPwd)}
                className="border-0 bg-transparent cursor-pointer flex-shrink-0"
                style={{ color: 'var(--foreground-tertiary)', padding: 0, display: 'flex', alignItems: 'center' }}
                aria-label="切换密码显示"
              >
                {showPwd ? <EyeOff className="w-[18px] h-[18px]" /> : <Eye className="w-[18px] h-[18px]" />}
              </button>
            </div>
          </div>

          {/* 错误提示 */}
          {error && (
            <div
              className="flex items-start gap-2.5 animate-slide-up"
              style={{
                padding: '10px 12px',
                borderRadius: 'calc(var(--radius) * 0.5)',
                background: 'var(--state-error-surface)',
                border: '1px solid rgba(255, 59, 48, 0.2)',
              }}
            >
              <AlertCircle className="w-4 h-4 mt-0.5 flex-shrink-0" style={{ color: 'var(--state-error)' }} />
              <span className="text-sm" style={{ color: 'var(--state-error)' }}>{error}</span>
            </div>
          )}

          {/* 记住我 + 忘记密码 */}
          <div className="flex items-center justify-between">
            <label className="flex items-center gap-2 cursor-pointer" style={{ fontSize: 13, color: 'var(--foreground)' }}>
              <input type="checkbox" className="cursor-pointer" style={{ width: 16, height: 16, accentColor: 'var(--brand-500)' }} />
              <span>记住我</span>
            </label>
            <a href="#" onClick={(e) => e.preventDefault()} style={{ fontSize: 13, color: 'var(--brand-500)', textDecoration: 'none', fontWeight: 500 }}>
              忘记密码?
            </a>
          </div>

          {/* 登录按钮 */}
          <button
            type="submit"
            disabled={loading}
            className="flex items-center justify-center gap-2 w-full transition-all"
            style={{
              height: 48,
              border: 0,
              borderRadius: 'calc(var(--radius) * 0.6)',
              background: 'var(--brand-500)',
              color: '#ffffff',
              fontSize: 15,
              fontWeight: 600,
              fontFamily: 'var(--font-sans)',
              cursor: loading ? 'not-allowed' : 'pointer',
              opacity: loading ? 0.7 : 1,
              marginTop: 4,
              boxShadow: 'var(--shadow-apple-sm)',
            }}
            onMouseEnter={(e) => { if (!loading) e.currentTarget.style.background = 'var(--brand-600)' }}
            onMouseLeave={(e) => { if (!loading) e.currentTarget.style.background = 'var(--brand-500)' }}
          >
            {loading ? (
              <><Loader2 className="w-[18px] h-[18px] animate-spin" /> 登录中...</>
            ) : (
              <><LogIn className="w-[18px] h-[18px]" /> <span>登录</span></>
            )}
          </button>
        </form>

        {/* 默认提示 */}
        <div
          className="text-center"
          style={{
            marginTop: 32,
            padding: 16,
            borderRadius: 'calc(var(--radius) * 0.5)',
            background: 'var(--background-muted)',
          }}
        >
          <p className="m-0" style={{ fontSize: 12, color: 'var(--foreground-tertiary)', lineHeight: 1.6 }}>
            默认管理员：<strong style={{ color: 'var(--foreground)', fontWeight: 600 }}>admin</strong> / <strong style={{ color: 'var(--foreground)', fontWeight: 600 }}>888888</strong>（首次启动自动创建）
          </p>
        </div>
      </div>

      {/* 移动端品牌信息（md 以下显示） */}
      <style>{`
        @media (max-width: 960px) {
          .login-brand-panel { display: none !important; }
        }
        @media (prefers-reduced-motion: reduce) {
          * { transition-duration: 0.01ms !important; }
        }
      `}</style>
    </div>
  )
}
