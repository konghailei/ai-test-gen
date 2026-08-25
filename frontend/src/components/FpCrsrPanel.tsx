import { useEffect, useState } from 'react'
import { Loader2, Layers, AlertTriangle, FileText, ListChecks, ShieldCheck, Database, GitBranch, Link2 } from 'lucide-react'
import { dependencyGraph as graphApi } from '../api/client'
import type { FpCrsrResult } from '../types'
import { brTypeLabel, caseTypeLabel } from '../utils/labels'

// 非功能约束键：英文 → 中文
const NF_LABELS: Record<string, string> = {
  performance: '性能', security: '安全', compatibility: '兼容性',
  accessibility: '可访问性', reliability: '可靠性', usability: '易用性',
  maintainability: '可维护性', scalability: '可扩展性', portability: '可移植性',
}

/**
 * v3.2 功能点权威完整版 SRR（CRSR）展示面板。
 * 跨版本同名功能点字段级聚合（标量取最新、数组去重并集、冲突以最新为主），
 * 依赖图谱节点点击 / 影响分析展开行共用。
 */
export default function FpCrsrPanel({ fpId, compact = false }: { fpId: string; compact?: boolean }) {
  const [data, setData] = useState<FpCrsrResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!fpId) return
    let cancelled = false
    setLoading(true); setError(''); setData(null)
    graphApi.fpCrsr(fpId)
      .then(res => { if (!cancelled) setData(res) })
      .catch((e: any) => { if (!cancelled) setError(e?.message || '加载失败') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [fpId])

  if (loading) {
    return (
      <div className="flex items-center gap-2 py-3 text-xs" style={{ color: 'var(--foreground-tertiary)' }}>
        <Loader2 className="w-3.5 h-3.5 animate-spin" /> 正在加载功能点最新完整 SRR（跨版本聚合）...
      </div>
    )
  }
  if (error) {
    return (
      <div className="flex items-center gap-2 py-2 text-xs" style={{ color: 'var(--state-error)' }}>
        <AlertTriangle className="w-3.5 h-3.5" /> {error}
      </div>
    )
  }
  if (!data) return null

  const crsr = data.crsr || {}
  const steps = crsr.flows?.happy_path?.steps || []
  const altFlows = crsr.flows?.alternate_flows || []
  const rules = crsr.business_rules || []
  const constraints = crsr.data_constraints || []
  const nf = crsr.non_functional && Object.keys(crsr.non_functional).length
    ? crsr.non_functional : null
  const multiVersion = (data.version_count || 0) > 1
  // v3.3 反链数据
  const bl = data.backlinks

  const sectionTitle = (icon: React.ReactNode, text: string, count?: number) => (
    <div className="flex items-center gap-1.5 mb-1.5">
      {icon}
      <span className="text-xs font-semibold" style={{ color: 'var(--foreground-secondary)' }}>{text}</span>
      {typeof count === 'number' && (
        <span className="text-[10px]" style={{ color: 'var(--foreground-quaternary)' }}>（{count}）</span>
      )}
    </div>
  )

  return (
    <div className={compact ? 'space-y-3' : 'space-y-4'}>
      {/* 标题：版本聚合说明 */}
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-1.5">
          <Layers className="w-4 h-4" style={{ color: 'var(--brand-500)' }} />
          <span className="text-xs font-semibold" style={{ color: 'var(--brand-700)' }}>
            {multiVersion ? `最新完整 SRR（跨 ${data.version_count} 个版本聚合，冲突以最新为准）` : '最新完整 SRR'}
          </span>
        </div>
        {(data.stored_version_refs?.length || 0) > 0 && (
          <span className="text-[10px] px-1.5 py-0.5 rounded"
            style={{ background: 'var(--background-muted)', color: 'var(--foreground-tertiary)' }}>
            已固化 {data.stored_version_refs.length} 个归档版本
          </span>
        )}
      </div>

      {/* 功能概述 */}
      {crsr.summary ? (
        <div>
          {sectionTitle(<FileText className="w-3.5 h-3.5" style={{ color: 'var(--brand-400)' }} />, '功能概述')}
          <p className="text-xs whitespace-pre-wrap leading-relaxed px-3 py-2"
            style={{ color: 'var(--foreground-secondary)', background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.5)' }}>
            {crsr.summary}
          </p>
        </div>
      ) : null}

      {/* 正常流程 */}
      {steps.length > 0 && (
        <div>
          {sectionTitle(<GitBranch className="w-3.5 h-3.5" style={{ color: 'var(--brand-400)' }} />, '正常流程', steps.length)}
          <ol className="space-y-1">
            {steps.map((s, i) => (
              <li key={i} className="flex gap-2 text-xs leading-relaxed" style={{ color: 'var(--foreground-secondary)' }}>
                <span className="flex-shrink-0 mt-0.5 w-4 h-4 rounded-full flex items-center justify-center text-[10px] font-medium"
                  style={{ background: 'var(--brand-soft)', color: 'var(--brand-700)' }}>{s.order ?? i + 1}</span>
                <span className="flex-1">
                  {s.action}
                  {s.expect ? <span style={{ color: 'var(--foreground-tertiary)' }}> → {s.expect}</span> : null}
                </span>
              </li>
            ))}
          </ol>
        </div>
      )}

      {/* 备选流程 */}
      {altFlows.length > 0 && (
        <div>
          {sectionTitle(<GitBranch className="w-3.5 h-3.5" style={{ color: 'var(--foreground-quaternary)' }} />, '备选流程', altFlows.length)}
          <div className="space-y-1">
            {altFlows.map((f, i) => (
              <div key={i} className="text-xs px-3 py-2" style={{ color: 'var(--foreground-secondary)', background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.5)' }}>
                {f.name && <span className="font-medium">{f.name}：</span>}
                {f.trigger ? <span>触发 {f.trigger}；</span> : null}
                {f.expect ? <span>预期 {f.expect}</span> : null}
                {!f.name && !f.trigger && !f.expect ? JSON.stringify(f).slice(0, 120) : null}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 业务规则 */}
      {rules.length > 0 && (
        <div>
          {sectionTitle(<ListChecks className="w-3.5 h-3.5" style={{ color: 'var(--brand-400)' }} />, '业务规则', rules.length)}
          <ul className="space-y-1">
            {rules.map((r, i) => (
              <li key={i} className="flex gap-2 text-xs leading-relaxed" style={{ color: 'var(--foreground-secondary)' }}>
                <span className="flex-shrink-0">•</span>
                <span className="flex-1">
                  {r.rule || JSON.stringify(r).slice(0, 120)}
                  {r.type ? (
                    <span className="ml-1.5 text-[10px] px-1 py-0.5 rounded"
                      style={{ background: 'var(--background-muted)', color: 'var(--foreground-tertiary)' }}>
                      {brTypeLabel(r.type)}
                    </span>
                  ) : null}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* 数据约束 */}
      {constraints.length > 0 && (
        <div>
          {sectionTitle(<Database className="w-3.5 h-3.5" style={{ color: 'var(--brand-400)' }} />, '数据约束', constraints.length)}
          <div className="space-y-1">
            {constraints.map((c, i) => (
              <div key={i} className="text-xs px-3 py-2" style={{ color: 'var(--foreground-secondary)', background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.5)' }}>
                <span className="font-medium">{c.field || '字段'}</span>
                {c.required !== undefined && (
                  <span className="ml-1.5 text-[10px]" style={{ color: c.required ? 'var(--state-warning)' : 'var(--foreground-quaternary)' }}>
                    {c.required ? '必填' : '选填'}
                  </span>
                )}
                {c.constraints ? <span style={{ color: 'var(--foreground-tertiary)' }}>：{c.constraints}</span> : null}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 非功能约束 */}
      {nf && (
        <div>
          {sectionTitle(<ShieldCheck className="w-3.5 h-3.5" style={{ color: 'var(--brand-400)' }} />, '非功能约束')}
          <div className="text-xs px-3 py-2" style={{ color: 'var(--foreground-secondary)', background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.5)' }}>
            {Object.entries(nf).map(([k, v]) => String(v ?? '').trim()).filter(Boolean).length > 0 ? (
              <div className="space-y-1">
                {Object.entries(nf)
                  .filter(([, v]) => String(v ?? '').trim())
                  .map(([k, v]) => (
                    <div key={k} className="flex gap-2">
                      <span className="flex-shrink-0 font-medium" style={{ color: 'var(--foreground-tertiary)' }}>{NF_LABELS[k] || k}：</span>
                      <span className="whitespace-pre-wrap">{typeof v === 'object' ? JSON.stringify(v) : v}</span>
                    </div>
                  ))}
              </div>
            ) : (
              <span style={{ color: 'var(--foreground-quaternary)' }}>暂无</span>
            )}
          </div>
        </div>
      )}

      {/* v3.3 反链（Obsidian Backlinks 借鉴）：谁依赖此功能 / 此功能依赖谁 / 用例引用 */}
      {bl && (bl.depended_by.length > 0 || bl.references_fp.length > 0 || bl.referenced_by_cases.length > 0) && (
        <div>
          <div className="flex items-center gap-1.5 mb-1.5">
            <Link2 className="w-3.5 h-3.5" style={{ color: 'var(--brand-400)' }} />
            <span className="text-xs font-semibold" style={{ color: 'var(--foreground-secondary)' }}>引用关系</span>
            <span className="text-[10px]" style={{ color: 'var(--foreground-quaternary)' }}>
              （被依赖 {bl.fan_in} · 依赖 {bl.fan_out}{bl.referenced_by_cases.length > 0 ? ` · 用例 ${bl.referenced_by_cases.length}` : ''}）
            </span>
          </div>
          <div className="space-y-1.5">
            {bl.depended_by.length > 0 && (
              <div>
                <span className="text-[10px]" style={{ color: 'var(--foreground-tertiary)' }}>依赖此功能：</span>
                <div className="flex flex-wrap gap-1.5 mt-1">
                  {bl.depended_by.map(d => (
                    <span key={d.fp_id} className="text-xs px-2 py-1 rounded flex items-center gap-1"
                      style={{ background: 'var(--background-muted)', color: 'var(--foreground-secondary)', border: d.review_label !== '已审核' ? '1px dashed var(--state-warning)' : '1px solid var(--border)' }}
                      title={`所属需求：${d.req_title}${d.review_label !== '已审核' ? `（依赖${d.review_label}）` : ''}${!d.is_active ? '（已冻结）' : ''}`}>
                      {d.fp_name}
                      <span className="text-[10px]" style={{ color: 'var(--foreground-quaternary)' }}>{d.relation_cn}</span>
                      {d.review_label !== '已审核' && (
                        <span className="text-[10px]" style={{ color: 'var(--state-warning)' }}>{d.review_label}</span>
                      )}
                    </span>
                  ))}
                </div>
              </div>
            )}
            {bl.references_fp.length > 0 && (
              <div>
                <span className="text-[10px]" style={{ color: 'var(--foreground-tertiary)' }}>此功能依赖：</span>
                <div className="flex flex-wrap gap-1.5 mt-1">
                  {bl.references_fp.map(d => (
                    <span key={d.fp_id} className="text-xs px-2 py-1 rounded flex items-center gap-1"
                      style={{ background: 'var(--background-muted)', color: 'var(--foreground-secondary)', border: d.review_label !== '已审核' ? '1px dashed var(--state-warning)' : '1px solid var(--border)' }}
                      title={`所属需求：${d.req_title}${d.review_label !== '已审核' ? `（依赖${d.review_label}）` : ''}${!d.is_active ? '（已冻结）' : ''}`}>
                      {d.fp_name}
                      <span className="text-[10px]" style={{ color: 'var(--foreground-quaternary)' }}>{d.relation_cn}</span>
                      {d.review_label !== '已审核' && (
                        <span className="text-[10px]" style={{ color: 'var(--state-warning)' }}>{d.review_label}</span>
                      )}
                    </span>
                  ))}
                </div>
              </div>
            )}
            {bl.referenced_by_cases.length > 0 && (
              <div>
                <span className="text-[10px]" style={{ color: 'var(--foreground-tertiary)' }}>引用此功能的用例：</span>
                <div className="space-y-1 mt-1">
                  {bl.referenced_by_cases.map(c => (
                    <div key={c.case_id} className="text-xs px-2 py-1 flex items-center gap-2"
                      style={{ background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.5)', color: 'var(--foreground-secondary)' }}>
                      <span className="truncate flex-1">{c.title || c.case_id}</span>
                      {c.case_type && (
                        <span className="text-[10px] px-1.5 py-0.5 rounded flex-shrink-0"
                          style={{ background: c.case_type === 'regression' ? 'var(--brand-soft, rgba(0,122,255,0.08))' : 'transparent', color: c.case_type === 'regression' ? 'var(--brand-700, var(--brand-500))' : 'var(--foreground-quaternary)' }}>
                          {caseTypeLabel(c.case_type)}
                        </span>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* 版本归属 */}
      {data.versions.length > 0 && (
        <div>
          {sectionTitle(<Layers className="w-3.5 h-3.5" style={{ color: 'var(--brand-400)' }} />, '版本归属', data.version_count)}
          <div className="space-y-1">
            {data.versions.map((v, i) => (
              <div key={i} className="flex items-center gap-2 text-xs px-3 py-1.5"
                style={{ background: v.is_latest ? 'var(--brand-soft)' : 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.5)' }}>
                <span className="font-medium flex-shrink-0" style={{ color: 'var(--foreground-secondary)' }}>v{v.req_version ?? '?'}</span>
                <span className="flex-1 truncate" style={{ color: v.is_archived ? 'var(--foreground-quaternary)' : 'var(--foreground-secondary)' }}>
                  {v.req_title || v.fp_name}
                </span>
                {v.is_latest && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded font-medium flex-shrink-0"
                    style={{ background: 'var(--brand-soft-strong)', color: 'var(--brand-700)' }}>最新</span>
                )}
                {v.is_archived && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded font-medium flex-shrink-0"
                    style={{ background: 'var(--input)', color: 'var(--foreground-tertiary)' }}>已归档</span>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
