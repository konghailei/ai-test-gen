import { useState, useEffect } from 'react'
import { LayoutDashboard, Package, Users, ArrowRight, RefreshCw } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { apiGet, apiPost } from '../api/client'
import { useProduct } from '../store/ProductContext'

export default function AdminDashboard() {
  const [data, setData] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const { setSelectedProduct } = useProduct()
  const navigate = useNavigate()

  useEffect(() => {
    apiGet('/products/admin/dashboard').then(d => setData(d)).finally(() => setLoading(false))
  }, [])

  // v3.4：向量出箱积压巡检（PG↔Milvus 一致性是否健康，一眼可见）
  const [outbox, setOutbox] = useState<any>(null)
  const [dispatching, setDispatching] = useState(false)
  const loadOutbox = () => apiGet('/observability/vector-outbox').then(d => setOutbox(d)).catch(() => setOutbox(null))
  useEffect(() => { loadOutbox(); const t = setInterval(loadOutbox, 30000); return () => clearInterval(t) }, [])
  const handleDispatch = async () => {
    setDispatching(true)
    try { await apiPost('/observability/vector-outbox/dispatch'); loadOutbox() } catch { /* 失败留待下轮自动重试 */ }
    finally { setDispatching(false) }
  }

  const enterProduct = (product: any) => {
    setSelectedProduct(product.id, product.name)
    navigate('/dashboard', { replace: true })
  }

  if (loading) return <div className="p-6">加载中...</div>
  if (!data) return <div className="p-6" style={{ color: 'var(--foreground-tertiary)' }}>加载失败</div>

  return (
    <div className="space-y-6">
      <div>
        <h1 className="page-title flex items-center gap-2">
          <LayoutDashboard className="w-6 h-6" style={{ color: 'var(--brand-500)' }} />
          管理后台
        </h1>
        <p className="text-sm mt-1" style={{ color: 'var(--foreground-tertiary)' }}>系统概览与产品入口</p>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
        <div className="card flex items-center gap-4">
          <div className="w-10 h-10 flex items-center justify-center" style={{ background: 'rgba(0, 122, 255, 0.12)', borderRadius: 'calc(var(--radius) * 0.75)' }}>
            <Package className="w-5 h-5" style={{ color: 'var(--brand-500)' }} />
          </div>
          <div><p className="text-2xl font-bold" style={{ color: 'var(--foreground)' }}>{data.total_products}</p><p className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>产品</p></div>
        </div>
        <div className="card flex items-center gap-4">
          <div className="w-10 h-10 flex items-center justify-center" style={{ background: 'var(--state-success-surface)', borderRadius: 'calc(var(--radius) * 0.75)' }}>
            <Users className="w-5 h-5" style={{ color: 'var(--state-success)' }} />
          </div>
          <div><p className="text-2xl font-bold" style={{ color: 'var(--foreground)' }}>{data.total_users}</p><p className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>用户</p></div>
        </div>
        <div className="card flex items-center gap-4">
          <div className="w-10 h-10 flex items-center justify-center" style={{ background: 'rgba(149, 0, 255, 0.12)', borderRadius: 'calc(var(--radius) * 0.75)' }}>
            <Package className="w-5 h-5" style={{ color: '#af52de' }} />
          </div>
          <div><p className="text-2xl font-bold" style={{ color: 'var(--foreground)' }}>{(data.products || []).length}</p><p className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>可访问</p></div>
        </div>
      </div>

      {outbox && (
        <div className="card p-5">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-semibold" style={{ color: 'var(--foreground-secondary)' }}>
              系统健康 · 向量出箱（PG→Milvus 一致性）
            </h2>
            <button onClick={handleDispatch} disabled={dispatching}
              className="btn-secondary text-xs flex items-center gap-1" style={{ opacity: dispatching ? 0.6 : 1 }}>
              <RefreshCw className={'w-3 h-3' + (dispatching ? ' animate-spin' : '')} /> 立即投递
            </button>
          </div>
          <div className="flex gap-6 text-sm mb-2">
            <span>待投递 <b>{outbox.summary?.by_status?.pending ?? 0}</b></span>
            <span>已投递 <b>{outbox.summary?.by_status?.done ?? 0}</b></span>
            <span style={{ color: (outbox.summary?.by_status?.failed ?? 0) > 0 ? 'var(--state-danger)' : undefined }}>
              失败待对账 <b>{outbox.summary?.by_status?.failed ?? 0}</b>
            </span>
            <span className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>
              最早未完成：{outbox.summary?.oldest_unfinished || '—'}
            </span>
          </div>
          {(outbox.recent || []).length > 0 && (
            <table className="w-full text-xs">
              <tbody>
                {outbox.recent.slice(0, 5).map((r: any) => (
                  <tr key={r.event_id} className="border-t" style={{ borderColor: 'var(--border)' }}>
                    <td className="py-1.5 pr-3" style={{ color: 'var(--foreground-tertiary)' }}>{r.aggregate_id}</td>
                    <td className="py-1.5 pr-3">{r.action}</td>
                    <td className="py-1.5 pr-3">{r.status}</td>
                    <td className="py-1.5 truncate" style={{ color: 'var(--foreground-tertiary)', maxWidth: 420 }}>{r.error || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      <div className="card p-0 overflow-hidden">
        <div className="px-5 py-3.5 border-b" style={{ borderColor: 'var(--border)', background: 'var(--background-muted)' }}>
          <h2 className="text-sm font-semibold" style={{ color: 'var(--foreground-secondary)' }}>可访问产品</h2>
        </div>
        {(data.products || []).length === 0 ? (
          <div className="p-8 text-center text-sm" style={{ color: 'var(--foreground-tertiary)' }}>暂无产品</div>
        ) : (
          <table className="w-full">
            <tbody>
              {(data.products || []).map((p: any) => (
                <tr key={p.id} className="border-b transition-colors hover:bg-[var(--background-muted)]" style={{ borderColor: 'var(--border)' }}>
                  <td className="px-5 py-3.5">
                    <p className="text-sm font-semibold" style={{ color: 'var(--foreground)' }}>{p.name}</p>
                    <p className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>{p.description || '无描述'}</p>
                  </td>
                  <td className="px-5 py-3.5 text-sm text-right" style={{ color: 'var(--foreground-tertiary)' }}>{p.requirement_count} 条需求</td>
                  <td className="px-5 py-3.5 text-right">
                    <button onClick={() => enterProduct(p)} className="btn-primary text-xs flex items-center gap-1">
                      进入 <ArrowRight className="w-3 h-3" />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
