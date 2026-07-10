import { useState, useEffect } from 'react'
import { LayoutDashboard, Package, Users, ArrowRight } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { apiGet } from '../api/client'
import { useProduct } from '../store/ProductContext'

export default function AdminDashboard() {
  const [data, setData] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const { setSelectedProduct } = useProduct()
  const navigate = useNavigate()

  useEffect(() => {
    apiGet('/products/admin/dashboard').then(d => setData(d)).finally(() => setLoading(false))
  }, [])

  const enterProduct = (product: any) => {
    setSelectedProduct(product.id, product.name)
    navigate('/dashboard', { replace: true })
  }

  if (loading) return <div className="p-6">加载中...</div>
  if (!data) return <div className="p-6 text-slate-400">加载失败</div>

  return (
    <div className="space-y-6">
      <div>
        <h1 className="page-title flex items-center gap-2">
          <LayoutDashboard className="w-6 h-6 text-primary-500" />
          管理后台
        </h1>
        <p className="text-sm text-slate-500 mt-1">系统概览与产品入口</p>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
        <div className="card flex items-center gap-4">
          <div className="w-10 h-10 rounded-xl bg-blue-100 flex items-center justify-center">
            <Package className="w-5 h-5 text-blue-600" />
          </div>
          <div><p className="text-2xl font-bold text-slate-800">{data.total_products}</p><p className="text-xs text-slate-400">产品</p></div>
        </div>
        <div className="card flex items-center gap-4">
          <div className="w-10 h-10 rounded-xl bg-emerald-100 flex items-center justify-center">
            <Users className="w-5 h-5 text-emerald-600" />
          </div>
          <div><p className="text-2xl font-bold text-slate-800">{data.total_users}</p><p className="text-xs text-slate-400">用户</p></div>
        </div>
        <div className="card flex items-center gap-4">
          <div className="w-10 h-10 rounded-xl bg-purple-100 flex items-center justify-center">
            <Package className="w-5 h-5 text-purple-600" />
          </div>
          <div><p className="text-2xl font-bold text-slate-800">{(data.products || []).length}</p><p className="text-xs text-slate-400">可访问</p></div>
        </div>
      </div>

      <div className="card p-0 overflow-hidden">
        <div className="px-5 py-3.5 border-b border-slate-100 bg-slate-50/50">
          <h2 className="text-sm font-semibold text-slate-600">可访问产品</h2>
        </div>
        {(data.products || []).length === 0 ? (
          <div className="p-8 text-center text-sm text-slate-400">暂无产品</div>
        ) : (
          <table className="w-full">
            <tbody>
              {(data.products || []).map((p: any) => (
                <tr key={p.id} className="border-b border-slate-50 hover:bg-slate-50/50 transition-colors">
                  <td className="px-5 py-3.5">
                    <p className="text-sm font-semibold text-slate-800">{p.name}</p>
                    <p className="text-xs text-slate-400">{p.description || '无描述'}</p>
                  </td>
                  <td className="px-5 py-3.5 text-sm text-slate-500 text-right">{p.requirement_count} 条需求</td>
                  <td className="px-5 py-3.5 text-right">
                    <button onClick={() => enterProduct(p)} className="btn-primary text-xs flex items-center gap-1">
                      进入 <ArrowRight className="w-3 h-3" />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
