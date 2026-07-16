/**
 * 用户手册截图脚本
 * 启动本地前端 dev server (127.0.0.1:5173) 与后端 (localhost:8000) 后运行：
 *   node shoot.js
 * 截图输出到 frontend/public/manual-imgs/
 */
const { chromium } = require('playwright')
const fs = require('fs')
const path = require('path')

// 关键：清除可能继承的代理环境变量，避免 Chromium 走代理去连本地 dev server
for (const k of ['HTTP_PROXY', 'http_proxy', 'HTTPS_PROXY', 'https_proxy', 'NO_PROXY', 'no_proxy', 'ALL_PROXY', 'all_proxy']) {
  delete process.env[k]
}

const BASE = 'http://127.0.0.1:5173'
const API = 'http://localhost:8000'
const OUT = path.resolve(__dirname, '../../frontend/public/manual-imgs')

const PRODUCT_ID = 'PROD-04406-853'
const PRODUCT_NAME = '测试'
const USER = { username: 'admin', password: '888888' }

// 关键需求 ID（来自线上数据，用于演示详情/澄清/生成/功能点）
const REQ_ARCHIVED_V2 = 'REQ-1784163945376-3043' // 登录功能优化需求（V2.0）— 增量/基线补全演示
const REQ_DRAFT = 'REQ-1784106222080-0218'          // 用户管理（草稿，待澄清）
const REQ_ACTIVE = 'REQ-1784165775204-4624'          // 登录（已就绪，可生成）

fs.mkdirSync(OUT, { recursive: true })

async function apiLogin() {
  const res = await fetch(`${API}/api/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(USER),
  })
  const json = await res.json()
  if (!json.success) throw new Error(json.message || 'login failed')
  console.log('[auth] API login ok, role:', json.data.user.role, 'permissions:', json.data.user.permissions.length)
  return json.data
}

async function shot(page, route, name, opts = {}) {
  try {
    console.log(`[shot] ${name} -> ${route}`)
    await page.goto(BASE + route, { waitUntil: 'commit', timeout: 120000 })
    await page.waitForTimeout(opts.wait || 4000)
    const url = page.url()
    if (url.includes('/login')) {
      console.warn(`  WARN ${name} still on login page (url=${url})`)
    }
    await page.screenshot({ path: path.join(OUT, name + '.png'), fullPage: !!opts.full })
    console.log('  OK  ', name)
  } catch (e) {
    console.log('  FAIL', name, '->', e.message.split('\n')[0])
  }
}

;(async () => {
  const { token, user } = await apiLogin()

  const browser = await chromium.launch({
    headless: true,
    args: ['--no-proxy-server', '--disable-dev-shm-usage'],
  })
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 })
  const page = await ctx.newPage()
  page.on('pageerror', (err) => console.log('[pageerror]', err.message))
  page.on('console', (msg) => { if (msg.type() === 'error') console.log('[console.error]', msg.text()) })

  // 1) 登录页截图（保留给手册使用）
  await page.goto(BASE + '/login', { waitUntil: 'domcontentloaded', timeout: 30000 })
  await page.waitForTimeout(1000)
  await page.screenshot({ path: path.join(OUT, 'login.png'), fullPage: true })
  console.log('[1] login captured')

  // 2) 注入认证态与产品选择（比 UI 登录更稳定，避免 SPA 跳转时序问题）
  await page.evaluate(([token, user, productId, productName]) => {
    localStorage.setItem('aitestgen_token', token)
    localStorage.setItem('aitestgen_user', JSON.stringify(user))
    localStorage.setItem('aitestgen_selected_product', productId)
    localStorage.setItem('aitestgen_selected_product_name', productName)
  }, [token, user, PRODUCT_ID, PRODUCT_NAME])
  console.log('[2] injected token, user, selected product')

  // 3) 进入管理后台首页（AuthProvider/AdminGuard 会读取 localStorage）
  await page.goto(BASE + '/admin', { waitUntil: 'commit', timeout: 120000 })
  await page.waitForTimeout(2000)
  console.log('[3] admin loaded:', page.url())

  // 4) 仪表盘
  await shot(page, '/dashboard', 'dashboard')

  // 5) 需求列表
  await shot(page, '/requirements', 'requirements-list')

  // 6) 上传需求弹窗
  try {
    await page.goto(BASE + '/requirements', { waitUntil: 'commit', timeout: 120000 })
    await page.waitForTimeout(1000)
    await page.click('button:has-text("上传需求")')
    await page.waitForTimeout(1000)
    await page.screenshot({ path: path.join(OUT, 'requirements-upload.png'), fullPage: true })
    console.log('  OK   requirements-upload')
    await page.keyboard.press('Escape')
    await page.waitForTimeout(300)
  } catch (e) {
    console.log('  FAIL requirements-upload ->', e.message.split('\n')[0])
  }

  // 7) 需求详情（已归档 V2 优化需求）
  await shot(page, '/requirements/' + REQ_ARCHIVED_V2, 'requirement-detail', { wait: 2500 })

  // 8) 功能点（FP）视图：srr Tab 默认即含「功能点（FP）审核」区块（FPSRRViewer），无需点击独立 Tab
  try {
    await page.waitForTimeout(2000)
    await page.screenshot({ path: path.join(OUT, 'requirement-fp.png'), fullPage: true })
    console.log('  OK   requirement-fp')
  } catch (e) {
    console.log('  FAIL requirement-fp ->', e.message.split('\n')[0])
  }

  // 9) 澄清页（草稿需求）
  await shot(page, '/requirements/' + REQ_DRAFT + '/clarify', 'requirement-clarify', { wait: 2500 })

  // 10) 用例生成页（已就绪需求）
  await shot(page, '/requirements/' + REQ_ACTIVE + '/generate', 'generate', { wait: 3000 })

  // 11) 测试用例管理
  await shot(page, '/testcases', 'testcases', { wait: 3000 })

  // 12) 知识库
  await shot(page, '/knowledge', 'knowledge', { wait: 3000 })

  // 13) 知识问答
  await shot(page, '/knowledge-qa', 'knowledge-qa', { wait: 3000 })

  // 14) 项目与模块
  await shot(page, '/projects-modules', 'projects-modules', { wait: 3000 })

  // 15) 依赖图谱（含功能点依赖）
  await shot(page, '/dependency-graph', 'dependency-graph', { wait: 3000 })

  // 16) 管理后台首页
  await shot(page, '/admin', 'admin-dashboard', { wait: 2500 })

  // 17) 产品管理
  await shot(page, '/admin/products', 'admin-products', { wait: 2500 })

  // 18) 用户管理
  await shot(page, '/admin/users', 'admin-users', { wait: 2500 })

  // 19) 角色管理
  await shot(page, '/admin/roles', 'admin-roles', { wait: 2500 })

  // 20) 系统设置（LLM 配置）
  await shot(page, '/admin/settings', 'admin-settings', { wait: 2500 })

  // 21) 提示词管理
  await shot(page, '/admin/prompts', 'admin-prompts', { wait: 2500 })

  // 22) 知识库诊断
  await shot(page, '/admin/knowledge', 'admin-kb', { wait: 2500 })

  // 23) 任务管理
  await shot(page, '/tasks', 'tasks', { wait: 3000 })

  await browser.close()
  console.log('\nALL_DONE ->', OUT)
})().catch(e => { console.error('FATAL', e); process.exit(1) })
