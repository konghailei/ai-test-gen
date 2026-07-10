import { useState, useEffect } from 'react'
import { Share2, Loader2, GitBranch, AlertTriangle, ArrowRight, Package, Puzzle } from 'lucide-react'
import { dependencyGraph as graphApi, products as prodApi } from '../api/client'
import type { ProjectImpact } from '../types'
import { useProduct } from '../store/ProductContext'

export default function ImpactAnalysis() {
  const { selectedProductId, products } = useProduct()
  const [projects, setProjects] = useState<Record<string, { id: string; name: string }[]>>({})
  const [selectedProj, setSelectedProj] = useState('')
  const [impact, setImpact] = useState<ProjectImpact | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (selectedProductId) loadProjects(selectedProductId)
    setSelectedProj('')
    setImpact(null)
  }, [selectedProductId])

  const loadProjects = async (pid: string) => {
    if (!projects[pid]) {
      const projs = await prodApi.listProjects(pid)
      setProjects(p => ({ ...p, [pid]: projs || [] }))
    }
  }

  const handleAnalyze = async () => {
    if (!selectedProj) return
    setLoading(true); setError(''); setImpact(null)
    try {
      const res = await graphApi.analyzeProject(selectedProj)
      setImpact(res)
    } catch (e: any) {
      setError(e.message || '分析失败')
    } finally { setLoading(false) }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="page-title flex items-center gap-2">
            <Share2 className="w-5 h-5 text-primary-500" /> 项目影响分析
          </h1>
          <p className="text-sm text-slate-500 mt-1">选择项目版本 → 模块图 BFS 遍历 → 输出影响范围和回归建议</p>
        </div>
      </div>

      {/* 选择项目（产品由顶部栏控制） */}
      <div className="card">
        <div className="flex items-end gap-3">
          <div className="flex-1">
            <label className="label">当前产品</label>
            <div className="text-sm font-medium text-slate-700 bg-slate-50 rounded-lg px-3 py-2">
              {selectedProductId
                ? products.find(p => p.id === selectedProductId)?.name || selectedProductId
                : '请先在顶部栏选择产品'}
            </div>
          </div>
          <div className="flex-1">
            <label className="label">选择项目/版本</label>
            <div className="flex gap-2">
              <select value={selectedProj} onChange={e => setSelectedProj(e.target.value)}
                className="select-field text-sm flex-1">
                <option value="">选择项目...</option>
                {Object.values(projects).flat().map(p => (
                  <option key={p.id} value={p.id}>{p.name}</option>
                ))}
              </select>
              <button onClick={handleAnalyze} disabled={!selectedProj || loading}
                className="btn-primary whitespace-nowrap">
                {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <GitBranch className="w-4 h-4" />}
                分析影响
              </button>
            </div>
          </div>
        </div>
      </div>

      {error && (
        <div className="bg-red-50 border border-red-100 rounded-2xl p-4 text-sm text-red-700">{error}</div>
      )}

      {/* 分析结果 */}
      {impact && (
        <>
          {/* 项目信息 */}
          <div className="card">
            <div className="flex items-center gap-3 mb-1">
              <Package className="w-5 h-5 text-primary-500" />
              <h2 className="text-lg font-semibold">{impact.project.name}</h2>
              <span className="badge bg-primary-50 text-primary-600">v{impact.project.version}</span>
            </div>
          </div>

          {/* 模块影响概览 */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div className="card text-center">
              <p className="text-2xl font-bold text-primary-600">{impact.direct_modules.length}</p>
              <p className="text-xs text-slate-400 mt-1">直接涉及模块</p>
            </div>
            <div className="card text-center">
              <p className="text-2xl font-bold text-amber-600">{impact.indirect_modules.length}</p>
              <p className="text-xs text-slate-400 mt-1">间接受影响模块</p>
            </div>
            <div className="card text-center">
              <p className="text-2xl font-bold text-emerald-600">{impact.regression_requirements.length}</p>
              <p className="text-xs text-slate-400 mt-1">需回归的需求</p>
            </div>
          </div>

          {/* 模块影响图谱 */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <div className="card">
              <h3 className="text-sm font-semibold text-slate-700 mb-3 flex items-center gap-2">
                <Puzzle className="w-4 h-4 text-primary-500" /> 直接涉及的模块
              </h3>
              {impact.direct_modules.length === 0 ? (
                <p className="text-xs text-slate-400">无</p>
              ) : (
                <div className="space-y-1.5">
                  {impact.direct_modules.map(m => (
                    <div key={m.id} className="flex items-center gap-2 p-2 rounded-lg bg-primary-50 text-sm">
                      <Puzzle className="w-4 h-4 text-primary-500" />
                      <span className="font-medium text-slate-700">{m.name}</span>
                      <span className="text-xs text-slate-400 font-mono ml-auto">{m.id}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div className="card">
              <h3 className="text-sm font-semibold text-slate-700 mb-3 flex items-center gap-2">
                <AlertTriangle className="w-4 h-4 text-amber-500" /> 间接受影响模块（BFS 遍历）
              </h3>
              {impact.indirect_modules.length === 0 ? (
                <p className="text-xs text-slate-400">无间接受影响模块</p>
              ) : (
                <div className="space-y-1.5">
                  {impact.indirect_modules.map((m, i) => (
                    <div key={i} className="flex items-center gap-2 p-2 rounded-lg bg-amber-50 text-sm">
                      <ArrowRight className="w-4 h-4 text-amber-500" />
                      <span className="font-medium text-slate-700">{m.name}</span>
                      <span className="text-xs text-slate-400">({m.relation_type}, {m.depth}跳)</span>
                      <span className="text-xs text-slate-400 font-mono ml-auto">{m.module_id}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>

          {/* 回归建议 */}
          <div className="card">
            <h3 className="text-sm font-semibold text-slate-700 mb-3">
              需回归的需求 ({impact.regression_requirements.length})
            </h3>
            {impact.regression_requirements.length === 0 ? (
              <p className="text-xs text-slate-400">无跨项目影响</p>
            ) : (
              <div className="space-y-2">
                {impact.regression_requirements.map(r => (
                  <div key={r.id} className="p-3 bg-slate-50 rounded-xl text-sm">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-xs text-slate-400">{r.id}</span>
                      <span className="font-medium text-slate-700">{r.title}</span>
                    </div>
                    <div className="flex flex-wrap gap-1 mt-1.5">
                      {r.matched_modules.map(mm => (
                        <span key={mm} className="badge bg-emerald-50 text-emerald-600 text-[10px]">{mm}</span>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* 回归测试用例（通过追溯矩阵定位） */}
          <div className="card">
            <h3 className="text-sm font-semibold text-slate-700 mb-2">
              追溯矩阵精确定位
            </h3>
            <div className="text-xs text-slate-500 leading-relaxed bg-slate-50 rounded-xl p-4">
              <p className="font-medium text-slate-600 mb-1">分析流程：</p>
              <p>Step 1: 项目「{impact.project.name}」涉及 {impact.direct_modules.length} 个模块</p>
              <p>Step 2: 模块图 BFS 遍历 → {impact.indirect_modules.length} 个模块间接受影响</p>
              <p>Step 3: 追溯矩阵定位 → {impact.regression_requirements.length} 个需求需回归</p>
              <p className="mt-2 text-slate-400">
                💡 接入追溯矩阵后，可进一步精确定位到具体测试用例。
              </p>
            </div>
          </div>
        </>
      )}
    </div>
  )
}
