import { useState, useEffect } from 'react'
import { Save, RotateCcw, Loader2, FileText, CheckCircle2, Search, TestTube, Shield, GitBranch } from 'lucide-react'
import { promptApi } from '../api/client'
import type { PromptItem } from '../types'

const TABS = [
  { key: 'req', label: '需求分析', icon: Search, modules: ['req_agent', 'clarify_agent'], desc: '需求解析与澄清 Agent 提示词' },
  { key: 'gen', label: '用例生成', icon: TestTube, modules: ['gen_agent'], desc: '测试用例自动生成 Agent 提示词' },
  { key: 'review', label: '用例审核', icon: Shield, modules: ['review_agent'], desc: '用例质量审核 Agent 提示词' },
  { key: 'impact', label: '影响分析', icon: GitBranch, modules: ['impact_agent'], desc: '需求变更影响分析 Agent 提示词' },
]

const MODULE_NAMES: Record<string, string> = {
  req_agent: 'ReqAgent（需求解析）',
  clarify_agent: 'ClarifyAgent（需求澄清）',
  gen_agent: 'GenAgent（用例生成）',
  review_agent: 'ReviewAgent（用例审核）',
  impact_agent: 'ImpactAgent（影响分析）',
}

export default function PromptManager() {
  const [items, setItems] = useState<PromptItem[]>([])
  const [loading, setLoading] = useState(true)
  const [tab, setTab] = useState('req')
  const [expanded, setExpanded] = useState<string | null>(null)
  const [editing, setEditing] = useState<string | null>(null)
  const [editValue, setEditValue] = useState('')
  const [saving, setSaving] = useState(false)
  const [toast, setToast] = useState('')

  const load = async () => {
    setLoading(true)
    try {
      const res = await promptApi.list()
      setItems(res.prompts || [])
    } catch { /* ignore */ }
    setLoading(false)
  }
  useEffect(() => { load() }, [])

  const handleSave = async (module: string, name: string) => {
    setSaving(true)
    try {
      await promptApi.update(module, name, editValue)
      setToast('已保存')
      setTimeout(() => setToast(''), 2000)
      setEditing(null)
      load()
    } catch (e: any) { alert('保存失败: ' + e.message) }
    setSaving(false)
  }

  const handleReset = async (module: string, name: string) => {
    if (!confirm('确认恢复为默认提示词？')) return
    try {
      await promptApi.reset(module, name)
      setToast('已恢复默认')
      setTimeout(() => setToast(''), 2000)
      if (editing === `${module}.${name}`) setEditing(null)
      load()
    } catch (e: any) { alert('重置失败: ' + e.message) }
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
        <p className="text-sm text-slate-500 mt-1">按模块管理 Agent 系统提示词，修改后即时生效（热重载）</p>
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

                  return (
                    <div key={id}>
                      <button onClick={() => setExpanded(isExpanded ? null : id)}
                        className="w-full px-5 py-3 flex items-center justify-between hover:bg-slate-50/50 transition-colors text-left">
                        <div className="flex items-center gap-3 min-w-0">
                          <FileText className="w-4 h-4 text-slate-400 flex-shrink-0" />
                          <div className="min-w-0">
                            <p className="text-sm font-medium text-slate-700 truncate">{item.name}</p>
                            <p className="text-xs text-slate-400 truncate">{item.description}</p>
                          </div>
                        </div>
                        <span className="text-xs text-slate-300 flex-shrink-0 ml-3">
                          {item.value.length} 字
                        </span>
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
                              </div>
                            </>
                          ) : (
                            <>
                              <pre className="text-xs font-mono text-slate-600 bg-slate-50 rounded-xl p-4 max-h-96 overflow-auto whitespace-pre-wrap">
                                {item.value}
                              </pre>
                              <div className="flex items-center gap-2 mt-3">
                                <button onClick={() => { setEditing(id); setEditValue(item.value) }}
                                  className="btn-ghost text-xs">编辑</button>
                                <button onClick={() => handleReset(item.module, item.name)}
                                  className="btn-ghost text-xs text-amber-600 hover:text-amber-700 flex items-center gap-1">
                                  <RotateCcw className="w-3 h-3" /> 恢复默认
                                </button>
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
    </div>
  )
}
