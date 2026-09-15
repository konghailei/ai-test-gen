import { useEffect, useState } from 'react'
import {
  AlertTriangle, BarChart3, Info, RefreshCw, Recycle, TestTube,
} from 'lucide-react'
import { knowledge as knowledgeApi } from '../api/client'

/**
 * 用例库质量报表（docs/14 方向 B / B5）。
 *
 * 四部分：三档占比 / 复用率 / weight 分布 / Top-N 复用与从未复用。
 *
 * ⚠️ **两个刻意保留的"免责说明"**（不是凑字数，是防止误读）：
 *
 *   1. **复用率会显著偏低**（实测 0.32%）。因为 `execution_count` 自 v3.3 才激活，
 *      历史复用未计入。不说明的话用户会误判"用例库完全没被使用"。
 *      因此报表显式展示"应看趋势而非绝对值"。
 *   2. **weight 当前区分度弱**（std 0.058，66% 落在 0.5 档）。因为公式 4 个输入里
 *      2 个尚无数据。展示直方图让用户自己看出这一点，而不是给一个漂亮但无意义的均值。
 */

type CaseReport = Awaited<ReturnType<typeof knowledgeApi.caseQualityReport>>

const STATUS_META: Record<string, { label: string; color: string }> = {
  approved: { label: '已核准', color: 'var(--state-success)' },
  reviewed: { label: '待核准', color: 'var(--brand-500)' },
  draft: { label: '草稿', color: 'var(--foreground-quaternary)' },
  rejected: { label: '已驳回', color: 'var(--state-error)' },
}

export default function CaseQualityReport({ productId }: { productId?: string }) {
  const [data, setData] = useState<CaseReport | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const load = async () => {
    setLoading(true)
    setError('')
    try {
      setData(await knowledgeApi.caseQualityReport(productId))
    } catch (e: any) {
      setError(e?.message || '加载用例库报表失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [productId])

  if (loading && !data) {
    return (
      <div className="rounded-lg border p-3 text-xs flex items-center gap-2"
        style={{ borderColor: 'var(--border)', color: 'var(--foreground-tertiary)' }}>
        <RefreshCw className="w-3.5 h-3.5 animate-spin" /> 正在统计用例库…
      </div>
    )
  }
  if (error) {
    return (
      <div className="rounded-lg border p-3 text-xs flex items-center gap-2"
        style={{ borderColor: 'var(--border)', color: 'var(--state-warning)' }}>
        <AlertTriangle className="w-3.5 h-3.5" /> {error}
        <button className="ml-auto underline" onClick={load}>重试</button>
      </div>
    )
  }
  if (!data || !data.total) return null

  const dist = data.status_distribution || {}
  const ratio = data.status_ratio || {}
  const reuse = data.reuse || ({} as any)
  const w = data.weight || ({} as any)
  const hist = w.histogram || []
  const maxHist = Math.max(...hist.map((h: any) => h.count), 1)
  const reusePct = (reuse.reuse_rate || 0) * 100

  return (
    <div className="rounded-lg border mb-4" style={{ borderColor: 'var(--border)' }}>
      <div className="p-3 flex items-center gap-2 border-b" style={{ borderColor: 'var(--border)' }}>
        <BarChart3 className="w-4 h-4" style={{ color: 'var(--brand-500)' }} />
        <span className="text-sm font-semibold" style={{ color: 'var(--foreground)' }}>
          用例库质量报表
        </span>
        <span className="text-xs" style={{ color: 'var(--foreground-quaternary)' }}>
          共 {data.total} 条
        </span>
        <button className="ml-auto" title="刷新" onClick={load}>
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`}
            style={{ color: 'var(--foreground-quaternary)' }} />
        </button>
      </div>

      <div className="p-3 grid gap-4" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))' }}>
        {/* ① 三档占比 */}
        <div>
          <div className="text-xs font-medium mb-2 flex items-center gap-1.5"
            style={{ color: 'var(--foreground-secondary)' }}>
            <TestTube className="w-3.5 h-3.5" /> 沉淀分级
          </div>
          <div className="space-y-1">
            {Object.keys(dist).sort((a, b) => dist[b] - dist[a]).map((st) => {
              const meta = STATUS_META[st] || { label: st, color: 'var(--foreground-tertiary)' }
              return (
                <div key={st} className="flex items-center gap-2 text-xs">
                  <span className="w-12 flex-shrink-0" style={{ color: meta.color }}>{meta.label}</span>
                  <div className="flex-1 h-2 rounded overflow-hidden"
                    style={{ background: 'var(--background-muted, transparent)' }}>
                    <div className="h-full rounded"
                      style={{ width: `${(ratio[st] || 0) * 100}%`, background: meta.color, opacity: 0.75 }} />
                  </div>
                  <span className="w-16 text-right flex-shrink-0"
                    style={{ color: 'var(--foreground-tertiary)' }}>
                    {dist[st]} ({((ratio[st] || 0) * 100).toFixed(1)}%)
                  </span>
                </div>
              )
            })}
          </div>
        </div>

        {/* ② 复用率 */}
        <div>
          <div className="text-xs font-medium mb-2 flex items-center gap-1.5"
            style={{ color: 'var(--foreground-secondary)' }}>
            <Recycle className="w-3.5 h-3.5" /> 复用率
          </div>
          <div className="flex items-baseline gap-2">
            <span className="text-2xl font-bold" style={{ color: 'var(--brand-500)' }}>
              {reusePct.toFixed(2)}%
            </span>
            <span className="text-xs" style={{ color: 'var(--foreground-quaternary)' }}>
              {reuse.reused_count} / {reuse.approved_total} 条已核准用例被检索命中
            </span>
          </div>
          {/* 必须说明：计数器刚上线，历史复用未计入 */}
          <div className="mt-1.5 text-[11px] flex items-start gap-1"
            style={{ color: 'var(--state-warning)' }}>
            <Info className="w-3 h-3 flex-shrink-0 mt-px" />
            <span>
              {reuse.note || '复用计数自 v3.3 起才开始统计，历史复用未计入。'}
            </span>
          </div>
        </div>

        {/* ③ weight 分布 */}
        <div>
          <div className="text-xs font-medium mb-2 flex items-center gap-1.5"
            style={{ color: 'var(--foreground-secondary)' }}>
            <BarChart3 className="w-3.5 h-3.5" /> 权重分布
            <span style={{ color: 'var(--foreground-quaternary)' }}>
              均值 {Number(w.mean || 0).toFixed(3)} · σ {Number(w.std || 0).toFixed(3)}
            </span>
          </div>
          <div className="flex items-end gap-1 h-16">
            {hist.map((h: any) => (
              <div key={h.bucket} className="flex-1 flex flex-col items-center justify-end gap-1"
                title={`${h.bucket}: ${h.count} 条`}>
                <span className="text-[10px]" style={{ color: 'var(--foreground-quaternary)' }}>
                  {h.count}
                </span>
                <div className="w-full rounded-t"
                  style={{
                    height: `${Math.max((h.count / maxHist) * 44, 2)}px`,
                    background: 'var(--brand-400)', opacity: 0.8,
                  }} />
                <span className="text-[10px]" style={{ color: 'var(--foreground-quaternary)' }}>
                  {h.bucket}
                </span>
              </div>
            ))}
          </div>
          <div className="mt-1.5 text-[11px]" style={{ color: 'var(--foreground-quaternary)' }}>
            {w.note}
          </div>
        </div>
      </div>

      {/* ④ Top-N 复用 / 从未复用 */}
      <div className="px-3 pb-3 grid gap-4" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))' }}>
        <div>
          <div className="text-xs font-medium mb-1.5" style={{ color: 'var(--foreground-secondary)' }}>
            复用最多
          </div>
          {(data.top_reused || []).length === 0 ? (
            <div className="text-[11px]" style={{ color: 'var(--foreground-quaternary)' }}>
              暂无复用记录
            </div>
          ) : (
            <div className="space-y-0.5">
              {(data.top_reused || []).slice(0, 5).map((c: any) => (
                <div key={c.id} className="flex items-center gap-2 text-[11px]">
                  <span className="flex-shrink-0 px-1 rounded"
                    style={{ color: 'var(--brand-500)', border: '1px solid var(--brand-500)' }}>
                    {c.execution_count}×
                  </span>
                  <span className="truncate" style={{ color: 'var(--foreground-tertiary)' }}>
                    {c.title}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
        <div>
          <div className="text-xs font-medium mb-1.5" style={{ color: 'var(--foreground-secondary)' }}>
            从未复用
            <span className="ml-1 font-normal" style={{ color: 'var(--foreground-quaternary)' }}>
              {data.never_reused?.count ?? 0} 条已核准用例
            </span>
          </div>
          <div className="text-[11px]" style={{ color: 'var(--foreground-quaternary)' }}>
            这些用例已入库但未被检索命中过——可能是覆盖了低频场景，
            也可能是与现有用例重复。可结合权重排序评估是否需要精简。
          </div>
        </div>
      </div>
    </div>
  )
}
