/**
 * 用户手册截图脚本
 * 启动本地前端 dev server (127.0.0.1:5173) 后运行：
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
const OUT = path.resolve(__dirname, '../../frontend/public/manual-imgs')
const PRODUCT_ID = 'PROD-04406-853'
const PRODUCT_NAME = '测试'

// 关键需求 ID（来自线上数据，用于演示详情/澄清/生成/功能点）
const REQ_ARCHIVED_V2 = 'REQ-1784163945376-3043' // 登录功能优化需求（V2.0）— 增量/基线补全演示
const REQ_DRAFT = 'REQ-1784106222080-0218'          // 用户管理（草稿，待澄清）
const REQ_ACTIVE = 'REQ-1784165775204-4624'          // 登录（已就绪，可生成）

fs.mkdirSync(OUT, { recursive: true })

async function shot(page, route, name, opts = {}) {
  try {
    await page.goto(BASE + route, { waitUntil: 'domcontentloaded', timeout: 30000 })
    await page.waitForTimeout(opts.wait || 1500)
    await page.screenshot({ path: path.join(OUT, name + '.png'), fullPage: !!opts.full })
    console.log('  OK  ', name)
  } catch (e) {
    console.log('  FAIL', name, '->', e.message.split('\n')[0])
  }
}

;(async () => {
  // 关键：Chromium 不能走系统/OS 代理去连本地 dev server，--no-proxy-server 强制直连
  const browser = await chromium.launch({
    args: ['--no-proxy-server', '--disable-dev-shm-usage'],
  })
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 })
  const page = await ctx.newPage()
  page.on('pageerror', () => {})

  // 1) 登录页
  await page.goto(BASE + '/login', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(800)
  await page.screenshot({ path: path.join(OUT, 'login.png'), fullPage: true })
  console.log('[1] login captured')

  // 2) 执行登录
  await page.fill('input[placeholder="请输入用户名"]', 'admin')
  await page.fill('input[placeholder="请输入密码"]', '888888')
  await page.click('button:has-text("登录")')
  await page.waitForURL('**/admin**', { timeout: 15000 })
  await page.waitForTimeout(1000)

  // 选择产品（直接写 localStorage，避免 UI 点击不确定性）
  await page.evaluate(([id, name]) => {
    localStorage.setItem('aitestgen_selected_product', id)
    localStorage.setItem('aitestgen_selected_product_name', name)
  }, [PRODUCT_ID, PRODUCT_NAME])
  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(1200)

  // 3) 仪表盘
  await shot(page, '/dashboard', 'dashboard')

  // 4) 需求列表
  await shot(page, '/requirements', 'requirements-list')

  // 5) 上传需求弹窗
  try {
    await page.goto(BASE + '/requirements', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(800)
    await page.click('button:has-text("上传需求")')
    await page.waitForTimeout(700)
    await page.screenshot({ path: path.join(OUT, 'requirements-upload.png'), fullPage: true })
    console.log('  OK   requirements-upload')
    await page.keyboard.press('Escape')
    await page.waitForTimeout(300)
  } catch (e) {
    console.log('  FAIL requirements-upload ->', e.message.split('\n')[0])
  }

  // 6) 需求详情（已归档 V2 优化需求）
  await shot(page, '/requirements/' + REQ_ARCHIVED_V2, 'requirement-detail')

  // 7) 功能点 Tab
  try {
    await page.click('button:has-text("功能点")')
    await page.waitForTimeout(900)
    await page.screenshot({ path: path.join(OUT, 'requirement-fp.png'), fullPage: true })
    console.log('  OK   requirement-fp')
  } catch (e) {
    console.log('  FAIL requirement-fp ->', e.message.split('\n')[0])
  }

  // 8) 澄清页（草稿需求）
  await shot(page, '/requirements/' + REQ_DRAFT + '/clarify', 'requirement-clarify')

  // 9) 用例生成页（已就绪需求）
  await shot(page, '/requirements/' + REQ_ACTIVE + '/generate', 'generate')

  // 10) 测试用例管理
  await shot(page, '/testcases', 'testcases')

  // 11) 知识库
  await shot(page, '/knowledge', 'knowledge')

  // 12) 知识问答
  await shot(page, '/knowledge-qa', 'knowledge-qa')

  // 13) 项目与模块
  await shot(page, '/projects-modules', 'projects-modules')

  // 14) 依赖图谱（含功能点依赖）
  await shot(page, '/dependency-graph', 'dependency-graph')

  // 15) 管理后台首页
  await shot(page, '/admin', 'admin-dashboard')

  // 16) 产品管理
  await shot(page, '/admin/products', 'admin-products')

  // 17) 用户管理
  await shot(page, '/admin/users', 'admin-users')

  // 18) 角色管理
  await shot(page, '/admin/roles', 'admin-roles')

  // 19) 系统设置（LLM 配置）
  await shot(page, '/admin/settings', 'admin-settings')

  // 20) 提示词管理
  await shot(page, '/admin/prompts', 'admin-prompts')

  // 21) 知识库诊断
  await shot(page, '/admin/knowledge', 'admin-kb')

  // 22) 任务管理
  await shot(page, '/tasks', 'tasks')

  await browser.close()
  console.log('\nALL_DONE ->', OUT)
})().catch(e => { console.error('FATAL', e); process.exit(1) })
