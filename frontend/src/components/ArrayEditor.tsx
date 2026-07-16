import { useState } from 'react'
import { Plus, Trash2 } from 'lucide-react'

/** 列定义 */
interface Column {
  key: string
  label: string
  type?: 'text' | 'select' | 'number' | 'checkbox'
  options?: { value: string; label: string }[]  // type=select 时使用
  placeholder?: string
}

/**
 * 行内可编辑对象数组编辑器。
 *
 * 按列定义渲染表格，每行可编辑、新增、删除。
 *
 * @param columns  列定义
 * @param items    当前数据
 * @param onChange 数据变更回调
 * @param title    标题（用于空状态提示）
 */
export default function ArrayEditor({
  columns,
  items,
  onChange,
  title,
}: {
  columns: Column[]
  items: any[]
  onChange: (items: any[]) => void
  title?: string
}) {
  const [data, setData] = useState<any[]>(items)

  const updateCell = (rowIdx: number, key: string, value: any) => {
    const next = data.map((row, i) => i === rowIdx ? { ...row, [key]: value } : row)
    setData(next)
    onChange(next)
  }

  const addRow = () => {
    const empty: any = {}
    columns.forEach(c => {
      empty[c.key] = c.type === 'checkbox' ? false : c.type === 'number' ? data.length + 1 : ''
    })
    const next = [...data, empty]
    setData(next)
    onChange(next)
  }

  const deleteRow = (rowIdx: number) => {
    const next = data.filter((_, i) => i !== rowIdx)
    setData(next)
    onChange(next)
  }

  const renderCell = (col: Column, value: any, rowIdx: number) => {
    if (col.type === 'select' && col.options) {
      return (
        <select
          value={value || ''}
          onChange={e => updateCell(rowIdx, col.key, e.target.value)}
          className="text-xs border border-slate-200 rounded px-1.5 py-1 bg-white w-full focus:border-primary-300 focus:ring-1 focus:ring-primary-200 outline-none"
        >
          <option value="">-</option>
          {col.options.map(opt => (
            <option key={opt.value} value={opt.value}>{opt.label}</option>
          ))}
        </select>
      )
    }
    if (col.type === 'checkbox') {
      return (
        <input type="checkbox"
          checked={!!value}
          onChange={e => updateCell(rowIdx, col.key, e.target.checked)}
          className="w-4 h-4 text-primary-500 rounded border-slate-300"
        />
      )
    }
    return (
      <input
        type="text"
        value={value ?? ''}
        onChange={e => updateCell(rowIdx, col.key, e.target.value)}
        placeholder={col.placeholder || ''}
        className="text-xs border border-slate-200 rounded px-1.5 py-1 bg-white w-full focus:border-primary-300 focus:ring-1 focus:ring-primary-200 outline-none"
      />
    )
  }

  return (
    <div className="border border-slate-200 rounded-lg overflow-hidden">
      {/* 表头 */}
      <div className="flex bg-slate-50 text-[11px] text-slate-500 font-medium border-b border-slate-200">
        {columns.map(c => (
          <div key={c.key} className="flex-1 px-2 py-1.5">{c.label}</div>
        ))}
        <div className="w-10" />
      </div>

      {/* 数据行 */}
      {data.length === 0 ? (
        <div className="px-3 py-4 text-xs text-slate-400 text-center italic">
          暂无数据
        </div>
      ) : (
        data.map((row, i) => (
          <div key={i} className="flex items-center border-b border-slate-100 last:border-0 hover:bg-slate-50/50">
            {columns.map(c => (
              <div key={c.key} className="flex-1 px-2 py-1">
                {renderCell(c, row[c.key], i)}
              </div>
            ))}
            <div className="w-10 flex justify-center">
              <button
                onClick={() => deleteRow(i)}
                className="p-1 text-slate-300 hover:text-red-500 rounded transition-colors"
                title="删除此行"
              >
                <Trash2 className="w-3.5 h-3.5" />
              </button>
            </div>
          </div>
        ))
      )}

      {/* 添加行 */}
      <div className="flex border-t border-slate-200">
        <button
          onClick={addRow}
          className="flex items-center gap-1 px-3 py-2 text-[11px] text-primary-500 hover:bg-primary-50 w-full transition-colors"
        >
          <Plus className="w-3 h-3" />
          添加一行
        </button>
      </div>
    </div>
  )
}
