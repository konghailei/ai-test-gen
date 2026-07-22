import { useState, useEffect, useMemo } from 'react'
import {
  Save, RotateCcw, Loader2, FileText, CheckCircle2, Search, TestTube,
  Shield, GitBranch, History, X, Eye, Code, Image, Database, Layers,
} from 'lucide-react'
import { promptApi } from '../api/client'
import type { PromptItem, PromptHistoryItem } from '../types'

const TABS = [
  { key: 'req', label: '需求分析', icon: Search, modules: ['req_agent', 'clarify_agent'], desc: '需求解析与澄清 Agent 提示词' },
  { key: 'gen', label: '用例生成', icon: TestTube, modules: ['gen_agent'], desc: '测试用例自动生成 Agent 提示词' },
  { key: 'review', label: '用例审核', icon: Shield, modules: ['review_agent'], desc: '用例质量审核 Agent 提示词' },
  { key: 'impact', label: '影响分析', icon: GitBranch, modules: ['impact_agent'], desc: '需求变更影响分析 Agent 提示词' },
  { key: 'fp', label: '功能点拆分', icon: Layers, modules: ['fp_agent'], desc: '功能点拆分与关联分析 Agent 提示词' },
  { key: 'kb', label: '知识库问答', icon: Database, modules: ['knowledge_qa'], desc: '知识库问答与追问 Agent 提示词' },
  { key: 'vision', label: '多模态识别', icon: Image, modules: ['vision'], desc: '图片/流程图/架构图等多模态内容识别提示词' },
]

const MODULE_NAMES: Record<string, string> = {
  req_agent: 'ReqAgent（需求解析）',
  clarify_agent: 'ClarifyAgent（需求澄清）',
  gen_agent: 'GenAgent（用例生成）',
  review_agent: 'ReviewAgent（用例审核）',
  impact_agent: 'ImpactAgent（影响分析）',
  fp_agent: 'FP-Agent（功能点拆分）',
  knowledge_qa: 'KnowledgeQA（知识库问答）',
  vision: 'Vision（多模态识别）',
}

// ============================================================================
// v2.3 简单 LCS diff（行级别）
// ============================================================================
interface DiffLine {
  type: 'added' | 'removed' | 'unchanged'
  text: string
  oldLine?: number
  newLine?: number
}

function computeDiff(oldText: string, newText: string): DiffLine[] {
  const oldLines = (oldText || '').split('\n')
  const newLines = (newText || '').split('\n')
  const m = oldLines.length, n = newLines.length
  // LCS DP 表
  const dp: number[][] = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0))
  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      if (oldLines[i - 1] === newLines[j - 1]) {
        dp[i][j] = dp[i - 1][j - 1] + 1
      } else {
        dp[i][j] = Math.max(dp[i - 1][j], dp[i][j - 1])
      }
    }
  }
  // 回溯
  const result: DiffLine[] = []
  let i = m, j = n
  while (i > 0 && j > 0) {
    if (oldLines[i - 1] === newLines[j - 1]) {
      result.unshift({ type: 'unchanged', text: oldLines[i - 1], oldLine: i, newLine: j })
      i--; j--
    } else if (dp[i - 1][j] >= dp[i][j - 1]) {
      result.unshift({ type: 'removed', text: oldLines[i - 1], oldLine: i })
      i--
    } else {
      result.unshift({ type: 'added', text: newLines[j - 1], newLine: j })
      j--
    }
  }
  while (i > 0) { result.unshift({ type: 'removed', text: oldLines[i - 1], oldLine: i }); i-- }
  while (j > 0) { result.unshift({ type: 'added', text: newLines[j - 1], newLine: j }); j-- }
  return result
}

// ============================================================================
// v2.3 版本历史面板
// ============================================================================
function PromptHistoryDrawer({
  prompt, items, loading, onClose, onRestore,
}: {
  prompt: PromptItem
  items: PromptHistoryItem[]
  loading: boolean
  onClose: () => void
  onRestore: (historyId: number, summary: string) => Promise<void>
}) {
  const [selectedId, setSelectedId] = useState<number | null>(items[0]?.id ?? null)
  const [viewMode, setViewMode] = useState<'diff' | 'side'>('diff')
  const [restoring, setRestoring] = useState<number | null>(null)

  const selected = useMemo(
    () => items.find(h => h.id === selectedId) || items[0] || null,
    [items, selectedId],
  )

  const diffLines = useMemo(() => {
    if (!selected) return []
    return computeDiff(selected.old_value, selected.new_value)
  }, [selected])

  const handleRestore = async (h: PromptHistoryItem) => {
    if (!confirm(`确认恢复到该历史版本？\n\n时间：${new Date(h.changed_at).toLocaleString('zh-CN')}\n修改人：${h.changed_by}\n${h.change_summary}`)) return
    setRestoring(h.id)
    try {
      await onRestore(h.id, h.change_summary)
    } finally {
      setRestoring(null)
    }
  }

  return (
    <div className="fixed inset-0 z-[60] bg-black/30 backdrop-blur-sm flex items-center justify-center p-4"
      onClick={onClose}>
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-6xl h-[85vh] flex flex-col"
        onClick={e => e.stopPropagation()}>
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-slate-100">
          <div className="flex items-center gap-2">
            <History className="w-5 h-5 text-primary-500" />
            <h3 className="text-base font-semibold text-slate-800">版本历史</h3>
            <span className="text-xs px-2 py-0.5 rounded bg-primary-50 text-primary-600">
              {MODULE_NAMES[prompt.module] || prompt.module} · {prompt.name}
            </span>
            <span className="text-xs text-slate-400">共 {items.length} 个版本</span>
          </div>
          <button onClick={onClose} className="p-1 rounded-lg hover:bg-slate-100">
            <X className="w-4 h-4 text-slate-400" />
          </button>
        </div>

        <div className="flex-1 flex min-h-0">
          {/* Left: history timeline */}
          <div className="w-72 border-r border-slate-100 overflow-y-auto p-3 space-y-1.5">
            {loading ? (
              <div className="flex items-center justify-center py-8">
                <Loader2 className="w-5 h-5 text-primary-500 animate-spin" />
              </div>
            ) : items.length === 0 ? (
              <p className="text-sm text-slate-400 text-center py-8">暂无历史记录</p>
            ) : (
              items.map(h => (
                <button key={h.id} onClick={() => setSelectedId(h.id)}
                  className={`w-full text-left p-3 rounded-xl border transition-colors ${
                    selected?.id === h.id
                      ? 'border-primary-300 bg-primary-50'
                      : 'border-slate-100 hover:border-slate-200 hover:bg-slate-50'
                  }`}>
                  <div className="flex items-center gap-1.5 mb-1">
                    <div className={`w-2 h-2 rounded-full ${
                      h.change_summary.includes('恢复') ? 'bg-amber-400' :
                      h.change_summary.includes('默认') ? 'bg-slate-400' : 'bg-primary-400'
                    }`} />
                    <span className="text-xs font-medium text-slate-700 truncate">
                      {h.change_summary || '修改'}
                    </span>
                  </div>
                  <p className="text-[11px] text-slate-500">
                    {new Date(h.changed_at).toLocaleString('zh-CN')}
                  </p>
                  <p className="text-[11px] text-slate-400 mt-0.5">
                    by <span className="font-medium text-slate-600">{h.changed_by || '未知'}</span>
                  </p>
                </button>
              ))
            )}
          </div>

          {/* Right: diff / side-by-side view */}
          <div className="flex-1 flex flex-col min-h-0">
            {selected ? (
              <>
                {/* Toolbar */}
                <div className="px-5 py-2.5 border-b border-slate-100 flex items-center gap-2">
                  <div className="flex items-center gap-1 bg-slate-100 rounded-lg p-0.5">
                    <button onClick={() => setViewMode('diff')}
                      className={`px-3 py-1 text-xs font-medium rounded-md flex items-center gap-1 ${
                        viewMode === 'diff' ? 'bg-white text-slate-800 shadow-sm' : 'text-slate-500'
                      }`}>
                      <Code className="w-3 h-3" /> 差异对比
                    </button>
                    <button onClick={() => setViewMode('side')}
                      className={`px-3 py-1 text-xs font-medium rounded-md flex items-center gap-1 ${
                        viewMode === 'side' ? 'bg-white text-slate-800 shadow-sm' : 'text-slate-500'
                      }`}>
                      <Eye className="w-3 h-3" /> 左右并排
                    </button>
                  </div>
                  <div className="flex-1" />
                  <button onClick={() => handleRestore(selected)} disabled={restoring === selected.id}
                    className="btn-primary text-xs flex items-center gap-1.5 disabled:opacity-50">
                    {restoring === selected.id
                      ? <Loader2 className="w-3 h-3 animate-spin" />
                      : <RotateCcw className="w-3 h-3" />}
                    恢复此版本
                  </button>
                </div>

                {/* Diff content */}
                <div className="flex-1 overflow-auto p-4">
                  {viewMode === 'diff' ? (
                    <div className="font-mono text-xs bg-slate-50 rounded-lg border border-slate-200 overflow-hidden">
                      {diffLines.map((line, idx) => (
                        <div key={idx} className={`flex items-start px-3 py-0.5 ${
                          line.type === 'added' ? 'bg-emerald-50' :
                          line.type === 'removed' ? 'bg-red-50' : ''
                        }`}>
                          <span className="text-slate-400 w-8 flex-shrink-0 select-none">
                            {line.type === 'added' ? '+' : line.type === 'removed' ? '-' : ' '}
                          </span>
                          <span className="text-slate-500 w-10 flex-shrink-0 select-none text-right pr-2">
                            {line.oldLine || line.newLine || ''}
                          </span>
                          <span className={`whitespace-pre-wrap break-all ${
                            line.type === 'added' ? 'text-emerald-700' :
                            line.type === 'removed' ? 'text-red-700 line-through opacity-70' :
                            'text-slate-600'
                          }`}>
                            {line.text || ' '}
                          </span>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="grid grid-cols-2 gap-3 h-full">
                      <div className="flex flex-col">
                        <div className="px-3 py-1.5 bg-red-50 text-red-700 text-xs font-medium rounded-t-lg border border-b-0 border-slate-200">
                          修改前
                        </div>
                        <pre className="flex-1 text-xs font-mono text-slate-600 bg-slate-50 border border-red-200 rounded-b-lg p-3 overflow-auto whitespace-pre-wrap">
                          {selected.old_value || '（空）'}
                        </pre>
                      </div>
                      <div className="flex flex-col">
                        <div className="px-3 py-1.5 bg-emerald-50 text-emerald-700 text-xs font-medium rounded-t-lg border border-b-0 border-slate-200">
                          修改后
                        </div>
                        <pre className="flex-1 text-xs font-mono text-slate-600 bg-slate-50 border border-emerald-200 rounded-b-lg p-3 overflow-auto whitespace-pre-wrap">
                          {selected.new_value || '（空）'}
                        </pre>
                      </div>
                    </div>
                  )}
                </div>

                {/* Footer info */}
                <div className="px-5 py-2 border-t border-slate-100 text-[11px] text-slate-500 flex items-center gap-4">
                  <span>修改人：<span className="font-medium text-slate-700">{selected.changed_by || '未知'}</span></span>
                  <span>时间：<span className="font-medium text-slate-700">{new Date(selected.changed_at).toLocaleString('zh-CN')}</span></span>
                  <span>变更摘要：<span className="text-slate-700">{selected.change_summary}</span></span>
                </div>
              </>
            ) : (
              <div className="flex-1 flex items-center justify-center text-slate-400 text-sm">
                请选择左侧的版本记录
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

// ============================================================================
// Main Page
// ============================================================================
export default function PromptManager() {
  const [items, setItems] = useState<PromptItem[]>([])
  const [loading, setLoading] = useState(true)
  const [tab, setTab] = useState('req')
  const [expanded, setExpanded] = useState<string | null>(null)
  const [editing, setEditing] = useState<string | null>(null)
  const [editValue, setEditValue] = useState('')
  const [saving, setSaving] = useState(false)
  const [toast, setToast] = useState('')

  // v2.3: 版本历史面板
  const [historyPrompt, setHistoryPrompt] = useState<PromptItem | null>(null)
  const [historyItems, setHistoryItems] = useState<PromptHistoryItem[]>([])
  const [historyLoading, setHistoryLoading] = useState(false)

  const load = async () => {
    setLoading(true)
    try {
      const res = await promptApi.list()
      setItems(res.prompts || [])
    } catch { /* ignore */ }
    setLoading(false)
  }
  useEffect(() => { load() }, [])

  const showToast = (msg: string) => {
    setToast(msg)
    setTimeout(() => setToast(''), 2500)
  }

  const handleSave = async (module: string, name: string) => {
    setSaving(true)
    try {
      await promptApi.update(module, name, editValue)
      showToast('已保存')
      setEditing(null)
      load()
    } catch (e: any) { alert('保存失败: ' + e.message) }
    setSaving(false)
  }

  const handleReset = async (module: string, name: string) => {
    if (!confirm('确认恢复为默认提示词？此操作会写入版本历史。')) return
    try {
      await promptApi.reset(module, name)
      showToast('已恢复默认')
      if (editing === `${module}.${name}`) setEditing(null)
      load()
    } catch (e: any) { alert('重置失败: ' + e.message) }
  }

  // v2.3: 打开版本历史面板
  const openHistory = async (prompt: PromptItem) => {
    setHistoryPrompt(prompt)
    setHistoryLoading(true)
    setHistoryItems([])
    try {
      const res = await promptApi.history(prompt.module, prompt.name, 50)
      setHistoryItems(res.items || [])
    } catch (e: any) {
      alert('加载历史失败: ' + e.message)
      setHistoryPrompt(null)
    }
    setHistoryLoading(false)
  }

  // v2.3: 从历史版本恢复
  const handleRestore = async (historyId: number, summary: string) => {
    if (!historyPrompt) return
    try {
      await promptApi.restore(historyPrompt.module, historyPrompt.name, historyId)
      showToast(`已恢复到版本：${summary}`)
      // 关闭面板并重新加载列表
      setHistoryPrompt(null)
      load()
    } catch (e: any) {
      alert('恢复失败: ' + e.message)
    }
  }

  const currentTab = TABS.find(t => t.key === tab) || TABS[0]
  const filtered = items.filter(i => currentTab.modules.includes(i.module))

  // 按 module 分组
  const grouped: Record<string, PromptItem[]> = {}
  for (const item of filtered) {
    if (!grouped[item.module]) grouped[item.module] = []
    grouped[item.module].push(item)
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="page-title flex items-center gap-2">
          <FileText className="w-6 h-6 text-primary-500" />
          提示词管理
        </h1>
        <p className="text-sm text-slate-500 mt-1">
          按模块管理 Agent 系统提示词，修改后即时生效（热重载）。支持版本历史回溯与恢复。
        </p>
      </div>

      {toast && (
        <div className="fixed top-20 right-6 z-50 bg-success-50 text-success-700 border border-success-200 rounded-xl px-4 py-2.5 text-sm font-medium shadow-lg flex items-center gap-2 animate-slide-up">
          <CheckCircle2 className="w-4 h-4" />{toast}
        </div>
      )}

      {/* Tab 导航 */}
      <div className="flex gap-1 bg-slate-100 p-1 rounded-xl">
        {TABS.map(t => (
          <button key={t.key} onClick={() => setTab(t.key)}
            className={`flex items-center gap-2 px-4 py-2.5 rounded-lg text-sm font-medium transition-all ${
              tab === t.key
                ? 'bg-white text-slate-800 shadow-sm'
                : 'text-slate-500 hover:text-slate-700 hover:bg-white/50'
            }`}>
            <t.icon className="w-4 h-4" />
            {t.label}
          </button>
        ))}
      </div>
      <p className="text-xs text-slate-400 -mt-4">{currentTab.desc}</p>

      {loading ? (
        <div className="space-y-3">{[1,2,3].map(i => <div key={i} className="skeleton h-16" />)}</div>
      ) : filtered.length === 0 ? (
        <div className="text-center py-12 text-slate-400">
          <FileText className="w-10 h-10 mx-auto mb-2 opacity-30" />
          <p className="text-sm">暂无提示词模板</p>
        </div>
      ) : (
        <div className="space-y-6">
          {Object.entries(grouped).map(([module, prompts]) => (
            <div key={module} className="card p-0 overflow-hidden">
              <div className="px-5 py-3 border-b border-slate-100 bg-slate-50/50 flex items-center justify-between">
                <h2 className="text-sm font-semibold text-slate-700">
                  {MODULE_NAMES[module] || module}
                </h2>
                <span className="text-xs text-slate-400">{prompts.length} 条提示词</span>
              </div>

              <div className="divide-y divide-slate-50">
                {prompts.map(item => {
                  const id = `${item.module}.${item.name}`
                  const isExpanded = expanded === id
                  const isEditing = editing === id
                  const isModified = item.value !== item.default_value

                  return (
                    <div key={id}>
                      <button onClick={() => setExpanded(isExpanded ? null : id)}
                        className="w-full px-5 py-3 flex items-center justify-between hover:bg-slate-50/50 transition-colors text-left">
                        <div className="flex items-center gap-3 min-w-0">
                          <FileText className="w-4 h-4 text-slate-400 flex-shrink-0" />
                          <div className="min-w-0">
                            <p className="text-sm font-medium text-slate-700 truncate flex items-center gap-1.5">
                              {item.name}
                              {isModified && (
                                <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-100 text-amber-700 font-medium">
                                  已修改
                                </span>
                              )}
                            </p>
                            <p className="text-xs text-slate-400 truncate">{item.description}</p>
                          </div>
                        </div>
                        <div className="flex items-center gap-2 flex-shrink-0 ml-3">
                          {/* v2.3: 历史按钮 */}
                          <span onClick={(e) => { e.stopPropagation(); openHistory(item) }}
                            role="button"
                            className="text-xs text-slate-400 hover:text-primary-600 flex items-center gap-1 px-2 py-1 rounded hover:bg-primary-50 transition-colors"
                            title="查看版本历史">
                            <History className="w-3 h-3" />
                            历史
                          </span>
                          <span className="text-xs text-slate-300">
                            {item.value.length} 字
                          </span>
                        </div>
                      </button>

                      {isExpanded && (
                        <div className="px-5 pb-4">
                          {isEditing ? (
                            <>
                              <textarea value={editValue} onChange={e => setEditValue(e.target.value)}
                                className="w-full h-80 text-sm font-mono bg-slate-50 border border-slate-200 rounded-xl p-4 focus:ring-2 focus:ring-primary-200 focus:border-primary-300 outline-none resize-y" />
                              <div className="flex items-center gap-2 mt-3">
                                <button onClick={() => handleSave(item.module, item.name)} disabled={saving}
                                  className="btn-primary text-xs flex items-center gap-1.5">
                                  {saving ? <Loader2 className="w-3 h-3 animate-spin" /> : <Save className="w-3 h-3" />}
                                  保存
                                </button>
                                <button onClick={() => setEditing(null)}
                                  className="btn-ghost text-xs">取消</button>
                                <span className="text-[11px] text-slate-400 ml-2">
                                  保存时会自动记录版本历史
                                </span>
                              </div>
                            </>
                          ) : (
                            <>
                              <pre className="text-xs font-mono text-slate-600 bg-slate-50 rounded-xl p-4 max-h-96 overflow-auto whitespace-pre-wrap">
                                {item.value}
                              </pre>
                              <div className="flex items-center gap-2 mt-3 flex-wrap">
                                <button onClick={() => { setEditing(id); setEditValue(item.value) }}
                                  className="btn-ghost text-xs">编辑</button>
                                <button onClick={() => openHistory(item)}
                                  className="btn-ghost text-xs text-primary-600 hover:text-primary-700 flex items-center gap-1">
                                  <History className="w-3 h-3" /> 版本历史
                                </button>
                                <button onClick={() => handleReset(item.module, item.name)}
                                  className="btn-ghost text-xs text-amber-600 hover:text-amber-700 flex items-center gap-1">
                                  <RotateCcw className="w-3 h-3" /> 恢复默认
                                </button>
                                {/* v2.3: 修改人/时间信息 */}
                                <div className="ml-auto text-[11px] text-slate-400 flex items-center gap-2">
                                  {item.updated_by && <span>by <span className="text-slate-600">{item.updated_by}</span></span>}
                                  <span>更新于 {new Date(item.updated_at).toLocaleString('zh-CN')}</span>
                                </div>
                              </div>
                            </>
                          )}
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* v2.3: 版本历史 Drawer */}
      {historyPrompt && (
        <PromptHistoryDrawer
          prompt={historyPrompt}
          items={historyItems}
          loading={historyLoading}
          onClose={() => setHistoryPrompt(null)}
          onRestore={handleRestore}
        />
      )}
    </div>
  )
}
