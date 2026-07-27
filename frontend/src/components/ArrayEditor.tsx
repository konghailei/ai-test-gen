import { useState } from 'react'
import { Plus, Trash2, ChevronDown, Check } from 'lucide-react'

/** 列定义 */
interface Column {
  key: string
  label: string
  type?: 'text' | 'select' | 'number' | 'checkbox' | 'multiselect'
  options?: { value: string; label: string }[]  // type=select/multiselect 时使用
  optionsSource?: 'fps'  // 从外部数据源获取选项（fps = 功能点列表）
  placeholder?: string
  flex?: number  // 列宽比例，默认 1
  hidden?: boolean  // 是否隐藏该列（数据中保留，但不显示在表头和单元格中）
  defaultValue?: (index: number) => any  // 自动生成默认值（用于 hidden 列，如 id）
}

/** 多选下拉单元格 */
function MultiselectCell({ value, options, onChange }: {
  value: string[]
  options: { value: string; label: string }[]
  onChange: (val: string[]) => void
}) {
  const [open, setOpen] = useState(false)
  const selected: string[] = Array.isArray(value) ? value : []
  const toggle = (val: string) => {
    if (selected.includes(val)) {
      onChange(selected.filter(v => v !== val))
    } else {
      onChange([...selected, val])
    }
  }
  const labels = selected.map(v => options.find(o => o.value === v)?.label || v)
  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="text-xs border border-slate-200 rounded px-1.5 py-1 bg-white w-full text-left flex items-center justify-between hover:border-primary-300 focus:border-primary-300 focus:ring-1 focus:ring-primary-200 outline-none"
      >
        <span className="truncate flex-1">
          {selected.length === 0
            ? <span className="text-slate-400">选择...</span>
            : labels.join(', ')}
        </span>
        <ChevronDown className="w-3 h-3 flex-shrink-0 ml-1 text-slate-400" />
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} />
          <div className="absolute z-20 mt-1 bg-white border border-slate-200 rounded-md shadow-lg max-h-40 overflow-y-auto min-w-full w-max">
            {options.length === 0 && (
              <div className="px-2 py-1.5 text-xs text-slate-400 italic">暂无可选功能点</div>
            )}
            {options.map(opt => {
              const checked = selected.includes(opt.value)
              return (
                <label
                  key={opt.value}
                  className="flex items-center gap-1.5 px-2 py-1 hover:bg-slate-50 cursor-pointer text-xs whitespace-nowrap"
                  onClick={() => toggle(opt.value)}
                >
                  <span className={`w-3.5 h-3.5 rounded border flex items-center justify-center flex-shrink-0 ${checked ? 'bg-primary-500 border-primary-500' : 'border-slate-300 bg-white'}`}>
                    {checked && <Check className="w-2.5 h-2.5 text-white" />}
                  </span>
                  <span>{opt.label}</span>
                </label>
              )
            })}
          </div>
        </>
      )}
    </div>
  )
}

/**
 * 行内可编辑对象数组编辑器。
 *
 * 按列定义渲染表格，每行可编辑、新增、删除。
 *
 * @param columns    列定义
 * @param items      当前数据
 * @param onChange   数据变更回调
 * @param title      标题（用于空状态提示）
 * @param optionsData 外部选项数据源（如功能点列表），key 为 optionsSource 值
 */
export default function ArrayEditor({
  columns,
  items,
  onChange,
  title,
  optionsData,
}: {
  columns: Column[]
  items: any[]
  onChange: (items: any[]) => void
  title?: string
  optionsData?: Record<string, { value: string; label: string }[]>
}) {
  const [data, setData] = useState<any[]>(items)

  // 仅显示非 hidden 列（hidden 列数据保留，但不渲染表头和单元格）
  const visibleColumns = columns.filter(c => !c.hidden)

  // 解析列的选项：优先用列自身 options，否则从 optionsData 中按 optionsSource 获取
  const resolveOptions = (col: Column): { value: string; label: string }[] => {
    if (col.options) return col.options
    if (col.optionsSource && optionsData) {
      return optionsData[col.optionsSource] || []
    }
    return []
  }

  const updateCell = (rowIdx: number, key: string, value: any) => {
    const next = data.map((row, i) => i === rowIdx ? { ...row, [key]: value } : row)
    setData(next)
    onChange(next)
  }

  const addRow = () => {
    const empty: any = {}
    columns.forEach(c => {
      if (c.defaultValue) {
        // hidden 列自动生成默认值（如 id）
        empty[c.key] = c.defaultValue(data.length + 1)
      } else if (c.type === 'checkbox') {
        empty[c.key] = false
      } else if (c.type === 'number') {
        empty[c.key] = data.length + 1
      } else if (c.type === 'multiselect') {
        empty[c.key] = []
      } else {
        empty[c.key] = ''
      }
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
    if (col.type === 'multiselect') {
      return (
        <MultiselectCell
          value={value}
          options={resolveOptions(col)}
          onChange={val => updateCell(rowIdx, col.key, val)}
        />
      )
    }
    if (col.type === 'select') {
      const opts = resolveOptions(col)
      return (
        <select
          value={value || ''}
          onChange={e => updateCell(rowIdx, col.key, e.target.value)}
          className="text-xs border border-slate-200 rounded px-1.5 py-1 bg-white w-full focus:border-primary-300 focus:ring-1 focus:ring-primary-200 outline-none appearance-none bg-no-repeat"
          style={{
            backgroundImage: "url(\"data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%2394a3b8' stroke-width='2'%3E%3Cpolyline points='6 9 12 15 18 9'/%3E%3C/svg%3E\")",
            backgroundPosition: 'right 4px center',
            backgroundSize: '12px',
            paddingRight: '20px',
          }}
        >
          <option value="">-</option>
          {opts.map(opt => (
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
        {visibleColumns.map(c => (
          <div key={c.key} className="px-2 py-1.5" style={{ flex: c.flex ?? 1 }}>{c.label}</div>
        ))}
        <div className="w-10 flex-shrink-0" />
      </div>

      {/* 数据行 */}
      {data.length === 0 ? (
        <div className="px-3 py-4 text-xs text-slate-400 text-center italic">
          暂无数据
        </div>
      ) : (
        data.map((row, i) => (
          <div key={i} className="flex items-center border-b border-slate-100 last:border-0 hover:bg-slate-50/50">
            {visibleColumns.map(c => (
              <div key={c.key} className="px-2 py-1" style={{ flex: c.flex ?? 1 }}>
                {renderCell(c, row[c.key], i)}
              </div>
            ))}
            <div className="w-10 flex-shrink-0 flex justify-center">
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
