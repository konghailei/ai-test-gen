/**
 * 用例变更的跨标签页通知。
 *
 * XMind 编辑页通常在独立标签页打开，保存后同一浏览器里的用例列表页不会自动刷新，
 * 用户切回去看到的还是旧数据。这里用 localStorage + storage 事件做轻量广播
 * （storage 事件只在「其他」标签页触发，正好符合需求）。
 */

const CASES_CHANGED_KEY = 'aitestgen:cases-changed'

/** 广播：用例已变更，请刷新 */
export function notifyCasesChanged(): void {
  try {
    localStorage.setItem(CASES_CHANGED_KEY, String(Date.now()))
  } catch {
    /* 隐私模式 / 存储不可用时静默降级，不影响主流程 */
  }
}

/** 订阅：返回取消订阅函数 */
export function subscribeCasesChanged(handler: () => void): () => void {
  const onStorage = (e: StorageEvent) => {
    if (e.key === CASES_CHANGED_KEY) handler()
  }
  window.addEventListener('storage', onStorage)
  return () => window.removeEventListener('storage', onStorage)
}
