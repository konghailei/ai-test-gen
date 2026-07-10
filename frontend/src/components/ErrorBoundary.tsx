import { Component, type ErrorInfo, type ReactNode } from 'react'
import { AlertCircle, RefreshCw, Home } from 'lucide-react'

interface Props {
  children: ReactNode
}
interface State {
  hasError: boolean
  error: Error | null
}

/**
 * 全局错误边界 — 捕获子组件渲染期异常，避免整页白屏。
 *
 * 触发场景示例：
 * - 后端返回非预期数据结构导致 .map 抛 TypeError
 * - 组件解构 undefined 字段
 * - 任何渲染期同步异常
 *
 * 异常会被记录到 console，并向用户展示可重试的错误页。
 */
export default class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props)
    this.state = { hasError: false, error: null }
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('[ErrorBoundary] 渲染异常:', error, info.componentStack)
  }

  handleRetry = () => {
    this.setState({ hasError: false, error: null })
  }

  handleGoHome = () => {
    window.location.href = '/'
  }

  render() {
    if (!this.state.hasError) return this.props.children

    return (
      <div className="min-h-screen flex items-center justify-center p-4 bg-slate-50">
        <div className="max-w-md w-full bg-white rounded-2xl shadow-xl p-6 space-y-4">
          <div className="flex items-center gap-3">
            <AlertCircle className="w-6 h-6 text-red-500 flex-shrink-0" />
            <h1 className="text-lg font-semibold text-slate-800">页面渲染异常</h1>
          </div>
          <p className="text-sm text-slate-600">
            页面在渲染过程中发生错误，可能是数据格式异常或网络请求返回了非预期的内容。
            可尝试重试，或返回首页继续操作。
          </p>
          {this.state.error && (
            <pre className="text-xs text-red-600 bg-red-50 rounded-xl p-3 overflow-x-auto whitespace-pre-wrap max-h-40 overflow-y-auto">
              {this.state.error.message}
              {this.state.error.stack && `\n\n${this.state.error.stack.split('\n').slice(0, 3).join('\n')}`}
            </pre>
          )}
          <div className="flex gap-2">
            <button onClick={this.handleRetry} className="btn-primary flex items-center gap-2">
              <RefreshCw className="w-4 h-4" /> 重试
            </button>
            <button onClick={this.handleGoHome} className="btn-secondary flex items-center gap-2">
              <Home className="w-4 h-4" /> 返回首页
            </button>
          </div>
        </div>
      </div>
    )
  }
}
