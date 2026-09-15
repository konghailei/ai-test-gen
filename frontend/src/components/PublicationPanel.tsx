import { AlertCircle, CheckCircle2, FileWarning, ShieldAlert, Info } from 'lucide-react'
import type { CompletionStatus, Publication, SSEEvent } from '../types'

/**
 * 发布决策面板（docs/15 §4.1 HITL）。
 *
 * 为什么单独做一块：后端从 v3.4 起把「生成完成」拆成了两个概念——
 *   ① 流水线跑完了（done）
 *   ② 产物获准自动进入权威知识库（publication.decision = auto_approve）
 * 只显示 done 会让用户以为用例已经入库，而实际上可能只到 reviewed、甚至被 blocked。
 * 这里把决策、原因、降级标记、失败子任务都摊开，用户才知道「为什么没自动核准、下一步该做什么」。
 */

const REASON_LABELS: Record<string, string> = {
  empty_generation: '本次没有产出任何用例',
  invalid_case_content: '用例不满足可执行合同（缺步骤/断言等）',
  quality_gate_not_passed: '需求质量门禁未通过',
  quality_gate_forced: '门禁是强制放行（forced_pass）',
  review_llm_failed: '审核 LLM 调用失败，本次审核降级为规则预检',
  review_not_verified: '审核结果未被验证（未走完完整审核）',
  review_case_count_mismatch: '审核用例数与实际产出不一致',
  review_issues_unavailable: '审核问题清单不可用',
  review_blockers: '审核发现 blocker 级问题',
  review_gaps_unresolved: '覆盖缺口未补齐（补生成已达上限）',
  coverage_unknown: '覆盖证据不足（用例未标注 SRR 要点，无法证明覆盖）',
  coverage_insufficient: '要点覆盖率不足 80%',
  high_priority_coverage_incomplete: 'P0/P1 要点未 100% 覆盖',
  invalid_point_references: '用例引用了不存在的要点 ID',
  generation_subtasks_failed: '有功能点/通道生成失败',
  publication_blocked: '内容不合格，已禁止写入知识库',
  kb_publication_incomplete: '知识库写入不完整（PG 与向量库不一致）',
  integration_generate_failed: '跨功能点/集成用例生成失败',
  fp_generate_failed: '部分功能点定向生成失败',
  batch_generate_failed: '全量兜底生成失败',
  fp_load_failed: '功能点加载失败',
  consistency_db_failed: '一致性对比的历史基线查询失败',
  semantic_layer_failed: '澄清语义层失败（仅规则层生效）',
  review_llm_failed_partial: '审核部分维度降级',
}

const STATUS_META: Record<CompletionStatus, { label: string; tone: string; icon: typeof CheckCircle2 }> = {
  completed: { label: '已完成（获准自动入库）', tone: 'emerald', icon: CheckCircle2 },
  awaiting_human_review: { label: '待人工审核（未自动入库）', tone: 'amber', icon: AlertCircle },
  failed: { label: '未通过（未入库）', tone: 'red', icon: ShieldAlert },
}

function reasonLabel(r: string): string {
  return REASON_LABELS[r] || r
}

interface Props {
  completionStatus?: CompletionStatus
  publication?: Publication
  degradedFlags?: string[]
  failedSubtasks?: SSEEvent['failed_subtasks']
}

export default function PublicationPanel({
  completionStatus, publication, degradedFlags = [], failedSubtasks = [],
}: Props) {
  if (!publication && !completionStatus && degradedFlags.length === 0) return null

  const status: CompletionStatus = completionStatus
    || (publication?.decision === 'auto_approve' ? 'completed'
      : publication?.decision === 'blocked' ? 'failed' : 'awaiting_human_review')
  const meta = STATUS_META[status]
  const Icon = meta.icon
  const tone = meta.tone
  const decisionText = publication?.decision === 'auto_approve' ? '自动核准入库'
    : publication?.decision === 'blocked' ? '拦截（禁止入库）'
      : publication?.decision === 'human_review' ? '转人工审核' : '未知'

  const boxClass = tone === 'emerald'
    ? 'border-l-emerald-500 bg-emerald-50/40'
    : tone === 'amber' ? 'border-l-amber-500 bg-amber-50/40' : 'border-l-red-500 bg-red-50/40'
  const headClass = tone === 'emerald' ? 'text-emerald-800'
    : tone === 'amber' ? 'text-amber-800' : 'text-red-800'

  return (
    <div className={`card space-y-3 border-l-4 ${boxClass}`}>
      <div className={`flex items-start gap-2 ${headClass}`}>
        <Icon className="w-5 h-5 shrink-0 mt-0.5" />
        <div className="min-w-0 flex-1">
          <p className="font-semibold text-sm flex flex-wrap items-center gap-2">
            {meta.label}
            <span className="px-2 py-0.5 rounded-full bg-white/70 text-[11px] font-normal">
              发布决策：{decisionText}
            </span>
            {publication?.policy_version && (
              <span className="text-[11px] font-normal opacity-70">策略 {publication.policy_version}</span>
            )}
          </p>

          {(publication?.reasons?.length ?? 0) > 0 && (
            <div className="mt-2">
              <p className="text-xs font-medium flex items-center gap-1">
                <FileWarning className="w-3.5 h-3.5" /> 未自动核准的原因（{publication!.reasons.length}）
              </p>
              <ul className="mt-1 list-disc list-inside text-xs space-y-0.5">
                {publication!.reasons.map((r, i) => <li key={`${r}-${i}`}>{reasonLabel(r)}</li>)}
              </ul>
            </div>
          )}

          {degradedFlags.length > 0 && (
            <div className="mt-2">
              <p className="text-xs font-medium">能力降级标记（{degradedFlags.length}）</p>
              <ul className="mt-1 list-disc list-inside text-xs space-y-0.5">
                {degradedFlags.map((f, i) => <li key={`${f}-${i}`}>{reasonLabel(f)}</li>)}
              </ul>
            </div>
          )}

          {failedSubtasks.length > 0 && (
            <div className="mt-2">
              <p className="text-xs font-medium">失败的功能点 / 通道（{failedSubtasks.length}）</p>
              <ul className="mt-1 list-disc list-inside text-xs space-y-0.5">
                {failedSubtasks.slice(0, 10).map((s, i) => (
                  <li key={i}>{typeof s === 'string' ? s : String((s as Record<string, unknown>).name || (s as Record<string, unknown>).key || JSON.stringify(s))}</li>
                ))}
              </ul>
            </div>
          )}

          <p className="text-xs mt-2 flex items-start gap-1 opacity-80">
            <Info className="w-3.5 h-3.5 shrink-0 mt-0.5" />
            {status === 'completed'
              ? '高置信度用例已自动入库并写入向量库，可被后续生成复用。'
              : status === 'failed'
                ? '本次内容不合格或为空，PG 与向量库均未写入。修正后重新生成即可，历史数据未被污染。'
                : '用例已入库为「待审核」状态，尚未写入向量库、暂不参与后续复用。请在用例库中逐条 approve / reject 后生效。'}
          </p>
        </div>
      </div>
    </div>
  )
}
