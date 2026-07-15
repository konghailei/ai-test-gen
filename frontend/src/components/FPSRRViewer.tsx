import { useState, useEffect } from 'react'
import {
  ChevronDown, ChevronRight, Check, AlertTriangle, Sparkles, RefreshCw,
  Loader2, Edit3, Save, X, Clock, ExternalLink, Layers, GitBranch, Share2,
} from 'lucide-react'
import { requirements as reqApi, dependencyGraph, products as prodApi } from '../api/client'
import { useNavigate } from 'react-router-dom'
import type { FPItem } from '../types'

interface Props {
  reqId: string
  productId?: string
  status?: string
}

interface FPDepEdge { source_fp_id: string; target_fp_id: string; relation_type: string }

const FP_TYPE_LABELS: Record<string, string> = {
  workflow: '业务流程', constraint: '约束校验', calculation: '计算规则',
  api_behavior: 'API 行为', data_record: '数据记录', config: '配置项',
}

const RELATION_LABELS: Record<string, string> = {
  depends_on: '前置依赖', data_flows: '数据流转', invariants: '共享约束',
  lifecycle_scenarios: '同流程', conflicts_with: '互斥',
}

export default function FPSRRViewer({ reqId, productId, status }: Props) {
  const [fps, setFps] = useState<FPItem[]>([])
  const [deps, setDeps] = useState<{ dependencies: FPDepEdge[] }>({ dependencies: [] })
  const [modules, setModules] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editName, setEditName] = useState('')
  const [editModule, setEditModule] = useState('')
  const [regeneratingId, setRegeneratingId] = useState<string | null>(null)
  const [showRegenModal, setShowRegenModal] = useState<string | null>(null)
  const [regenHint, setRegenHint] = useState('')
  const [impactModal, setImpactModal] = useState<any>(null)
  const [impactLoading, setImpactLoading] = useState(false)
  const isArchived = status === 'archived'
  const navigate = useNavigate()

  useEffect(() => { load(); loadModules(); loadDeps() }, [reqId, productId])

  const load = async () => {
    setLoading(true)
    try { const r = await reqApi.fpList(reqId); setFps(r.items || []) } catch { }
    setLoading(false)
  }
  const loadModules = async () => {
    if (!productId) return
    try { setModules(await prodApi.listModules(productId) || []) } catch { }
  }
  const loadDeps = async () => {
    try { const r = await reqApi.fpDependencies(reqId); setDeps(r) } catch { }
  }

  // 按模块分组
  const grouped = new Map<string, FPItem[]>()
  for (const fp of fps) {
    const mod = fp.module_id || '_unassigned'
    if (!grouped.has(mod)) grouped.set(mod, [])
    grouped.get(mod)!.push(fp)
  }
  const groups = [...grouped.entries()]

  const approved = fps.filter(f => f.status === 'approved').length
  const allApproved = fps.length > 0 && approved === fps.length

  // 全量重新拆分
  const handleResplit = async () => {
    if (!confirm('重新拆分会先删除当前所有功能点，基于原始文档重新生成。确定继续？')) return
    setLoading(true)
    try { await reqApi.fpSplit(reqId, []); await load(); await loadDeps() } catch { }
    setLoading(false)
  }

  const toggle = (id: string) => {
    const next = new Set(expanded)
    if (next.has(id)) next.delete(id); else next.add(id)
    setExpanded(next)
  }

  const approve = async (fpId: string) => {
    await reqApi.fpApprove(reqId, fpId)
    setFps(prev => prev.map(f => f.id === fpId ? { ...f, status: 'approved' } : f))
  }

  const startRegen = (fpId: string) => {
    setShowRegenModal(fpId)
    setRegenHint('')
  }

  const doRegenerate = async (fpId: string) => {
    setRegeneratingId(fpId)
    try {
      await reqApi.fpRegenerate(reqId, fpId, regenHint)
      await load()
    } catch { }
    setRegeneratingId(null)
    setShowRegenModal(null)
  }

  const updateFP = async (fpId: string) => {
    await reqApi.fpUpdate(reqId, fpId, { name: editName || undefined, module_id: editModule || undefined })
    setFps(prev => prev.map(f => f.id === fpId ? { ...f, name: editName || f.name, module_id: editModule || f.module_id } : f))
    setEditingId(null)
  }

  // 影响分析
  const runImpact = async (fpId: string) => {
    if (!productId) return
    setImpactLoading(true)
    try {
      const result = await dependencyGraph.fpImpact(productId, fpId)
      setImpactModal(result)
    } catch (e: any) {
      alert(e?.message || '影响分析失败，请重试')
    }
    setImpactLoading(false)
  }

  // 依赖图谱：找到当前 FP 相关的边
  const relatedDeps = (fpId: string) =>
    deps.dependencies?.filter(d => d.source_fp_id === fpId || d.target_fp_id === fpId) || []

  // 获取 FP 名称映射
  const fpNameMap: Record<string, string> = {}
  fps.forEach(f => { fpNameMap[f.id] = f.name })

  if (loading) return <div className="flex items-center justify-center py-12"><Loader2 className="w-6 h-6 text-primary-500 animate-spin" /></div>

  return (
    <div className="space-y-4">
      {/* 审核进度 + 重新拆分 */}
      {!isArchived && (
        <>
        {fps.length > 0 && (
          <div className={[
            'flex items-center gap-3 p-3 rounded-xl text-sm',
            allApproved ? 'bg-emerald-50 text-emerald-700' : 'bg-amber-50 text-amber-700',
          ].join(' ')}>
            {allApproved ? <Check className="w-5 h-5" /> : <Clock className="w-5 h-5" />}
            <span>
              {allApproved
                ? '全部功能点已审核通过，可进行下一步：生成测试用例'
                : `审核进度：${approved}/${fps.length} 个已通过，全部审核通过后可生成测试用例`}
            </span>
          </div>
        )}
        <div className="flex items-center justify-between">
          <span className="text-xs text-slate-400">
            上传时自动拆分。拆分不准确可重新拆分。
          </span>
          <button onClick={handleResplit} disabled={loading}
            className="btn-secondary text-xs flex items-center gap-1.5">
            <RefreshCw className="w-3.5 h-3.5" /> 重新拆分
          </button>
        </div>
        </>
      )}

      {fps.length === 0 ? (
        <p className="text-sm text-slate-400 py-8 text-center">
          {isArchived ? '此需求已归档，暂无功能点数据。' : '暂无功能点数据。'}
        </p>
      ) : (
        groups.map(([modId, fpList]) => {
          const modName = modId === '_unassigned' ? '未分配模块' : (modules.find(m => m.id === modId)?.name || modId)
          return (
            <div key={modId} className="border border-slate-200 rounded-xl overflow-hidden">
              {/* 模块标题 */}
              <div className="px-4 py-2.5 bg-slate-50 border-b border-slate-100 flex items-center gap-2">
                <Layers className="w-4 h-4 text-slate-400" />
                <span className="text-sm font-medium text-slate-700">{modName}</span>
                <span className="text-xs text-slate-400">{fpList.length} 个功能点</span>
              </div>

              {/* FP 列表 */}
              {fpList.map(fp => {
                const isExpanded = expanded.has(fp.id)
                const depsForFp = relatedDeps(fp.id)
                return (
                  <div key={fp.id} className="border-b border-slate-100 last:border-b-0">
                    {/* FP 标题行 */}
                    <div className={[
                      'flex items-center justify-between px-4 py-3 hover:bg-slate-50 cursor-pointer transition-colors',
                      fp.status === 'approved' ? '' : 'bg-amber-50/30',
                    ].join(' ')}
                      onClick={() => toggle(fp.id)}
                    >
                      <div className="flex items-center gap-3">
                        <button className="text-slate-400">
                          {isExpanded ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
                        </button>
                        <div>
                          <div className="flex items-center gap-2">
                            <span className="text-sm font-medium text-slate-700">{fp.name}</span>
                            <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-500">
                              {FP_TYPE_LABELS[fp.fp_type] || fp.fp_type}
                            </span>
                            {fp.status === 'approved' ? (
                              <span className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-100 text-emerald-600">
                                已审核
                              </span>
                            ) : (
                              <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-100 text-amber-600">
                                待审核
                              </span>
                            )}
                          </div>
                          {fp.test_hints?.length > 0 && (
                            <p className="text-[11px] text-slate-400 mt-0.5 line-clamp-1">
                              {fp.test_hints.slice(0, 2).join('；')}
                            </p>
                          )}
                        </div>
                      </div>
                      <div className="flex items-center gap-1" onClick={e => e.stopPropagation()}>
                        {fp.status !== 'approved' && !isArchived && (
                          <button
                            onClick={() => approve(fp.id)}
                            className="p-1.5 text-emerald-600 hover:bg-emerald-50 rounded-lg text-xs flex items-center gap-1"
                            title="审核通过"
                          >
                            <Check className="w-3.5 h-3.5" /> 通过
                          </button>
                        )}
                        {!isArchived && (
                          <>
                            <button
                              onClick={() => startRegen(fp.id)}
                              className="p-1.5 text-amber-600 hover:bg-amber-50 rounded-lg"
                              title="AI 重新生成"
                            >
                              <RefreshCw className="w-3.5 h-3.5" />
                            </button>
                            {editingId === fp.id ? (
                              <>
                                <button onClick={() => updateFP(fp.id)} className="p-1 text-emerald-600"><Save className="w-3.5 h-3.5" /></button>
                                <button onClick={() => setEditingId(null)} className="p-1 text-slate-400"><X className="w-3.5 h-3.5" /></button>
                              </>
                            ) : (
                              <button
                                onClick={() => { setEditingId(fp.id); setEditName(fp.name); setEditModule(fp.module_id || '') }}
                                className="p-1.5 text-slate-400 hover:text-primary-500 rounded-lg"
                              >
                                <Edit3 className="w-3.5 h-3.5" />
                              </button>
                            )}
                            {productId && (
                              <button
                                onClick={() => runImpact(fp.id)}
                                className="p-1.5 text-indigo-400 hover:text-indigo-600 hover:bg-indigo-50 rounded-lg"
                                title="影响分析"
                              >
                                <Share2 className="w-3.5 h-3.5" />
                              </button>
                            )}
                          </>
                        )}
                      </div>
                    </div>

                    {/* 编辑态 */}
                    {editingId === fp.id && (
                      <div className="px-4 pb-3 flex items-center gap-2" onClick={e => e.stopPropagation()}>
                        <input value={editName} onChange={e => setEditName(e.target.value)}
                          className="input-field text-sm flex-1" placeholder="功能点名称" autoFocus />
                        <select value={editModule} onChange={e => setEditModule(e.target.value)}
                          className="select-field text-sm max-w-[160px]">
                          <option value="">选择模块</option>
                          {modules.map((m: any) => (
                            <option key={m.id} value={m.id}>{m.name}</option>
                          ))}
                        </select>
                      </div>
                    )}

                    {/* SRR 展开内容 */}
                    {isExpanded && (
                      <div className="px-4 pb-4 bg-slate-50/50 space-y-3">
                        {/* 子 SRR 内容 */}
                        {fp.srr_json && Object.keys(fp.srr_json).length > 0 && (
                          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                            {fp.srr_json.description && (
                              <div className="md:col-span-2">
                                <span className="text-[10px] text-slate-400 uppercase tracking-wider">描述</span>
                                <p className="text-sm text-slate-600 mt-0.5">{fp.srr_json.description}</p>
                              </div>
                            )}
                            {fp.srr_json.flows && (() => {
                              const flows = fp.srr_json.flows
                              return (
                                <div className="md:col-span-2 space-y-2">
                                  {flows.happy_path && (
                                    <div>
                                      <span className="text-[10px] text-slate-400 uppercase tracking-wider">正常流程</span>
                                      <p className="text-sm text-slate-600 mt-0.5">{flows.happy_path}</p>
                                    </div>
                                  )}
                                  {flows.alternate_flows && (
                                    <div>
                                      <span className="text-[10px] text-slate-400 uppercase tracking-wider">异常分支</span>
                                      <p className="text-sm text-slate-600 mt-0.5">{flows.alternate_flows}</p>
                                    </div>
                                  )}
                                </div>
                              )
                            })()}
                            {fp.srr_json.business_rules?.length > 0 && (
                              <div>
                                <span className="text-[10px] text-slate-400 uppercase tracking-wider">业务规则</span>
                                <ul className="mt-1 space-y-0.5">
                                  {fp.srr_json.business_rules.map((r: string, i: number) => (
                                    <li key={i} className="text-sm text-slate-600 pl-3 border-l-2 border-slate-200">{r}</li>
                                  ))}
                                </ul>
                              </div>
                            )}
                            {fp.srr_json.acceptance_criteria?.length > 0 && (
                              <div>
                                <span className="text-[10px] text-slate-400 uppercase tracking-wider">验收标准</span>
                                <ul className="mt-1 space-y-0.5">
                                  {fp.srr_json.acceptance_criteria.map((ac: string, i: number) => (
                                    <li key={i} className="text-sm text-slate-600 pl-3 border-l-2 border-emerald-200">{ac}</li>
                                  ))}
                                </ul>
                              </div>
                            )}
                            {fp.srr_json.preconditions?.length > 0 && (
                              <div>
                                <span className="text-[10px] text-slate-400 uppercase tracking-wider">前置条件</span>
                                <ul className="mt-1 space-y-0.5">
                                  {fp.srr_json.preconditions.map((p: string, i: number) => (
                                    <li key={i} className="text-sm text-slate-600">• {p}</li>
                                  ))}
                                </ul>
                              </div>
                            )}
                          </div>
                        )}

                        {/* 测试提示 */}
                        {fp.test_hints?.length > 0 && (
                          <div>
                            <span className="text-[10px] text-slate-400 uppercase tracking-wider">测试提示</span>
                            <div className="flex flex-wrap gap-1 mt-1">
                              {fp.test_hints.map((h: string, i: number) => (
                                <span key={i} className="text-xs px-2 py-0.5 bg-indigo-50 text-indigo-600 rounded">{h}</span>
                              ))}
                            </div>
                          </div>
                        )}

                        {/* 依赖关系 */}
                        {depsForFp.length > 0 && (
                          <div>
                            <div className="flex items-center gap-2 mb-1">
                              <span className="text-[10px] text-slate-400 uppercase tracking-wider">依赖关系</span>
                              <button
                                onClick={() => navigate(`/dependency-graph?productId=${productId || ''}&fp=${fp.id}`)}
                                className="text-[10px] text-primary-500 hover:underline flex items-center gap-0.5"
                              >
                                <ExternalLink className="w-3 h-3" /> 查看图谱
                              </button>
                            </div>
                            <div className="space-y-1">
                              {depsForFp.map((d, i) => {
                                const other = d.source_fp_id === fp.id ? d.target_fp_id : d.source_fp_id
                                const label = RELATION_LABELS[d.relation_type] || d.relation_type
                                const dir = d.source_fp_id === fp.id ? '→' : '←'
                                return (
                                  <div key={i} className="text-xs text-slate-500 flex items-center gap-1">
                                    <span className="px-1 py-0.5 bg-slate-100 rounded text-[10px]">{label}</span>
                                    {d.source_fp_id === fp.id && (
                                      <span>{fpNameMap[other] || other}</span>
                                    )}
                                    <span className="text-slate-300">{dir}</span>
                                    {d.target_fp_id === fp.id && (
                                      <span>{fpNameMap[other] || other}</span>
                                    )}
                                  </div>
                                )
                              })}
                            </div>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )
        })
      )}

      {/* AI 重生成弹窗 */}
      {showRegenModal && (
        <div className="fixed inset-0 z-50 bg-black/30 backdrop-blur-sm flex items-center justify-center p-4" onClick={() => setShowRegenModal(null)}>
          <div className="bg-white rounded-2xl shadow-xl max-w-md w-full p-6 animate-slide-up" onClick={e => e.stopPropagation()}>
            <h2 className="text-lg font-semibold text-slate-800 flex items-center gap-2">
              <Sparkles className="w-5 h-5 text-amber-500" /> AI 重新生成
            </h2>
            <p className="text-sm text-slate-500 mt-2">
              输入补充说明来引导 AI 重新生成该功能点的子 SRR（可选）。
            </p>
            <textarea
              value={regenHint}
              onChange={e => setRegenHint(e.target.value)}
              className="input-field resize-none mt-3 text-sm"
              rows={3}
              placeholder="例如：请重点补充密码复杂度校验的业务规则..."
            />
            <div className="flex items-center justify-end gap-3 mt-4">
              <button onClick={() => setShowRegenModal(null)} className="btn-secondary">取消</button>
              <button onClick={() => doRegenerate(showRegenModal)}
                disabled={!!regeneratingId}
                className="btn-primary flex items-center gap-2 disabled:opacity-50">
                {regeneratingId === showRegenModal ? (
                  <><Loader2 className="w-4 h-4 animate-spin" /> 生成中...</>
                ) : (
                  <><RefreshCw className="w-4 h-4" /> 重新生成</>
                )}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 影响分析弹窗 */}
      {impactModal && (
        <div className="fixed inset-0 z-50 bg-black/30 backdrop-blur-sm flex items-center justify-center p-4" onClick={() => setImpactModal(null)}>
          <div className="bg-white rounded-2xl shadow-xl max-w-lg w-full p-6 animate-slide-up max-h-[80vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
            <h2 className="text-lg font-semibold text-slate-800 flex items-center gap-2">
              <Share2 className="w-5 h-5 text-indigo-500" /> 影响分析
            </h2>
            <p className="text-sm text-slate-500 mt-2 mb-4 whitespace-pre-line">{impactModal.impact_summary}</p>

            {impactModal.affected_modules?.length > 0 && (
              <div className="space-y-3">
                <span className="text-xs text-slate-400 uppercase tracking-wider">受影响模块</span>
                {impactModal.affected_modules.map((mod: any) => (
                  <div key={mod.module_id} className="p-3 bg-slate-50 rounded-xl">
                    <p className="text-xs font-medium text-slate-600 mb-1">
                      {mod.module_id === '_unassigned' ? '未分配模块' : mod.module_id}
                    </p>
                    <div className="flex flex-wrap gap-1">
                      {mod.fp_names.map((n: string, i: number) => (
                        <span key={i} className="text-xs px-2 py-0.5 bg-white border border-slate-200 rounded text-slate-600">
                          {n}
                        </span>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            )}

            {impactModal.total === 0 && (
              <p className="text-sm text-slate-400 py-4 text-center">未检测到受影响的功能点。</p>
            )}

            <div className="flex items-center justify-between mt-4 pt-3 border-t border-slate-100">
              <p className="text-xs text-slate-400">
                影响范围可确认后作为测试用例生成的参考来源
              </p>
              <button onClick={() => setImpactModal(null)} className="btn-secondary">关闭</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
