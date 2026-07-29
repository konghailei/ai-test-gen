import { useState } from 'react'
import { Loader2 } from 'lucide-react'

interface Props {
  stages: string[]
  currentStage: string
  progress: number
  message: string
  onCancel?: () => void
}

const stageLabels: Record<string, string> = {
  parse: '需求解析',
  select_channel: '通道选择',
  clarify: '需求澄清',
  gate: '质量门禁',
  impact: '影响分析',
  generate: '用例生成',
  review: '质量审核',
  done: '完成',
  error: '出错',
}

// v2.0 修复：按 stage 名称映射图标，避免按索引取值导致错位
const stageIcons: Record<string, string> = {
  parse: '📄',
  select_channel: '🔀',
  clarify: '💬',
  gate: '🛡️',
  impact: '🎯',
  generate: '⚙️',
  review: '✅',
  done: '✅',
  error: '❌',
}

export default function SSEProgress({ stages, currentStage, progress, message, onCancel }: Props) {
  const currentIdx = stages.indexOf(currentStage)

  return (
    <div className="card space-y-4">
      {/* Progress bar */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <span className="text-sm font-medium text-slate-700">{message}</span>
          <span className="text-xs font-mono text-slate-400">{progress}%</span>
        </div>
        <div className="w-full h-2.5 bg-slate-100 rounded-full overflow-hidden">
          <div
            className="progress-bar h-full rounded-full transition-all duration-500 ease-out"
            style={{ width: `${progress}%` }}
          />
        </div>
      </div>

      {/* Stage timeline */}
      <div className="flex items-center gap-1 overflow-x-auto py-1">
        {stages.filter(s => s !== 'done' && s !== 'error').map((stage, i) => {
          const isDone = currentIdx > i
          const isActive = currentIdx === i
          const isError = currentStage === 'error' && i === currentIdx
          return (
            <div key={stage} className="flex items-center gap-1 flex-shrink-0">
              <div className={`flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs font-medium transition-all ${
                isDone ? 'bg-primary-100 text-primary-700' :
                isActive ? 'bg-primary-500 text-white shadow-sm' :
                isError ? 'bg-red-100 text-red-700' :
                'bg-slate-50 text-slate-400'
              }`}>
                <span>{stageIcons[stage] || '•'}</span>
                <span>{stageLabels[stage] || stage}</span>
              </div>
              {i < stages.length - 2 && (
                <div className={`w-4 h-px ${isDone ? 'bg-primary-300' : 'bg-slate-200'}`} />
              )}
            </div>
          )
        })}
      </div>

      {/* Cancel */}
      {currentStage !== 'done' && currentStage !== 'error' && onCancel && (
        <button onClick={onCancel} className="btn-ghost text-red-500 text-xs">取消生成</button>
      )}
    </div>
  )
}
