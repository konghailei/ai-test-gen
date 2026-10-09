import { useState, useEffect, useCallback } from 'react'
import { Bell, CheckCheck, AlertTriangle, RefreshCw, Loader2, Ban, Zap } from 'lucide-react'
import { settings as settingsApi } from '../api/client'
import type { AdminNotificationItem } from '../types'

/** v3.5.6: 管理员通知消息页（LLM 供应商禁用/全败告警等）。 */
export default function Notifications() {
  const [items, setItems] = useState<AdminNotificationItem[]>([])
  const [unread, setUnread] = useState(0)
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(true)
  const [onlyUnread, setOnlyUnread] = useState(false)
  const [marking, setMarking] = useState(false)

  const load = useCallback(async (p: number) => {
    setLoading(true)
    try {
      const r = await settingsApi.listNotifications({ only_unread: onlyUnread, page: p, page_size: 20 })
      setItems(r.items)
      setUnread(r.unread)
      setTotal(r.total)
      setPage(p)
    } catch {
      // 静默
    } finally {
      setLoading(false)
    }
  }, [onlyUnread])

  useEffect(() => { load(1) }, [load])

  const markAllRead = async () => {
    setMarking(true)
    try {
      await settingsApi.markNotificationsRead(undefined)
      await load(page)
    } finally {
      setMarking(false)
    }
  }

  const iconFor = (t: string) => {
    if (t === 'llm_provider_disabled') return <Ban className="w-4 h-4" style={{ color: 'var(--state-error)' }} />
    if (t === 'llm_all_providers_failed') return <AlertTriangle className="w-4 h-4" style={{ color: 'var(--state-warning)' }} />
    return <Zap className="w-4 h-4" style={{ color: 'var(--brand-500)' }} />
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="page-title flex items-center gap-2">
            <Bell className="w-5 h-5" style={{ color: 'var(--brand-500)' }} /> 通知消息
          </h1>
          <p className="text-sm mt-1" style={{ color: 'var(--foreground-tertiary)' }}>
            系统告警与管理员通知（LLM 供应商禁用、调用全败等）
          </p>
        </div>
        <div className="flex items-center gap-2">
          {unread > 0 && (
            <span className="badge" style={{ background: 'var(--state-error-surface)', color: 'var(--state-error)' }}>
              {unread} 条未读
            </span>
          )}
          <button
            onClick={() => setOnlyUnread(v => !v)}
            className="btn-secondary text-xs py-1.5"
            style={onlyUnread ? { borderColor: 'var(--brand-500)', color: 'var(--brand-500)' } : undefined}
          >
            {onlyUnread ? '只看未读 ✓' : '只看未读'}
          </button>
          <button onClick={markAllRead} disabled={marking || unread === 0} className="btn-secondary text-xs py-1.5">
            {marking ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <CheckCheck className="w-3.5 h-3.5" />}
            全部已读
          </button>
          <button onClick={() => load(page)} className="btn-secondary text-xs py-1.5">
            <RefreshCw className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      <div className="card">
        {loading ? (
          <div className="flex items-center justify-center py-12">
            <Loader2 className="w-6 h-6 animate-spin" style={{ color: 'var(--foreground-tertiary)' }} />
          </div>
        ) : items.length === 0 ? (
          <p className="text-sm text-center py-12" style={{ color: 'var(--foreground-tertiary)' }}>
            {onlyUnread ? '没有未读通知' : '暂无通知'}
          </p>
        ) : (
          <div className="divide-y" style={{ borderColor: 'var(--border)' }}>
            {items.map(n => (
              <div key={n.id} className="py-3 flex items-start gap-3" style={{
                background: n.is_read ? undefined : 'var(--state-warning-surface)',
                borderRadius: n.is_read ? undefined : 'calc(var(--radius) * 0.5)',
                padding: n.is_read ? undefined : '8px',
              }}>
                <div className="mt-0.5">{iconFor(n.type)}</div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <p className="text-sm font-medium" style={{ color: 'var(--foreground-secondary)' }}>{n.title}</p>
                    {!n.is_read && <span className="w-1.5 h-1.5 rounded-full flex-shrink-0" style={{ background: 'var(--state-error)' }} />}
                  </div>
                  <p className="text-xs mt-1 leading-relaxed" style={{ color: 'var(--foreground-tertiary)' }}>{n.content}</p>
                  <p className="text-[10px] mt-1" style={{ color: 'var(--foreground-tertiary)' }}>
                    {n.created_at ? new Date(n.created_at).toLocaleString('zh-CN') : ''}
                    {n.related_id ? ` · ${n.related_id}` : ''}
                  </p>
                </div>
              </div>
            ))}
          </div>
        )}
        {total > 20 && (
          <div className="flex items-center justify-center gap-2 pt-4">
            <button onClick={() => load(page - 1)} disabled={page <= 1} className="btn-secondary text-xs py-1">上一页</button>
            <span className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>{page} / {Math.ceil(total / 20)}</span>
            <button onClick={() => load(page + 1)} disabled={page >= Math.ceil(total / 20)} className="btn-secondary text-xs py-1">下一页</button>
          </div>
        )}
      </div>
    </div>
  )
}
