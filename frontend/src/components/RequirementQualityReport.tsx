import { useEffect, useState } from 'react'
import {
  Activity, AlertTriangle, CheckCircle2, ChevronDown, ChevronRight,
  Gauge, Layers, MessageCircle, RefreshCw,
} from 'lucide-react'
import { requirements as requirementApi } from '../api/client'

/**
 * 需求质量报表（docs/14 二期 A5）。
 *
 * 目的：让「需求写得好不好」对用户可见 —— 没有度量就没有改进动力。
 *
 * 三块内容：
 *   1. 结构完整度：分数 + **缺口清单**（按权重降序，权重高=影响大，优先补）
 *   2. 质量分拆解：完整性 / 歧义 / 分数来源（规则公式 vs LLM 自评）
 *   3. 澄清轮次与收敛原因
 *
 * ⚠️ 两个刻意的展示选择：
 *   - **结构分低不等于需求不能用**，因此文案是"建议补充"而非"不合格"，
 *     与后端门禁"只 Warning 不 Blocker"的口径一致（docs/14 §A4）。
 *   - `quality.breakdown_available=false` 时（存量需求）**不显示 0**，
 *     而是明确说明"拆解数据需重新解析后可用"，避免把"没有数据"读成"得 0 分"。
 */

type QualityReport = Awaited<ReturnType<typeof requirementApi.getQualityReport>>

/** 结构分档位 → 颜色。与后端 STRUCTURE_WARN_THRESHOLD(0.60) 对齐。 */
function scoreTone(score: number) {
  if (score >= 0.80) return { color: 'var(--state-success)', label: '良好' }
  if (score >= 0.60) return { color: 'var(--brand-500)', label: '尚可' }
  if (score >= 0.35) return { color: 'var(--state-warning)', label: '偏低' }
  return { color: 'var(--state-error)', label: '很简略' }
}

export default function RequirementQualityReport({ reqId }: { reqId: string }) {
  const [data, setData] = useState<QualityReport | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string>('')
  const [expanded, setExpanded] = useState(false)

  const load = async () => {
    if (!reqId) return
    setLoading(true)
    setError('')
    try {
      setData(await requirementApi.getQualityReport(reqId))
    } catch (e: any) {
      setError(e?.message || '加载质量报表失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reqId])

  if (loading && !data) {
    return (
      <div className="rounded-lg border p-4 text-xs flex items-center gap-2"
        style={{ borderColor: 'var(--border)', color: 'var(--foreground-tertiary)' }}>
        <RefreshCw className="w-3.5 h-3.5 animate-spin" /> 正在计算质量报表…
      </div>
    )
  }
  if (error) {
    return (
      <div className="rounded-lg border p-4 text-xs flex items-center gap-2"
        style={{ borderColor: 'var(--border)', color: 'var(--state-warning)' }}>
        <AlertTriangle className="w-3.5 h-3.5" /> {error}
        <button className="ml-auto underline" onClick={load}>重试</button>
      </div>
    )
  }
  if (!data) return null

  const st = data.structure || ({} as any)
  const score = typeof st.score === 'number' ? st.score : 0
  const tone = scoreTone(score)
  const missing = (st.missing || []) as any[]
  const q = data.quality || ({} as any)
  const cl = data.clarify || ({} as any)

  return (
    <div className="rounded-lg border" style={{ borderColor: 'var(--border)' }}>
      {/* 头部：总分 */}
      <div className="p-4 flex items-center gap-3 border-b" style={{ borderColor: 'var(--border)' }}>
        <Gauge className="w-4 h-4 flex-shrink-0" style={{ color: tone.color }} />
        <div className="min-w-0">
          <div className="text-sm font-semibold" style={{ color: 'var(--foreground)' }}>
            需求质量报表
          </div>
          <div className="text-xs mt-0.5" style={{ color: 'var(--foreground-tertiary)' }}>
            {st.summary || '—'}
          </div>
        </div>
        <div className="ml-auto text-right flex-shrink-0">
          <div className="text-2xl font-bold leading-none" style={{ color: tone.color }}>
            {Math.round(score * 100)}%
          </div>
          <div className="text-[11px] mt-0.5" style={{ color: 'var(--foreground-quaternary)' }}>
            结构完整度 · {tone.label}
          </div>
        </div>
        <button className="ml-2 flex-shrink-0" title="重新计算" onClick={load}>
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`}
            style={{ color: 'var(--foreground-quaternary)' }} />
        </button>
      </div>

      {/* 结构缺口清单 */}
      <div className="p-4 space-y-2">
        <div className="flex items-center gap-2 text-xs font-medium"
          style={{ color: 'var(--foreground-secondary)' }}>
          <Layers className="w-3.5 h-3.5" />
          结构缺口
          {missing.length > 0 ? (
            <span style={{ color: 'var(--state-warning)' }}>
              {missing.length} 项建议补充
            </span>
          ) : (
            <span className="flex items-center gap-1" style={{ color: 'var(--state-success)' }}>
              <CheckCircle2 className="w-3 h-3" /> 无缺口
            </span>
          )}
          {typeof st.fp_count === 'number' && (
            <span style={{ color: 'var(--foreground-quaternary)' }}>
              · 已纳入 {st.fp_count} 个功能点子需求
            </span>
          )}
        </div>

        {missing.length > 0 && (
          <div className="space-y-1">
            {(expanded ? missing : missing.slice(0, 4)).map((m: any) => (
              <div key={m.key} className="flex items-start gap-2 text-xs">
                <span className="flex-shrink-0 px-1 py-px rounded text-[10px] leading-4"
                  style={{
                    color: 'var(--state-warning)',
                    border: '1px solid var(--state-warning)',
                  }}>
                  权重 {m.weight}
                </span>
                <div className="min-w-0">
                  <span style={{ color: 'var(--foreground-secondary)' }}>{m.label}</span>
                  {m.hint && (
                    <span className="ml-1" style={{ color: 'var(--foreground-quaternary)' }}>
                      （取数位置：{m.hint}）
                    </span>
                  )}
                </div>
              </div>
            ))}
            {missing.length > 4 && (
              <button className="text-xs flex items-center gap-1"
                style={{ color: 'var(--brand-500)' }}
                onClick={() => setExpanded(!expanded)}>
                {expanded ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
                {expanded ? '收起' : `展开其余 ${missing.length - 4} 项`}
              </button>
            )}
          </div>
        )}

        {/* 不适用豁免：解释"为什么这项没算分"，避免用户以为漏了 */}
        {(st.not_applicable || []).length > 0 && (
          <div className="text-[11px] pt-1" style={{ color: 'var(--foreground-quaternary)' }}>
            已按适用性豁免：
            {(st.not_applicable || []).map((n: any) => `${n.label}（${n.reason}）`).join('；')}
          </div>
        )}

        {st.score < 0.60 && (
          <div className="text-[11px] pt-1" style={{ color: 'var(--foreground-tertiary)' }}>
            结构分偏低只作提示，<strong>不阻断</strong>需求流转 —— 可按需补充后再生成用例。
          </div>
        )}
      </div>

      {/* 质量分拆解 */}
      <div className="px-4 pb-4 space-y-2">
        <div className="flex items-center gap-2 text-xs font-medium"
          style={{ color: 'var(--foreground-secondary)' }}>
          <Activity className="w-3.5 h-3.5" /> 质量分拆解
        </div>
        {q.overall_score != null && (
          <div className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>
            总分 <strong style={{ color: 'var(--foreground)' }}>{Number(q.overall_score).toFixed(2)}</strong>
            {q.score_source && <> · 来源：{q.score_source}</>}
          </div>
        )}
        {q.breakdown_available ? (
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs"
            style={{ color: 'var(--foreground-tertiary)' }}>
            {q.completeness_score != null && <span>完整性 {Number(q.completeness_score).toFixed(2)}</span>}
            {q.ambiguity_score != null && <span>歧义度 {Number(q.ambiguity_score).toFixed(2)}</span>}
            {q.ambiguity_count != null && <span>歧义项 {q.ambiguity_count} 条</span>}
          </div>
        ) : (
          <div className="text-[11px]" style={{ color: 'var(--foreground-quaternary)' }}>
            拆解数据需重新解析后可用（该需求解析时尚未启用质量分拆解）。
          </div>
        )}
      </div>

      {/* 澄清轮次与收敛 */}
      <div className="px-4 pb-4 space-y-2">
        <div className="flex items-center gap-2 text-xs font-medium"
          style={{ color: 'var(--foreground-secondary)' }}>
          <MessageCircle className="w-3.5 h-3.5" /> 澄清收敛
          <span style={{ color: 'var(--foreground-quaternary)' }}>
            共 {cl.total_rounds || 0} 轮
          </span>
        </div>
        {(cl.rounds || []).length > 0 && (
          <div className="flex flex-wrap gap-2">
            {(cl.rounds || []).map((r: any) => (
              <span key={r.round} className="text-[11px] px-2 py-0.5 rounded"
                style={{ color: 'var(--foreground-tertiary)', background: 'var(--surface-subtle, transparent)', border: '1px solid var(--border)' }}>
                第 {r.round} 轮：{r.questions_count} 问 / {r.answers_count} 答
              </span>
            ))}
          </div>
        )}
        {cl.force_passed && (
          <div className="text-[11px]" style={{ color: 'var(--state-warning)' }}>
            该需求曾<strong>强制通过</strong>澄清{cl.force_passed_reason ? `：${cl.force_passed_reason}` : ''}
          </div>
        )}
      </div>
    </div>
  )
}
