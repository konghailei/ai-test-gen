import { useState, useEffect, useCallback } from 'react'
import {
  CheckCircle2, XCircle, ClipboardCheck, Loader2, Filter, ChevronLeft, ChevronRight,
  Layers, Tag, Clock, User, AlertCircle, MessageSquare, Eye, EyeOff,
} from 'lucide-react'
import { knowledgeQA, products as prodApi } from '../api/client'
import { useProduct } from '../store/ProductContext'
import type { KbQARecord, Module } from '../types'
import LoadingSpinner from '../components/LoadingSpinner'

// ============================================================================
// 工具函数
// ============================================================================

function formatDateTime(iso?: string | null): string {
  if (!iso) return ''
  try {
    const d = new Date(iso)
    const pad = (n: number) => String(n).padStart(2, '0')
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
  } catch {
    return iso.slice(0, 16).replace('T', ' ')
  }
}

const STATUS_LABEL: Record<string, string> = {
  pending_review: '待审核',
  approved: '已通过',
  rejected: '已拒绝',
  none: '未纠正',
}

const STATUS_COLOR: Record<string, { bg: string; fg: string }> = {
  pending_review: { bg: 'var(--state-warning-surface)', fg: 'var(--state-warning)' },
  approved: { bg: 'var(--state-success-surface)', fg: 'var(--state-success)' },
  rejected: { bg: 'var(--state-error-surface)', fg: 'var(--state-error)' },
  none: { bg: 'var(--background-muted)', fg: 'var(--foreground-tertiary)' },
}

// ============================================================================
// 主组件
// ============================================================================

export default function CorrectionReview() {
  const { selectedProductId } = useProduct()

  // 筛选条件
  const [statusFilter, setStatusFilter] = useState<string>('pending_review')
  const [moduleFilter, setModuleFilter] = useState<string>('')
  const [page, setPage] = useState(1)
  const pageSize = 10

  // 数据
  const [records, setRecords] = useState<KbQARecord[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  // 模块列表（用于筛选下拉）
  const [modules, setModules] = useState<Module[]>([])

  // 展开原回答的记录 id 集合
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set())

  // 审核操作状态
  const [reviewing, setReviewing] = useState<{ record: KbQARecord; action: 'approve' | 'reject' } | null>(null)
  const [reviewNotes, setReviewNotes] = useState('')
  const [submitting, setSubmitting] = useState(false)

  // 提示
  const [toast, setToast] = useState<{ type: 'success' | 'error' | 'warning'; msg: string } | null>(null)

  // ============================================================================
  // 数据加载
  // ============================================================================

  const loadCorrections = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const resp = await knowledgeQA.corrections({
        status: statusFilter,
        module: moduleFilter,
        product_id: selectedProductId || '',
        page,
        page_size: pageSize,
      })
      setRecords(resp.items || [])
      setTotal(resp.total || 0)
    } catch (e: any) {
      setError(e?.message || '加载失败')
      setRecords([])
      setTotal(0)
    } finally {
      setLoading(false)
    }
  }, [statusFilter, moduleFilter, selectedProductId, page])

  // 加载模块列表（用于筛选下拉）
  useEffect(() => {
    if (!selectedProductId) {
      setModules([])
      return
    }
    prodApi.listModules(selectedProductId).then((mods: Module[]) => {
      setModules(mods || [])
    }).catch(() => setModules([]))
  }, [selectedProductId])

  // 初始加载 + 筛选条件变化时重新加载
  useEffect(() => {
    loadCorrections()
  }, [loadCorrections])

  // 切换筛选条件时回到第一页
  useEffect(() => {
    setPage(1)
  }, [statusFilter, moduleFilter, selectedProductId])

  // toast 自动消失
  useEffect(() => {
    if (!toast) return
    const t = setTimeout(() => setToast(null), 4000)
    return () => clearTimeout(t)
  }, [toast])

  // ============================================================================
  // 操作
  // ============================================================================

  const toggleExpand = (id: string) => {
    setExpandedIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const startReview = (record: KbQARecord, action: 'approve' | 'reject') => {
    setReviewing({ record, action })
    setReviewNotes('')
  }

  const submitReview = async () => {
    if (!reviewing) return
    setSubmitting(true)
    try {
      if (reviewing.action === 'approve') {
        const r = await knowledgeQA.approve(reviewing.record.id, reviewNotes.trim())
        setToast({
          type: 'success',
          msg: `审核通过，已归档到知识库（${r.archived_type === 'functional_point' ? '功能点' : '需求'}）`,
        })
      } else {
        await knowledgeQA.reject(reviewing.record.id, reviewNotes.trim())
        setToast({ type: 'success', msg: '已拒绝该纠正' })
      }
      setReviewing(null)
      setReviewNotes('')
      // 重新加载列表
      await loadCorrections()
    } catch (e: any) {
      setToast({ type: 'error', msg: `操作失败：${e?.message || '未知错误'}` })
    } finally {
      setSubmitting(false)
    }
  }

  // ============================================================================
  // 渲染
  // ============================================================================

  const totalPages = Math.max(1, Math.ceil(total / pageSize))
  const statusTabs = [
    { key: 'pending_review', label: '待审核' },
    { key: 'approved', label: '已通过' },
    { key: 'rejected', label: '已拒绝' },
    { key: 'all', label: '全部' },
  ]

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="page-title flex items-center gap-2">
            <ClipboardCheck className="w-5 h-5" style={{ color: 'var(--brand-500)' }} /> 纠正审核
          </h1>
          <p className="text-sm mt-1" style={{ color: 'var(--foreground-tertiary)' }}>
            审核知识问答的人工纠正内容，通过后自动归档到向量知识库
          </p>
        </div>
      </div>

      {/* 流程提示 */}
      <div className="card p-4 text-xs flex items-start gap-2" style={{ color: 'var(--foreground-tertiary)' }}>
        <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5" style={{ color: 'var(--state-warning)' }} />
        <div>
          <span className="font-medium">审核流程：</span>
          用户在「知识问答」提交纠正 → 进入<span style={{ color: 'var(--state-warning)' }}>待审核</span>状态 →
          审核通过后纠正内容归档到向量库（<span style={{ color: 'var(--brand-500)' }}>整篇需求</span>或
          <span style={{ color: 'var(--brand-500)' }}>单个功能点</span>替换原 FP 向量）→
          审核拒绝则不进入向量库。
        </div>
      </div>

      {/* Filters */}
      <div className="card p-4 space-y-3">
        {/* 状态 tabs */}
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-xs flex items-center gap-1" style={{ color: 'var(--foreground-tertiary)' }}>
            <Filter className="w-3 h-3" /> 状态：
          </span>
          {statusTabs.map(tab => {
            const active = statusFilter === tab.key
            return (
              <button
                key={tab.key}
                onClick={() => setStatusFilter(tab.key)}
                className="px-3 py-1 text-xs rounded-full transition-colors"
                style={active
                  ? { background: 'var(--brand-500)', color: '#fff' }
                  : { background: 'var(--background-muted)', color: 'var(--foreground-secondary)' }
                }
              >
                {tab.label}
              </button>
            )
          })}
        </div>

        {/* 模块筛选 */}
        <div className="flex items-center gap-2">
          <span className="text-xs flex items-center gap-1" style={{ color: 'var(--foreground-tertiary)' }}>
            <Layers className="w-3 h-3" /> 模块：
          </span>
          <select
            value={moduleFilter}
            onChange={e => setModuleFilter(e.target.value)}
            className="input-field text-sm"
            style={{ maxWidth: 240 }}
          >
            <option value="">全部模块</option>
            {modules.map(m => (
              <option key={m.id} value={m.name}>{m.name}</option>
            ))}
          </select>
          {moduleFilter && (
            <button
              onClick={() => setModuleFilter('')}
              className="text-xs px-2 py-1 rounded"
              style={{ background: 'var(--background-muted)', color: 'var(--foreground-tertiary)' }}
            >
              清除
            </button>
          )}
          <span className="text-xs ml-auto" style={{ color: 'var(--foreground-quaternary)' }}>
            共 {total} 条
          </span>
        </div>
      </div>

      {/* List */}
      <div className="card p-6 min-h-[400px]">
        {loading ? (
          <LoadingSpinner text="加载纠正列表..." />
        ) : error ? (
          <div className="text-center py-12 text-sm" style={{ color: 'var(--state-error)' }}>
            <AlertCircle className="w-8 h-8 mx-auto mb-2 opacity-50" />
            {error}
            <div className="mt-3">
              <button onClick={loadCorrections} className="btn-secondary text-xs">重试</button>
            </div>
          </div>
        ) : records.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-12" style={{ color: 'var(--foreground-quaternary)' }}>
            <ClipboardCheck className="w-12 h-12 mb-3 opacity-30" />
            <p className="text-sm font-medium">暂无纠正记录</p>
            <p className="text-xs mt-1">
              {statusFilter === 'pending_review' ? '暂无待审核的纠正内容' : '当前筛选条件下没有记录'}
            </p>
          </div>
        ) : (
          <div className="space-y-4">
            {records.map(record => {
              const expanded = expandedIds.has(record.id)
              const status = record.correction_status || 'none'
              const sc = STATUS_COLOR[status] || STATUS_COLOR.none
              const isPending = status === 'pending_review'
              return (
                <div
                  key={record.id}
                  className="border rounded-xl p-4 transition-all"
                  style={{ borderColor: 'var(--border)', background: 'var(--background-muted)' }}
                >
                  {/* 头部：状态 + 时间 + 提交人 */}
                  <div className="flex items-center gap-2 flex-wrap mb-3">
                    <span
                      className="text-xs px-2 py-0.5 rounded-full font-medium"
                      style={{ background: sc.bg, color: sc.fg }}
                    >
                      {STATUS_LABEL[status] || status}
                    </span>
                    {record.correction_module && (
                      <span className="text-xs px-2 py-0.5 rounded-full flex items-center gap-1"
                        style={{ background: 'var(--background)', color: 'var(--foreground-tertiary)' }}>
                        <Layers className="w-3 h-3" /> {record.correction_module}
                      </span>
                    )}
                    <span className="text-xs px-2 py-0.5 rounded-full flex items-center gap-1"
                      style={{ background: 'var(--background)', color: 'var(--foreground-tertiary)' }}>
                      <Tag className="w-3 h-3" />
                      {record.correction_target_type === 'functional_point' ? '单个功能点' : '整篇需求'}
                      {record.correction_target_type === 'functional_point' && record.correction_target_id
                        ? ` · ${record.correction_target_id}`
                        : ''}
                    </span>
                    <span className="text-xs flex items-center gap-1 ml-auto" style={{ color: 'var(--foreground-quaternary)' }}>
                      <Clock className="w-3 h-3" /> {formatDateTime(record.updated_at)}
                    </span>
                  </div>

                  {/* 问题 */}
                  <div className="mb-2">
                    <p className="text-xs font-medium mb-1 flex items-center gap-1" style={{ color: 'var(--foreground-tertiary)' }}>
                      <MessageSquare className="w-3 h-3" /> 原问题
                    </p>
                    <p className="text-sm" style={{ color: 'var(--foreground)' }}>{record.question}</p>
                  </div>

                  {/* 原回答（折叠） */}
                  <div className="mb-2">
                    <button
                      onClick={() => toggleExpand(record.id)}
                      className="text-xs flex items-center gap-1 hover:underline"
                      style={{ color: 'var(--foreground-tertiary)' }}
                    >
                      {expanded ? <EyeOff className="w-3 h-3" /> : <Eye className="w-3 h-3" />}
                      {expanded ? '隐藏原回答' : '查看原回答'}
                    </button>
                    {expanded && (
                      <div className="mt-2 p-3 rounded-md text-xs whitespace-pre-wrap"
                        style={{ background: 'var(--background)', color: 'var(--foreground-secondary)', border: '1px dashed var(--border)' }}>
                        {record.answer}
                      </div>
                    )}
                  </div>

                  {/* 纠正内容 */}
                  <div className="mb-2">
                    <p className="text-xs font-medium mb-1 flex items-center gap-1" style={{ color: 'var(--brand-500)' }}>
                      <CheckCircle2 className="w-3 h-3" /> 纠正内容
                    </p>
                    <div className="p-3 rounded-md text-sm whitespace-pre-wrap"
                      style={{ background: 'var(--brand-50, rgba(0, 122, 255, 0.06))', color: 'var(--foreground)', border: '1px solid var(--brand-100, rgba(0, 122, 255, 0.15))' }}>
                      {record.corrected_answer || '(空)'}
                    </div>
                  </div>

                  {/* 纠正说明 */}
                  {record.correction_notes && (
                    <div className="mb-2">
                      <p className="text-xs font-medium mb-1" style={{ color: 'var(--foreground-tertiary)' }}>
                        纠正说明
                      </p>
                      <p className="text-xs p-2 rounded-md" style={{ background: 'var(--background)', color: 'var(--foreground-secondary)' }}>
                        {record.correction_notes}
                      </p>
                    </div>
                  )}

                  {/* 审核信息（已审核的显示） */}
                  {!isPending && record.reviewed_by && (
                    <div className="text-xs flex items-center gap-3 mt-2 pt-2 border-t" style={{ borderColor: 'var(--border)', color: 'var(--foreground-quaternary)' }}>
                      <span className="flex items-center gap-1">
                        <User className="w-3 h-3" /> 审核人: {record.reviewed_by}
                      </span>
                      <span>· {formatDateTime(record.reviewed_at)}</span>
                      {record.review_notes && <span className="flex items-center gap-1">· 备注: {record.review_notes}</span>}
                    </div>
                  )}

                  {/* 操作按钮 */}
                  {isPending && (
                    <div className="flex items-center gap-2 mt-3 pt-3 border-t" style={{ borderColor: 'var(--border)' }}>
                      <button
                        onClick={() => startReview(record, 'approve')}
                        className="btn-primary text-xs flex items-center gap-1"
                        style={{ background: 'var(--state-success)' }}
                      >
                        <CheckCircle2 className="w-3.5 h-3.5" /> 审核通过
                      </button>
                      <button
                        onClick={() => startReview(record, 'reject')}
                        className="btn-secondary text-xs flex items-center gap-1"
                        style={{ color: 'var(--state-error)' }}
                      >
                        <XCircle className="w-3.5 h-3.5" /> 拒绝
                      </button>
                    </div>
                  )}
                </div>
              )
            })}

            {/* 分页 */}
            {total > pageSize && (
              <div className="flex items-center justify-center gap-3 pt-4 border-t" style={{ borderColor: 'var(--border)' }}>
                <button
                  onClick={() => setPage(p => Math.max(1, p - 1))}
                  disabled={page <= 1}
                  className="btn-secondary text-xs flex items-center gap-1"
                  style={{ opacity: page <= 1 ? 0.5 : 1 }}
                >
                  <ChevronLeft className="w-3.5 h-3.5" /> 上一页
                </button>
                <span className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>
                  第 {page} / {totalPages} 页
                </span>
                <button
                  onClick={() => setPage(p => Math.min(totalPages, p + 1))}
                  disabled={page >= totalPages}
                  className="btn-secondary text-xs flex items-center gap-1"
                  style={{ opacity: page >= totalPages ? 0.5 : 1 }}
                >
                  下一页 <ChevronRight className="w-3.5 h-3.5" />
                </button>
              </div>
            )}
          </div>
        )}
      </div>

      {/* 审核弹窗 */}
      {reviewing && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4"
          style={{ background: 'rgba(0, 0, 0, 0.32)', backdropFilter: 'blur(4px)' }}
          onClick={() => !submitting && setReviewing(null)}>
          <div className="max-w-md w-full p-6 animate-slide-up"
            style={{ background: 'var(--background)', borderRadius: 'calc(var(--radius) * 1.1)', boxShadow: 'var(--shadow-apple-xl)' }}
            onClick={e => e.stopPropagation()}>
            <div className="flex items-center gap-2 mb-4">
              {reviewing.action === 'approve'
                ? <CheckCircle2 className="w-5 h-5" style={{ color: 'var(--state-success)' }} />
                : <XCircle className="w-5 h-5" style={{ color: 'var(--state-error)' }} />}
              <h2 className="text-base font-semibold" style={{ color: 'var(--foreground)' }}>
                {reviewing.action === 'approve' ? '审核通过纠正' : '拒绝纠正'}
              </h2>
            </div>

            {/* 简要信息 */}
            <div className="mb-3 p-3 rounded-md text-xs space-y-1" style={{ background: 'var(--background-muted)', color: 'var(--foreground-secondary)' }}>
              <div><span style={{ color: 'var(--foreground-tertiary)' }}>原问题：</span>{reviewing.record.question}</div>
              <div>
                <span style={{ color: 'var(--foreground-tertiary)' }}>颗粒度：</span>
                {reviewing.record.correction_target_type === 'functional_point' ? '单个功能点' : '整篇需求'}
                {reviewing.record.correction_target_type === 'functional_point' && reviewing.record.correction_target_id
                  ? ` · ${reviewing.record.correction_target_id}`
                  : ''}
              </div>
              <div><span style={{ color: 'var(--foreground-tertiary)' }}>模块：</span>{reviewing.record.correction_module || '(未指定)'}</div>
            </div>

            <p className="text-xs mb-2" style={{ color: 'var(--foreground-tertiary)' }}>
              审核备注{reviewing.action === 'approve' ? '（可选）' : '（建议填写拒绝原因）'}：
            </p>
            <textarea
              value={reviewNotes}
              onChange={e => setReviewNotes(e.target.value)}
              className="w-full input-field text-sm min-h-[80px]"
              placeholder={reviewing.action === 'approve'
                ? '可填写补充说明，例如"已核对原需求，纠正内容正确"'
                : '请填写拒绝原因，例如"纠正内容与原需求不符"'}
            />

            {reviewing.action === 'approve' && (
              <div className="mt-3 p-2 rounded-md text-xs flex items-start gap-2"
                style={{ background: 'var(--state-warning-surface)', color: 'var(--state-warning)' }}>
                <AlertCircle className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
                <span>
                  通过后将执行：
                  {reviewing.record.correction_target_type === 'functional_point'
                    ? '删除原功能点向量并重新嵌入纠正内容（部分纠正）'
                    : '将纠正内容归档为新需求并嵌入向量库'}
                </span>
              </div>
            )}

            <div className="flex justify-end gap-3 mt-4">
              <button
                onClick={() => setReviewing(null)}
                disabled={submitting}
                className="btn-secondary text-sm"
              >
                取消
              </button>
              <button
                onClick={submitReview}
                disabled={submitting || (reviewing.action === 'reject' && !reviewNotes.trim())}
                className="btn-primary text-sm flex items-center gap-1"
                style={reviewing.action === 'approve'
                  ? { background: 'var(--state-success)' }
                  : { background: 'var(--state-error)' }
                }
              >
                {submitting ? <Loader2 className="w-4 h-4 animate-spin" /> :
                  reviewing.action === 'approve' ? <CheckCircle2 className="w-4 h-4" /> : <XCircle className="w-4 h-4" />
                }
                {reviewing.action === 'approve' ? '确认通过' : '确认拒绝'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Toast */}
      {toast && (
        <div className="fixed bottom-6 right-6 z-50 px-4 py-3 rounded-lg shadow-lg animate-slide-up flex items-center gap-2"
          style={{
            background: toast.type === 'success' ? 'var(--state-success-surface)'
              : toast.type === 'error' ? 'var(--state-error-surface)'
              : 'var(--state-warning-surface)',
            color: toast.type === 'success' ? 'var(--state-success)'
              : toast.type === 'error' ? 'var(--state-error)'
              : 'var(--state-warning)',
            border: `1px solid ${toast.type === 'success' ? 'var(--state-success)'
              : toast.type === 'error' ? 'var(--state-error)'
              : 'var(--state-warning)'}`,
          }}
        >
          {toast.type === 'success' ? <CheckCircle2 className="w-4 h-4" />
            : toast.type === 'error' ? <AlertCircle className="w-4 h-4" />
            : <AlertCircle className="w-4 h-4" />}
          <span className="text-sm">{toast.msg}</span>
        </div>
      )}
    </div>
  )
}
