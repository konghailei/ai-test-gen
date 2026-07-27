import { useState, useEffect } from 'react'
import { Settings as SettingsIcon, Key, Server, RefreshCw, CheckCircle2, XCircle, Loader2, Eye, EyeOff, AlertCircle, Plus, Trash2, Star, Lock, Shield, Database, Image, Sparkles, Code } from 'lucide-react'
import { settings as settingsApi, health, auth, knowledge } from '../api/client'
import type { LLMConfig, LLMProviderItem, HealthStatus } from '../types'

const DEFAULT_PROVIDER: LLMProviderItem = {
  id: 0,
  provider: 'deepseek',
  provider_label: 'DeepSeek',
  api_key: '',
  base_url: 'https://api.deepseek.com/v1',
  model: 'deepseek-chat',
  is_primary: false,
  enable_thinking: false,
  thinking_params: '',
}

export default function Settings() {
  const [config, setConfig] = useState<LLMConfig | null>(null)
  const [healthStatus, setHealth] = useState<HealthStatus | null>(null)
  const [loading, setLoading] = useState(true)

  // 编辑中的供应商列表
  const [providers, setProviders] = useState<LLMProviderItem[]>([])
  const [editing, setEditing] = useState(false)
  const [saving, setSaving] = useState(false)

  // 测试连接
  const [testing, setTesting] = useState<string | null>(null) // provider being tested
  const [testResults, setTestResults] = useState<Record<string, { success: boolean; latency_ms: number; message: string }>>({})

  const [showKeys, setShowKeys] = useState<Record<string, boolean>>({})

  // 修改密码
  const [passwordForm, setPasswordForm] = useState({ current: '', new: '', confirm: '' })
  const [passwordFormOpen, setPasswordFormOpen] = useState(false)
  const [changingPassword, setChangingPassword] = useState(false)
  const [passwordMsg, setPasswordMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [showNewPwd, setShowNewPwd] = useState(false)
  const [showCurrentPwd, setShowCurrentPwd] = useState(false)

  // KB 专属 LLM 选择器
  const [kbLlmProvider, setKbLlmProvider] = useState('')
  const [kbLlmProviders, setKbLlmProviders] = useState<Array<{ provider: string; provider_label: string; model: string }>>([])
  const [kbLlmLoading, setKbLlmLoading] = useState(false)

  // 多模态 LLM 选择器（v2.18: 图片/流程图识别专用）
  const [visionLlmProvider, setVisionLlmProvider] = useState('')
  const [visionLlmProviders, setVisionLlmProviders] = useState<Array<{ provider: string; provider_label: string; model: string }>>([])
  const [visionLlmLoading, setVisionLlmLoading] = useState(false)

  const loadKbLlm = () => {
    knowledge.getKbLlm().then(r => {
      setKbLlmProvider(r.provider || '')
      setKbLlmProviders(r.providers || [])
    }).catch(() => {})
  }

  const loadVisionLlm = () => {
    knowledge.getVisionLlm().then(r => {
      setVisionLlmProvider(r.provider || '')
      setVisionLlmProviders(r.providers || [])
    }).catch(() => {})
  }

  const loadSettings = () => {
    setLoading(true)
    Promise.all([settingsApi.get(), health()]).then(([c, h]) => {
      setConfig(c)
      setHealth(h)
      // 初始化编辑列表
      if (c.providers.length > 0) {
        setProviders(c.providers.map(p => ({ ...p })))
      } else {
        setProviders([{ ...DEFAULT_PROVIDER }])
      }
    }).catch(() => {}).finally(() => setLoading(false))
    loadKbLlm()
    loadVisionLlm()
  }

  useEffect(() => { loadSettings() }, [])

  // 选择内置供应商预设
  const applyPreset = (idx: number, providerKey: string) => {
    const preset = config?.builtin_providers?.[providerKey]
    if (!preset) return
    setProviders(prev => {
      const next = [...prev]
      next[idx] = {
        ...next[idx],
        provider: providerKey,
        provider_label: preset.label,
        base_url: preset.base_url,
        model: preset.model,
      }
      return next
    })
  }

  // 添加供应商行
  const addProvider = () => {
    setProviders(prev => [...prev, { ...DEFAULT_PROVIDER, id: 0, is_primary: prev.length === 0 }])
  }

  // 删除供应商行
  const removeProvider = (idx: number) => {
    setProviders(prev => prev.filter((_, i) => i !== idx))
  }

  // 更新单个字段
  const updateField = (idx: number, field: string, value: string | boolean) => {
    setProviders(prev => {
      const next = [...prev]
      ;(next[idx] as any)[field] = value
      return next
    })
  }

  // 设为主供应商
  const setPrimary = (idx: number) => {
    setProviders(prev => prev.map((p, i) => ({ ...p, is_primary: i === idx })))
  }

  // 获取激活的 API Key（显示脱敏或明文）
  const getDisplayKey = (p: LLMProviderItem) => {
    if (p.api_key && !p.api_key.includes('***')) {
      return showKeys[p.provider] ? p.api_key : p.api_key.slice(0, 8) + '…' + p.api_key.slice(-4)
    }
    return p.api_key || '未配置'
  }

  const handleSave = async () => {
    // 过滤掉完全空的行
    const validProviders = providers.filter(p => p.provider.trim())
    if (validProviders.length === 0) { alert('至少需要配置一个供应商'); return }

    setSaving(true)
    try {
      const updated = await settingsApi.update(validProviders.map(p => ({
        provider: p.provider,
        provider_label: p.provider_label || p.provider,
        api_key: p.api_key,
        base_url: p.base_url || undefined,
        model: p.model || undefined,
        is_primary: p.is_primary,
        enable_thinking: p.enable_thinking || false,
        thinking_params: p.thinking_params || '',
      })))
      setConfig(updated)
      setEditing(false)
      loadSettings()
    } catch (e: any) {
      alert('保存失败: ' + e.message)
    } finally {
      setSaving(false)
    }
  }

  const handleTest = async (provider: string) => {
    setTesting(provider)
    setTestResults(prev => ({ ...prev, [provider]: undefined as any }))
    try {
      const r = await settingsApi.test(provider)
      setTestResults(prev => ({ ...prev, [provider]: r }))
    } catch (e: any) {
      setTestResults(prev => ({ ...prev, [provider]: { success: false, latency_ms: 0, message: e.message } }))
    } finally {
      setTesting(null)
    }
  }

  // 任意供应商是否有已配置的 Key
  const hasAnyKey = providers.some(p => p.api_key && p.api_key.length > 4)

  // 修改密码处理
  const handleChangePassword = async () => {
    setPasswordMsg(null)
    if (passwordForm.new.length < 4) {
      setPasswordMsg({ ok: false, text: '新密码至少需要 4 位' })
      return
    }
    if (passwordForm.new !== passwordForm.confirm) {
      setPasswordMsg({ ok: false, text: '两次输入的新密码不一致' })
      return
    }
    if (passwordForm.current === passwordForm.new) {
      setPasswordMsg({ ok: false, text: '新密码不能与当前密码相同' })
      return
    }
    setChangingPassword(true)
    try {
      await auth.changePassword(passwordForm.current, passwordForm.new)
      setPasswordMsg({ ok: true, text: '密码修改成功，请使用新密码重新登录' })
      setPasswordForm({ current: '', new: '', confirm: '' })
      setPasswordFormOpen(false)
    } catch (e: any) {
      setPasswordMsg({ ok: false, text: e.message || '修改失败' })
    } finally {
      setChangingPassword(false)
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="page-title">系统设置</h1>
        <p className="text-sm mt-1" style={{ color: 'var(--foreground-tertiary)' }}>LLM 供应商配置与系统信息</p>
      </div>

      {/* LLM 供应商配置 */}
      <div className="card space-y-5">
        <div className="flex items-center justify-between">
          <h2 className="section-title flex items-center gap-2">
            <Key className="w-4 h-4" style={{ color: 'var(--brand-500)' }} /> LLM 供应商
          </h2>
          <div className="flex items-center gap-2">
            {!editing && hasAnyKey && (
              <span className="badge flex items-center gap-1" style={{ background: 'var(--state-success-surface)', color: 'var(--state-success)' }}>
                <CheckCircle2 className="w-3 h-3" /> 已配置
              </span>
            )}
            <span className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>{providers.length} 个</span>
          </div>
        </div>

        {/* 查看模式 */}
        {!editing && (
          <div className="space-y-3">
            {providers.length === 0 ? (
              <div className="border p-4 flex items-start gap-3" style={{ background: 'var(--state-warning-surface)', borderColor: 'var(--state-warning)', borderRadius: 'calc(var(--radius) * 0.75)' }}>
                <AlertCircle className="w-5 h-5 flex-shrink-0 mt-0.5" style={{ color: 'var(--state-warning)' }} />
                <div>
                  <p className="text-sm font-medium" style={{ color: 'var(--state-warning)' }}>LLM 未配置</p>
                  <p className="text-xs mt-0.5" style={{ color: 'var(--state-warning)' }}>请配置 API Key 后，系统才能调用大语言模型解析需求和生成测试用例。</p>
                </div>
              </div>
            ) : (
              providers.map((p, idx) => (
                <div key={idx} className="p-4 border" style={p.is_primary
                  ? { background: 'rgba(0, 122, 255, 0.06)', borderColor: 'rgba(0, 122, 255, 0.2)', borderRadius: 'calc(var(--radius) * 0.75)' }
                  : { background: 'var(--background-muted)', borderColor: 'var(--border)', borderRadius: 'calc(var(--radius) * 0.75)' }}>
                  <div className="flex items-center justify-between mb-3">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-semibold" style={{ color: 'var(--foreground-secondary)' }}>{p.provider_label || p.provider}</span>
                      {p.is_primary && <span className="badge text-[10px]" style={{ background: 'rgba(0, 122, 255, 0.12)', color: 'var(--brand-500)' }}>主供应商</span>}
                    </div>
                  </div>
                  <div className="grid grid-cols-2 gap-3 text-xs">
                    <div><span style={{ color: 'var(--foreground-tertiary)' }}>标识</span><br /><span className="font-medium" style={{ color: 'var(--foreground-secondary)' }}>{p.provider}</span></div>
                    <div><span style={{ color: 'var(--foreground-tertiary)' }}>模型</span><br /><span className="font-medium" style={{ color: 'var(--foreground-secondary)' }}>{p.model || '-'}</span></div>
                    <div className="col-span-2"><span style={{ color: 'var(--foreground-tertiary)' }}>API 地址</span><br /><span className="font-medium truncate block" style={{ color: 'var(--foreground-secondary)' }}>{p.base_url || '-'}</span></div>
                    <div className="col-span-2">
                      <span style={{ color: 'var(--foreground-tertiary)' }}>API Key</span><br />
                      <span className="font-medium font-mono text-[11px]" style={{ color: 'var(--foreground-secondary)' }}>
                        {p.api_key ? (showKeys[p.provider] ? p.api_key : _maskKey(p.api_key)) : '未配置'}
                      </span>
                      {p.api_key && (
                        <button onClick={() => setShowKeys(prev => ({ ...prev, [p.provider]: !prev[p.provider] }))} className="ml-2 inline-flex hover:text-[var(--foreground-secondary)]" style={{ color: 'var(--foreground-tertiary)' }}>
                          {showKeys[p.provider] ? <EyeOff className="w-3 h-3" /> : <Eye className="w-3 h-3" />}
                        </button>
                      )}
                    </div>
                    <div className="col-span-2 flex items-center gap-2 pt-1">
                      <span style={{ color: 'var(--foreground-tertiary)' }}>思考模式</span>
                      {p.enable_thinking ? (
                        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium" style={{ background: 'rgba(0, 122, 255, 0.08)', color: 'var(--brand-500)' }}>
                          <Sparkles className="w-3 h-3" /> 已启用
                        </span>
                      ) : (
                        <span className="text-[11px]" style={{ color: 'var(--foreground-tertiary)' }}>未启用</span>
                      )}
                      {p.enable_thinking && p.thinking_params && (
                        <span className="text-[10px] truncate ml-2" style={{ color: 'var(--foreground-tertiary)' }} title={p.thinking_params}>
                          自定义参数已配置
                        </span>
                      )}
                    </div>
                  </div>
                  {testResults[p.provider] && (
                    <div className="mt-3 p-3 text-xs leading-relaxed whitespace-pre-line border" style={testResults[p.provider].success
                      ? { background: 'var(--state-success-surface)', color: 'var(--state-success)', borderColor: 'var(--state-success)', borderRadius: 'calc(var(--radius) * 0.75)' }
                      : { background: 'var(--state-error-surface)', color: 'var(--state-error)', borderColor: 'var(--state-error)', borderRadius: 'calc(var(--radius) * 0.75)' }}>
                      <div className="flex items-start gap-2">
                        {testResults[p.provider].success ? (
                          <CheckCircle2 className="w-4 h-4 mt-0.5 flex-shrink-0" style={{ color: 'var(--state-success)' }} />
                        ) : (
                          <XCircle className="w-4 h-4 mt-0.5 flex-shrink-0" style={{ color: 'var(--state-error)' }} />
                        )}
                        <div>
                          <p className="font-semibold mb-0.5">
                            {testResults[p.provider].success ? '连接成功' : '连接失败'}
                          </p>
                          <p className="opacity-90">{testResults[p.provider].message}</p>
                        </div>
                      </div>
                    </div>
                  )}
                  <div className="flex gap-2 mt-3">
                    <button onClick={() => handleTest(p.provider)} disabled={testing === p.provider || !p.api_key} className="btn-secondary text-xs py-1.5">
                      {testing === p.provider ? <Loader2 className="w-3 h-3 animate-spin" /> : <RefreshCw className="w-3 h-3" />}
                      测试连接
                    </button>
                  </div>
                </div>
              ))
            )}
            <button onClick={() => setEditing(true)} className="btn-secondary">
              <Key className="w-4 h-4" /> {hasAnyKey ? '修改配置' : '添加配置'}
            </button>
          </div>
        )}

        {/* 编辑模式 */}
        {editing && (
          <div className="space-y-6">
            {providers.map((p, idx) => (
              <div key={idx} className="p-4 border" style={p.is_primary
                ? { borderColor: 'rgba(0, 122, 255, 0.2)', background: 'rgba(0, 122, 255, 0.04)', borderRadius: 'calc(var(--radius) * 0.75)' }
                : { borderColor: 'var(--border)', background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.75)' }}>
                {/* 表头 */}
                <div className="flex items-center justify-between mb-3">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium" style={{ color: 'var(--foreground-secondary)' }}># {idx + 1}</span>
                    {p.is_primary && <span className="badge text-[10px]" style={{ background: 'rgba(0, 122, 255, 0.12)', color: 'var(--brand-500)' }}>主供应商</span>}
                  </div>
                  <div className="flex items-center gap-1">
                    {!p.is_primary && (
                      <button onClick={() => setPrimary(idx)} className="p-1" title="设为主供应商" style={{ color: 'var(--state-warning)' }}>
                        <Star className="w-4 h-4" />
                      </button>
                    )}
                    {providers.length > 1 && (
                      <button onClick={() => removeProvider(idx)} className="p-1" title="删除" style={{ color: 'var(--state-error)' }}>
                        <Trash2 className="w-4 h-4" />
                      </button>
                    )}
                  </div>
                </div>

                {/* 内置供应商选择（快捷入口） */}
                {config?.builtin_providers && (
                  <div className="mb-3">
                    <label className="label text-xs mb-1" style={{ color: 'var(--foreground-tertiary)' }}>选择内置供应商预设（自动填充）</label>
                    <div className="flex flex-wrap gap-1.5">
                      {Object.entries(config.builtin_providers).map(([key, preset]) => (
                        <button
                          key={key}
                          onClick={() => applyPreset(idx, key)}
                          className="text-xs px-2.5 py-1 border transition-colors"
                          style={p.provider === key
                            ? { background: 'rgba(0, 122, 255, 0.12)', borderColor: 'rgba(0, 122, 255, 0.3)', color: 'var(--brand-500)', borderRadius: 'calc(var(--radius) * 0.5)' }
                            : { background: 'var(--background)', borderColor: 'var(--border)', color: 'var(--foreground-tertiary)', borderRadius: 'calc(var(--radius) * 0.5)' }}
                        >
                          {preset.label}
                        </button>
                      ))}
                      <button
                        onClick={() => { updateField(idx, 'provider', 'custom'); updateField(idx, 'provider_label', '自定义'); }}
                        className="text-xs px-2.5 py-1 border transition-colors"
                        style={(p.provider === 'custom' || (!config.builtin_providers[p.provider]))
                          ? { background: 'var(--background-muted)', borderColor: 'var(--input)', color: 'var(--foreground-secondary)', borderRadius: 'calc(var(--radius) * 0.5)' }
                          : { background: 'var(--background)', borderColor: 'var(--border)', color: 'var(--foreground-tertiary)', borderRadius: 'calc(var(--radius) * 0.5)' }}
                      >
                        自定义
                      </button>
                    </div>
                  </div>
                )}

                {/* 表单 */}
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="label">供应商标识</label>
                    <input value={p.provider} onChange={e => updateField(idx, 'provider', e.target.value)}
                      className="input-field text-sm" placeholder="例如：deepseek / my-custom-llm" />
                  </div>
                  <div>
                    <label className="label">显示名称</label>
                    <input value={p.provider_label} onChange={e => updateField(idx, 'provider_label', e.target.value)}
                      className="input-field text-sm" placeholder="例如：DeepSeek V4" />
                  </div>
                  <div className="col-span-2">
                    <label className="label">API 基础地址</label>
                    <input value={p.base_url} onChange={e => updateField(idx, 'base_url', e.target.value)}
                      className="input-field text-sm font-mono" placeholder="https://api.example.com/v1" />
                  </div>
                  <div>
                    <label className="label">模型名称</label>
                    <input value={p.model} onChange={e => updateField(idx, 'model', e.target.value)}
                      className="input-field text-sm" placeholder="deepseek-chat / gpt-4o" />
                  </div>
                  <div>
                    <label className="label">API Key</label>
                    <div className="relative">
                      <input type={showKeys[p.provider] ? 'text' : 'password'}
                        value={p.api_key} onChange={e => updateField(idx, 'api_key', e.target.value)}
                        className="input-field pr-10 text-sm font-mono" placeholder="sk-..." />
                      <button onClick={() => setShowKeys(prev => ({ ...prev, [p.provider]: !prev[p.provider] }))}
                        className="absolute right-3 top-1/2 -translate-y-1/2 hover:text-[var(--foreground-secondary)]" style={{ color: 'var(--foreground-tertiary)' }}>
                        {showKeys[p.provider] ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                      </button>
                    </div>
                  </div>
                </div>

                {/* v2.36: 思考模式开关 */}
                <div className="mt-3 p-3 border" style={{ background: 'var(--background-muted)', borderColor: 'var(--border)', borderRadius: 'calc(var(--radius) * 0.5)' }}>
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <Sparkles className="w-4 h-4" style={{ color: 'var(--brand-500)' }} />
                      <div>
                        <label className="text-sm font-medium" style={{ color: 'var(--foreground-secondary)' }}>思考模式</label>
                        <p className="text-xs mt-0.5" style={{ color: 'var(--foreground-tertiary)' }}>
                          启用深度推理（仅对支持思考的模型生效，如 Hy3 / GLM-4.5 / GLM-5 / Qwen3 / DeepSeek-R1 / Claude 3.5+）
                        </p>
                      </div>
                    </div>
                    <button
                      type="button"
                      onClick={() => updateField(idx, 'enable_thinking', !p.enable_thinking)}
                      className="relative inline-flex h-6 w-11 items-center rounded-full transition-colors"
                      style={{ background: p.enable_thinking ? 'var(--brand-500)' : 'var(--input)' }}
                      aria-pressed={p.enable_thinking}
                    >
                      <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${p.enable_thinking ? 'translate-x-6' : 'translate-x-1'}`} />
                    </button>
                  </div>

                  {/* v2.37: 自定义思考参数（仅思考模式开启时显示） */}
                  {p.enable_thinking && (
                    <div className="mt-3 pt-3 border-t" style={{ borderColor: 'var(--border)' }}>
                      <label className="text-xs font-medium flex items-center gap-1" style={{ color: 'var(--foreground-secondary)' }}>
                        <Code className="w-3 h-3" />
                        自定义思考参数
                        <span className="font-normal" style={{ color: 'var(--foreground-tertiary)' }}>（可选，留空则使用内置规则）</span>
                      </label>
                      <textarea
                        value={p.thinking_params || ''}
                        onChange={e => updateField(idx, 'thinking_params', e.target.value)}
                        className="input-field mt-1.5 text-xs font-mono"
                        rows={3}
                        placeholder='{"thinking": {"type": "enabled"}} 或 {"enable_thinking": true}'
                      />
                      <div className="mt-1.5 flex items-start gap-1.5">
                        <AlertCircle className="w-3 h-3 mt-0.5 flex-shrink-0" style={{ color: 'var(--foreground-tertiary)' }} />
                        <div className="text-[11px] leading-relaxed" style={{ color: 'var(--foreground-tertiary)' }}>
                          <p>JSON 格式，优先级高于内置规则。常见示例：</p>
                          <p>· 腾讯混元/GLM/DeepSeek-V4：<code style={{ color: 'var(--brand-500)' }}>{'{"thinking":{"type":"enabled"}}'}</code></p>
                          <p>· Qwen3：<code style={{ color: 'var(--brand-500)' }}>{'{"enable_thinking":true}'}</code></p>
                          <p>· Claude：<code style={{ color: 'var(--brand-500)' }}>{'{"thinking":{"type":"enabled","budget_tokens":10000}}'}</code></p>
                          <p>· OpenAI o 系列：<code style={{ color: 'var(--brand-500)' }}>{'{"reasoning_effort":"high"}'}</code></p>
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              </div>
            ))}

            {/* 添加供应商 */}
            <button onClick={addProvider} className="btn-secondary w-full border-dashed border-2 py-3 hover:text-[var(--brand-500)]" style={{ color: 'var(--foreground-tertiary)' }}>
              <Plus className="w-4 h-4" /> 添加供应商
            </button>

            {/* 操作按钮 */}
            <div className="flex items-center gap-3 pt-2">
              <button onClick={handleSave} disabled={saving} className="btn-primary">
                {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <CheckCircle2 className="w-4 h-4" />}
                保存全部配置
              </button>
              <button onClick={() => { setEditing(false); loadSettings() }} className="btn-secondary">取消</button>
            </div>

            <p className="text-xs mt-2" style={{ color: 'var(--foreground-tertiary)' }}>
              💡 API Key 使用 Fernet 加密后存储，前端展示已脱敏。首次保存后 Key 不可见，如需修改请重新输入。
            </p>
          </div>
        )}
      </div>

      {/* 修改密码 */}
      <div className="card space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="section-title flex items-center gap-2">
            <Shield className="w-4 h-4" style={{ color: 'var(--foreground-tertiary)' }} /> 账户安全
          </h2>
          {!passwordFormOpen && (
            <button onClick={() => { setPasswordFormOpen(true); setPasswordMsg(null); setPasswordForm({ current: '', new: '', confirm: '' }) }} className="btn-secondary text-xs py-1.5">
              <Lock className="w-3.5 h-3.5" /> 修改密码
            </button>
          )}
        </div>

        {passwordFormOpen && (
          <div className="space-y-4">
            {/* 当前密码 */}
            <div>
              <label className="label">当前密码</label>
              <div className="relative">
                <input type={showCurrentPwd ? 'text' : 'password'}
                  value={passwordForm.current}
                  onChange={e => setPasswordForm(prev => ({ ...prev, current: e.target.value }))}
                  className="input-field pr-10 text-sm" placeholder="输入当前密码" />
                <button onClick={() => setShowCurrentPwd(!showCurrentPwd)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 hover:text-[var(--foreground-secondary)]" style={{ color: 'var(--foreground-tertiary)' }}>
                  {showCurrentPwd ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                </button>
              </div>
            </div>

            {/* 新密码 */}
            <div>
              <label className="label">新密码</label>
              <div className="relative">
                <input type={showNewPwd ? 'text' : 'password'}
                  value={passwordForm.new}
                  onChange={e => setPasswordForm(prev => ({ ...prev, new: e.target.value }))}
                  className="input-field pr-10 text-sm" placeholder="输入新密码（至少 4 位）" />
                <button onClick={() => setShowNewPwd(!showNewPwd)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 hover:text-[var(--foreground-secondary)]" style={{ color: 'var(--foreground-tertiary)' }}>
                  {showNewPwd ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                </button>
              </div>
            </div>

            {/* 确认新密码 */}
            <div>
              <label className="label">确认新密码</label>
              <input type="password"
                value={passwordForm.confirm}
                onChange={e => setPasswordForm(prev => ({ ...prev, confirm: e.target.value }))}
                className="input-field text-sm" placeholder="再次输入新密码" />
            </div>

            {/* 反馈消息 */}
            {passwordMsg && (
              <div className="p-3 text-sm flex items-start gap-2 border" style={passwordMsg.ok
                ? { background: 'var(--state-success-surface)', color: 'var(--state-success)', borderColor: 'var(--state-success)', borderRadius: 'calc(var(--radius) * 0.75)' }
                : { background: 'var(--state-error-surface)', color: 'var(--state-error)', borderColor: 'var(--state-error)', borderRadius: 'calc(var(--radius) * 0.75)' }}>
                {passwordMsg.ok ? (
                  <CheckCircle2 className="w-4 h-4 mt-0.5 flex-shrink-0" style={{ color: 'var(--state-success)' }} />
                ) : (
                  <XCircle className="w-4 h-4 mt-0.5 flex-shrink-0" style={{ color: 'var(--state-error)' }} />
                )}
                <span>{passwordMsg.text}</span>
              </div>
            )}

            {/* 操作按钮 */}
            <div className="flex items-center gap-3 pt-1">
              <button onClick={handleChangePassword} disabled={changingPassword} className="btn-primary text-sm">
                {changingPassword ? <Loader2 className="w-4 h-4 animate-spin" /> : <CheckCircle2 className="w-4 h-4" />}
                确认修改
              </button>
              <button onClick={() => { setPasswordFormOpen(false); setPasswordMsg(null) }} className="btn-secondary text-sm">取消</button>
            </div>
          </div>
        )}
      </div>

      {/* KB 专属 LLM 配置 */}
      <div className="card">
        <h2 className="section-title flex items-center gap-2 mb-4">
          <Database className="w-4 h-4" style={{ color: 'var(--brand-500)' }} /> 知识库 LLM
        </h2>
        <p className="text-xs mb-4" style={{ color: 'var(--foreground-tertiary)' }}>
          知识库问答（/ask）和需求澄清（ClarifyAgent）使用独立 LLM，可选指定供应商优化成本与性能。
        </p>
        {kbLlmProviders.length === 0 ? (
          <p className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>暂无可用 LLM 供应商，请先在「LLM 供应商」中配置 API Key。</p>
        ) : (
          <div className="flex items-center gap-3">
            <span className="text-sm" style={{ color: 'var(--foreground-secondary)' }}>当前：</span>
            <select
              value={kbLlmProvider}
              onChange={e => {
                const v = e.target.value
                setKbLlmLoading(true)
                knowledge.setKbLlm(v).then(() => {
                  setKbLlmProvider(v)
                }).catch(() => {}).finally(() => setKbLlmLoading(false))
              }}
              disabled={kbLlmLoading}
              className="select-field text-sm max-w-[240px]"
            >
              <option value="">跟随全局（默认主供应商）</option>
              {kbLlmProviders.map(p => (
                <option key={p.provider} value={p.provider}>
                  {p.provider_label} ({p.model})
                </option>
              ))}
            </select>
            {kbLlmLoading && <Loader2 className="w-4 h-4 animate-spin" style={{ color: 'var(--foreground-tertiary)' }} />}
          </div>
        )}
      </div>

      {/* 多模态 LLM 配置（v2.18: 图片/流程图识别专用） */}
      <div className="card">
        <h2 className="section-title flex items-center gap-2 mb-4">
          <Image className="w-4 h-4" style={{ color: '#af52de' }} /> 多模态 LLM
        </h2>
        <p className="text-xs mb-4" style={{ color: 'var(--foreground-tertiary)' }}>
          需求文档中的图片、流程图、架构图等多模态内容识别专用 LLM。文字解析仍使用主 LLM，图片内容交给多模态模型。多模态模型也可作为主模型使用。
        </p>
        {visionLlmProviders.length === 0 ? (
          <p className="text-xs" style={{ color: 'var(--foreground-tertiary)' }}>暂无可用 LLM 供应商，请先在「LLM 供应商」中配置 API Key。</p>
        ) : (
          <div className="flex items-center gap-3">
            <span className="text-sm" style={{ color: 'var(--foreground-secondary)' }}>当前：</span>
            <select
              value={visionLlmProvider}
              onChange={e => {
                const v = e.target.value
                setVisionLlmLoading(true)
                knowledge.setVisionLlm(v).then(() => {
                  setVisionLlmProvider(v)
                }).catch(() => {}).finally(() => setVisionLlmLoading(false))
              }}
              disabled={visionLlmLoading}
              className="select-field text-sm max-w-[240px]"
            >
              <option value="">跟随全局（默认主供应商）</option>
              {visionLlmProviders.map(p => (
                <option key={p.provider} value={p.provider}>
                  {p.provider_label} ({p.model})
                </option>
              ))}
            </select>
            {visionLlmLoading && <Loader2 className="w-4 h-4 animate-spin" style={{ color: 'var(--foreground-tertiary)' }} />}
          </div>
        )}
      </div>

      {/* 系统信息 */}
      <div className="card">
        <h2 className="section-title flex items-center gap-2 mb-4">
          <Server className="w-4 h-4" style={{ color: 'var(--foreground-tertiary)' }} /> 系统信息
        </h2>
        <div className="grid grid-cols-2 gap-4">
          {[
            ['版本', healthStatus?.version ? `v${healthStatus.version}` : '-'],
            ['主供应商', healthStatus?.llm_provider || '-'],
            ['LLM 状态', healthStatus?.llm_configured ? '已配置' : '未配置'],
            ['知识库模式', healthStatus?.cold_start_mode ? '冷启动' : '正常'],
          ].map(([label, value]) => (
            <div key={label} className="flex items-center justify-between p-3" style={{ background: 'var(--background-muted)', borderRadius: 'calc(var(--radius) * 0.75)' }}>
              <span className="text-sm" style={{ color: 'var(--foreground-tertiary)' }}>{label}</span>
              <span className="text-sm font-medium" style={{ color: 'var(--foreground-secondary)' }}>{value}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

// 脱敏辅助
function _maskKey(key: string): string {
  if (!key || key.length < 8) return key ? '***' : ''
  if (key.includes('***')) return key
  return key.slice(0, 4) + '…' + key.slice(-4)
}
