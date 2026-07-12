import { useState, useEffect } from 'react'
import { Settings as SettingsIcon, Key, Server, RefreshCw, CheckCircle2, XCircle, Loader2, Eye, EyeOff, AlertCircle, Plus, Trash2, Star, Lock, Shield } from 'lucide-react'
import { settings as settingsApi, health, auth } from '../api/client'
import type { LLMConfig, LLMProviderItem, HealthStatus } from '../types'

const DEFAULT_PROVIDER: LLMProviderItem = {
  id: 0,
  provider: 'deepseek',
  provider_label: 'DeepSeek',
  api_key: '',
  base_url: 'https://api.deepseek.com/v1',
  model: 'deepseek-chat',
  is_primary: false,
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
        <p className="text-sm text-slate-500 mt-1">LLM 供应商配置与系统信息</p>
      </div>

      {/* LLM 供应商配置 */}
      <div className="card space-y-5">
        <div className="flex items-center justify-between">
          <h2 className="section-title flex items-center gap-2">
            <Key className="w-4 h-4 text-primary-500" /> LLM 供应商
          </h2>
          <div className="flex items-center gap-2">
            {!editing && hasAnyKey && (
              <span className="badge bg-success-50 text-success-600 flex items-center gap-1">
                <CheckCircle2 className="w-3 h-3" /> 已配置
              </span>
            )}
            <span className="text-xs text-slate-400">{providers.length} 个</span>
          </div>
        </div>

        {/* 查看模式 */}
        {!editing && (
          <div className="space-y-3">
            {providers.length === 0 ? (
              <div className="bg-amber-50 border border-amber-100 rounded-xl p-4 flex items-start gap-3">
                <AlertCircle className="w-5 h-5 text-amber-500 flex-shrink-0 mt-0.5" />
                <div>
                  <p className="text-sm font-medium text-amber-800">LLM 未配置</p>
                  <p className="text-xs text-amber-600/80 mt-0.5">请配置 API Key 后，系统才能调用大语言模型解析需求和生成测试用例。</p>
                </div>
              </div>
            ) : (
              providers.map((p, idx) => (
                <div key={idx} className={`p-4 rounded-xl ${p.is_primary ? 'bg-primary-50/50 border border-primary-100' : 'bg-slate-50/50 border border-slate-100'}`}>
                  <div className="flex items-center justify-between mb-3">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-semibold text-slate-700">{p.provider_label || p.provider}</span>
                      {p.is_primary && <span className="badge bg-primary-100 text-primary-700 text-[10px]">主供应商</span>}
                    </div>
                  </div>
                  <div className="grid grid-cols-2 gap-3 text-xs">
                    <div><span className="text-slate-400">标识</span><br /><span className="text-slate-600 font-medium">{p.provider}</span></div>
                    <div><span className="text-slate-400">模型</span><br /><span className="text-slate-600 font-medium">{p.model || '-'}</span></div>
                    <div className="col-span-2"><span className="text-slate-400">API 地址</span><br /><span className="text-slate-600 font-medium truncate block">{p.base_url || '-'}</span></div>
                    <div className="col-span-2">
                      <span className="text-slate-400">API Key</span><br />
                      <span className="text-slate-600 font-medium font-mono text-[11px]">
                        {p.api_key ? (showKeys[p.provider] ? p.api_key : _maskKey(p.api_key)) : '未配置'}
                      </span>
                      {p.api_key && (
                        <button onClick={() => setShowKeys(prev => ({ ...prev, [p.provider]: !prev[p.provider] }))} className="ml-2 text-slate-400 hover:text-slate-600 inline-flex">
                          {showKeys[p.provider] ? <EyeOff className="w-3 h-3" /> : <Eye className="w-3 h-3" />}
                        </button>
                      )}
                    </div>
                  </div>
                  {testResults[p.provider] && (
                    <div className={`mt-3 p-3 rounded-xl text-xs leading-relaxed whitespace-pre-line ${
                      testResults[p.provider].success
                        ? 'bg-success-50 text-success-700 border border-success-200'
                        : 'bg-red-50 text-red-700 border border-red-200'
                    }`}>
                      <div className="flex items-start gap-2">
                        {testResults[p.provider].success ? (
                          <CheckCircle2 className="w-4 h-4 mt-0.5 flex-shrink-0 text-success-500" />
                        ) : (
                          <XCircle className="w-4 h-4 mt-0.5 flex-shrink-0 text-red-500" />
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
              <div key={idx} className={`p-4 rounded-xl border ${p.is_primary ? 'border-primary-200 bg-primary-50/30' : 'border-slate-200 bg-slate-50/30'}`}>
                {/* 表头 */}
                <div className="flex items-center justify-between mb-3">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium text-slate-600"># {idx + 1}</span>
                    {p.is_primary && <span className="badge bg-primary-100 text-primary-700 text-[10px]">主供应商</span>}
                  </div>
                  <div className="flex items-center gap-1">
                    {!p.is_primary && (
                      <button onClick={() => setPrimary(idx)} className="text-amber-500 hover:text-amber-600 p-1" title="设为主供应商">
                        <Star className="w-4 h-4" />
                      </button>
                    )}
                    {providers.length > 1 && (
                      <button onClick={() => removeProvider(idx)} className="text-red-400 hover:text-red-600 p-1" title="删除">
                        <Trash2 className="w-4 h-4" />
                      </button>
                    )}
                  </div>
                </div>

                {/* 内置供应商选择（快捷入口） */}
                {config?.builtin_providers && (
                  <div className="mb-3">
                    <label className="label text-xs text-slate-400 mb-1">选择内置供应商预设（自动填充）</label>
                    <div className="flex flex-wrap gap-1.5">
                      {Object.entries(config.builtin_providers).map(([key, preset]) => (
                        <button
                          key={key}
                          onClick={() => applyPreset(idx, key)}
                          className={`text-xs px-2.5 py-1 rounded-lg border transition-colors ${
                            p.provider === key
                              ? 'bg-primary-100 border-primary-300 text-primary-700'
                              : 'bg-white border-slate-200 text-slate-500 hover:border-primary-200 hover:text-primary-600'
                          }`}
                        >
                          {preset.label}
                        </button>
                      ))}
                      <button
                        onClick={() => { updateField(idx, 'provider', 'custom'); updateField(idx, 'provider_label', '自定义'); }}
                        className={`text-xs px-2.5 py-1 rounded-lg border transition-colors ${
                          p.provider === 'custom' || (!config.builtin_providers[p.provider])
                            ? 'bg-slate-200 border-slate-300 text-slate-700'
                            : 'bg-white border-slate-200 text-slate-500 hover:border-slate-300'
                        }`}
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
                        className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600">
                        {showKeys[p.provider] ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            ))}

            {/* 添加供应商 */}
            <button onClick={addProvider} className="btn-secondary w-full border-dashed border-2 py-3 text-slate-400 hover:text-primary-600 hover:border-primary-300">
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

            <p className="text-xs text-slate-400 mt-2">
              💡 API Key 使用 Fernet 加密后存储，前端展示已脱敏。首次保存后 Key 不可见，如需修改请重新输入。
            </p>
          </div>
        )}
      </div>

      {/* 修改密码 */}
      <div className="card space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="section-title flex items-center gap-2">
            <Shield className="w-4 h-4 text-slate-500" /> 账户安全
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
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600">
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
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600">
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
              <div className={`p-3 rounded-xl text-sm flex items-start gap-2 ${
                passwordMsg.ok
                  ? 'bg-success-50 text-success-700 border border-success-200'
                  : 'bg-red-50 text-red-700 border border-red-200'
              }`}>
                {passwordMsg.ok ? (
                  <CheckCircle2 className="w-4 h-4 mt-0.5 flex-shrink-0 text-success-500" />
                ) : (
                  <XCircle className="w-4 h-4 mt-0.5 flex-shrink-0 text-red-500" />
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

      {/* 系统信息 */}
      <div className="card">
        <h2 className="section-title flex items-center gap-2 mb-4">
          <Server className="w-4 h-4 text-slate-500" /> 系统信息
        </h2>
        <div className="grid grid-cols-2 gap-4">
          {[
            ['版本', healthStatus?.version ? `v${healthStatus.version}` : '-'],
            ['主供应商', healthStatus?.llm_provider || '-'],
            ['LLM 状态', healthStatus?.llm_configured ? '已配置' : '未配置'],
            ['知识库模式', healthStatus?.cold_start_mode ? '冷启动' : '正常'],
          ].map(([label, value]) => (
            <div key={label} className="flex items-center justify-between p-3 bg-slate-50/50 rounded-xl">
              <span className="text-sm text-slate-500">{label}</span>
              <span className="text-sm font-medium text-slate-700">{value}</span>
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
