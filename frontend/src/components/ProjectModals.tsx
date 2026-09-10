import { useState } from 'react'
import { Edit3, Save, X, Archive, Loader2 } from 'lucide-react'
import { products as prodApi } from '../api/client'

/**
 * 项目编辑 / 归档弹窗（v3.2.11 从 ProjectManagement 抽出，供列表页与项目详情页复用）。
 */

/** 编辑弹窗 */
export function EditModal({
  name, description, version, onSave, onClose, saving
}: {
  name: string; description: string; version?: string;
  onSave: (name: string, desc: string, version?: string) => Promise<void>;
  onClose: () => void; saving: boolean
}) {
  const [n, setN] = useState(name)
  const [d, setD] = useState(description)
  const [v, setV] = useState(version || '1.0')
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" style={{ background: 'rgba(0, 0, 0, 0.32)', backdropFilter: 'blur(4px)' }} onClick={() => !saving && onClose()}>
      <div className="max-w-md w-full p-6 animate-slide-up" style={{ background: 'var(--background)', borderRadius: 'calc(var(--radius) * 1.1)', boxShadow: 'var(--shadow-apple-xl)' }} onClick={e => e.stopPropagation()}>
        <div className="flex items-center gap-3 mb-4">
          <Edit3 className="w-5 h-5" style={{ color: 'var(--brand-500)' }} />
          <h2 className="text-lg font-semibold" style={{ color: 'var(--foreground)' }}>编辑项目</h2>
        </div>
        <div className="space-y-3">
          <div>
            <label className="label">名称</label>
            <input value={n} onChange={e => setN(e.target.value)} className="input-field text-sm" autoFocus />
          </div>
          <div>
            <label className="label">描述</label>
            <textarea value={d} onChange={e => setD(e.target.value)} className="input-field text-sm resize-none" rows={3} placeholder="可选" />
          </div>
          <div>
            <label className="label">版本号</label>
            <input value={v} onChange={e => setV(e.target.value)} className="input-field text-sm" placeholder="如 1.0、2.1.0" />
          </div>
        </div>
        <div className="flex justify-end gap-3 mt-5">
          <button onClick={onClose} className="btn-secondary" disabled={saving}><X className="w-4 h-4" /> 取消</button>
          <button onClick={() => onSave(n, d, v)} disabled={!n.trim() || saving} className="btn-primary">
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />} 保存
          </button>
        </div>
      </div>
    </div>
  )
}

/** v2.45 项目归档弹窗 */
export function ArchiveModal({ projId, projName, onClose, onArchived }: {
  projId: string; projName: string; onClose: () => void; onArchived: () => void
}) {
  // v2.45 修复：使用本地日期方法生成默认值，避免 toISOString() 返回 UTC 时间导致 UTC+8 凌晨显示前一天
  const todayLocal = () => {
    const d = new Date()
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  }
  const [goLiveDate, setGoLiveDate] = useState(todayLocal())
  const [changeSummary, setChangeSummary] = useState('')
  const [archiving, setArchiving] = useState(false)
  const [error, setError] = useState('')
  // v2.45 增强：归档为高影响操作，需输入项目名二次确认防止误操作
  const [confirmName, setConfirmName] = useState('')

  const handleArchive = async () => {
    setArchiving(true); setError('')
    try {
      await prodApi.archiveProject(projId, goLiveDate, changeSummary)
      // v2.8.58: 后端改为 Celery 异步派发，归档立即返回，不再阻塞
      onArchived()
    } catch (e: any) {
      setError(e.message || '归档失败')
    } finally { setArchiving(false) }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" style={{ background: 'rgba(0, 0, 0, 0.32)', backdropFilter: 'blur(4px)' }} onClick={() => !archiving && onClose()}>
      <div className="max-w-md w-full p-6 animate-slide-up" style={{ background: 'var(--background)', borderRadius: 'calc(var(--radius) * 1.1)', boxShadow: 'var(--shadow-apple-xl)' }} onClick={e => e.stopPropagation()}>
        <div className="flex items-center gap-3 mb-4">
          <Archive className="w-5 h-5" style={{ color: 'var(--foreground-tertiary)' }} />
          <h2 className="text-lg font-semibold" style={{ color: 'var(--foreground)' }}>归档项目</h2>
        </div>
        <p className="text-sm mb-4" style={{ color: 'var(--foreground-secondary)' }}>
          项目「{projName}」归档后，项目下所有需求将自动归档并写入知识库，供后续需求检索参考。
        </p>
        <div className="space-y-3">
          <div>
            <label className="label">上线时间 *</label>
            <input
              type="date"
              value={goLiveDate}
              onChange={e => setGoLiveDate(e.target.value)}
              className="input-field text-sm"
              autoFocus
            />
            <p className="text-xs mt-1" style={{ color: 'var(--foreground-tertiary)' }}>用于知识库检索时区分最新需求</p>
          </div>
          <div>
            <label className="label">变更说明（可选）</label>
            <textarea
              value={changeSummary}
              onChange={e => setChangeSummary(e.target.value)}
              className="input-field text-sm resize-none"
              rows={2}
              placeholder="本次归档的变更说明，将记录到版本历史"
            />
          </div>
          <div>
            <label className="label">输入项目名以确认 *</label>
            <input
              value={confirmName}
              onChange={e => setConfirmName(e.target.value)}
              className="input-field text-sm"
              placeholder={projName}
            />
            <p className="text-xs mt-1" style={{ color: 'var(--foreground-tertiary)' }}>
              归档会写入知识库并冻结依赖版本快照，请输入项目名「{projName}」确认
            </p>
          </div>
          {error && (
            <div className="text-sm p-2" style={{ background: 'var(--state-error-surface)', color: 'var(--state-error)', borderRadius: 'calc(var(--radius) * 0.4)' }}>
              {error}
            </div>
          )}
        </div>
        <div className="flex justify-end gap-3 mt-5">
          <button onClick={onClose} className="btn-secondary" disabled={archiving}><X className="w-4 h-4" /> 取消</button>
          <button
            onClick={handleArchive}
            disabled={!goLiveDate || archiving || confirmName.trim() !== projName}
            className="btn-primary"
          >
            {archiving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Archive className="w-4 h-4" />} 确认归档
          </button>
        </div>
      </div>
    </div>
  )
}
