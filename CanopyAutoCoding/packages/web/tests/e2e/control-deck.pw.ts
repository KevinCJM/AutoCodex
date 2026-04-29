import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'

const snapshotsPayload = {
  app: {
    project_dir: '/tmp/canopy-demo',
    requirement_name: '需求A',
    active_run_id: 'run-1',
    active_stage: 'stage.a07.start',
    active_stage_label: '任务开发',
  },
  stages: {
    development: {
      project_dir: '/tmp/canopy-demo',
      requirement_name: '需求A',
      files: [{ label: '任务单', path: '/tmp/task.md', exists: true, summary: 'M1 ready' }],
      workers: [{
        index: 1,
        session_name: '开发工程师-1',
        status: 'running',
        workflow_stage: 'development',
        agent_state: 'BUSY',
        health_status: 'alive',
        transcript_path: '/tmp/transcript.log',
      }],
      milestones: [{ key: 'M1', completed: false, tasks: [{ key: 'M1-T1', completed: false }] }],
      current_milestone_key: 'M1',
    },
  },
  control: {
    control_id: 'ctrl-1',
    run_id: 'run-1',
    workers: [{
      index: 1,
      session_name: '控制台-1',
      status: 'running',
      workflow_stage: 'control',
      agent_state: 'BUSY',
      health_status: 'alive',
      work_dir: '/tmp/canopy-demo',
    }],
  },
  hitl: { pending: false },
  artifacts: { items: [{ path: '/tmp/task.md', summary: 'M1 ready' }] },
  prompt: { pending: false },
}

const idleSnapshotsPayload = {
  ...snapshotsPayload,
  app: {
    project_dir: '',
    requirement_name: '',
    active_run_id: '',
    active_stage: 'idle',
    active_stage_label: '等待中',
  },
  stages: {},
  control: { control_id: '', run_id: '', workers: [] },
  artifacts: { items: [] },
}

function bootstrapPayloadFor(snapshotPayload: Record<string, unknown>) {
  return {
    ok: true,
    payload: {
      protocol_version: '1.0',
      python_path: '/usr/bin/python',
      routes: ['home', 'routing', 'requirements', 'review', 'design', 'task-split', 'development', 'overall-review', 'control'],
      commands: ['app.bootstrap'],
      capabilities: { structured_snapshots: true },
      snapshots: snapshotPayload,
    },
  }
}

async function installFakeBridge(page: Page) {
  await page.addInitScript(() => {
    class FakeEventSource {
      static instances: FakeEventSource[] = []
      listeners = new Map<string, Array<(event: MessageEvent<string>) => void>>()
      constructor(_url: string) {
        FakeEventSource.instances.push(this)
      }
      addEventListener(type: string, listener: (event: MessageEvent<string>) => void) {
        const list = this.listeners.get(type) ?? []
        list.push(listener)
        this.listeners.set(type, list)
      }
      close() {}
      emit(type: string, payload: Record<string, unknown>) {
        const event = { data: JSON.stringify({ type, payload }) } as MessageEvent<string>
        for (const listener of this.listeners.get(type) ?? []) listener(event)
      }
    }
    ;(window as any).FakeEventSource = FakeEventSource
    ;(window as any).EventSource = FakeEventSource
  })
}

async function mockBridgeApi(
  page: Page,
  promptPayload: () => Record<string, unknown> = () => ({ pending: false }),
  snapshotPayload: Record<string, unknown> = snapshotsPayload,
  onRequest?: (request: Record<string, unknown>) => void,
) {
  await page.route('**/api/bootstrap', (route) => route.fulfill({ json: bootstrapPayloadFor(snapshotPayload) }))
  await page.route('**/api/snapshots', (route) => route.fulfill({ json: { ok: true, payload: snapshotPayload } }))
  await page.route('**/api/prompt', (route) => route.fulfill({ json: { ok: true, payload: promptPayload() } }))
  await page.route('**/api/request', (route) => {
    onRequest?.(route.request().postDataJSON())
    return route.fulfill({ json: { ok: true, payload: { accepted: true } } })
  })
  await page.route('**/api/file-preview?*', (route) => route.fulfill({
    json: { ok: true, payload: { path: '/tmp/task.md', size: 8, updated_at: 'now', truncated: false, text: 'task doc' } },
  }))
}

async function emitPrompt(page: Page, payload: Record<string, unknown>) {
  await page.evaluate((nextPayload) => {
    const source = (window as any).FakeEventSource.instances[0]
    source.emit('prompt.request', nextPayload)
  }, payload)
}

async function assertNoCommandUi(page: Page) {
  await expect(page.getByRole('heading', { name: 'Command' })).toHaveCount(0)
  await expect(page.getByText('workflow.a00.start')).toHaveCount(0)
  await expect(page.getByText('argv')).toHaveCount(0)
  await expect(page.getByText('JSON')).toHaveCount(0)
  await expect(page.locator('.advanced-panel textarea')).toHaveCount(0)
}

function promptChoice(page: Page, name: string) {
  return page.locator('.prompt-card .choice-list').getByRole('radio', { name })
}

test('renders the app-like shell and keeps command controls out of the UI', async ({ page }) => {
  await installFakeBridge(page)
  await mockBridgeApi(page)

  await page.goto('/')
  await expect(page).toHaveTitle('CANOPY://deck')
  await expect(page.getByRole('heading', { name: 'CANOPY' })).toBeVisible()
  await expect(page.getByRole('navigation', { name: 'main tabs' })).toBeVisible()
  await expect(page.locator('.hero-card .card-kicker').filter({ hasText: '当前阶段' })).toBeVisible()
  await expect(page.locator('.hero-card').getByRole('heading', { name: '任务开发' })).toBeVisible()
  await expect(page.getByText('Control Worker')).toBeHidden()

  await page.getByRole('button', { name: 'more' }).click()
  await expect(page.getByRole('heading', { name: '更多' })).toBeVisible()
  await expect(page.getByText('Control Worker')).toBeVisible()
  await assertNoCommandUi(page)
})

test('idle home only starts the real A00 workflow', async ({ page }) => {
  await installFakeBridge(page)
  const bridgeRequests: Record<string, unknown>[] = []
  await mockBridgeApi(page, () => ({ pending: false }), idleSnapshotsPayload, (request) => {
    bridgeRequests.push(request)
  })

  await page.goto('/')
  await expect(page.getByRole('button', { name: '启动' })).toBeVisible()
  await expect(page.getByText('项目目录')).toHaveCount(0)
  await expect(page.getByText('需求名称')).toHaveCount(0)
  await expect(page.getByText('选择阶段配置')).toHaveCount(0)
  await expect(page.getByText('第 1 阶段')).toHaveCount(0)
  await expect(page.getByText('主智能体')).toHaveCount(0)
  await expect(page.getByText('审核智能体数量')).toHaveCount(0)
  await expect(page.getByText('评审轮次')).toHaveCount(0)
  await expect(page.getByText('流程')).toHaveCount(0)
  await assertNoCommandUi(page)
  await expect(page.locator('select')).toHaveCount(0)

  await page.getByRole('button', { name: '启动' }).click()

  expect(bridgeRequests).toEqual([
    { action: 'workflow.a00.start', payload: { argv: [] } },
  ])
})

test('prompt submission locks choices and supports previous-stage back from requirement intake', async ({ page }) => {
  await installFakeBridge(page)
  let promptSnapshot: Record<string, unknown> = { pending: false }
  await mockBridgeApi(page, () => promptSnapshot, idleSnapshotsPayload)
  const promptResponses: unknown[] = []
  let releaseResponse: (() => void) | undefined
  await page.route('**/api/prompt-response', async (route) => {
    promptResponses.push(route.request().postDataJSON())
    await new Promise<void>((resolve) => {
      releaseResponse = resolve
    })
    promptSnapshot = { pending: false }
    return route.fulfill({ json: { ok: true, payload: { accepted: true } } })
  })

  await page.goto('/')
  promptSnapshot = {
    pending: true,
    prompt_id: 'p-init',
    prompt_type: 'select',
    payload: {
      title: '是否执行 AGENT初始化',
      default_value: 'yes',
      allow_back: true,
      back_value: '__canopy_back__',
      options: [{ value: 'yes', label: 'yes' }, { value: 'no', label: 'no' }],
    },
  }
  await emitPrompt(page, {
    id: 'p-init',
    prompt_type: 'select',
    title: '是否执行 AGENT初始化',
    default_value: 'yes',
    allow_back: true,
    back_value: '__canopy_back__',
    options: [{ value: 'yes', label: 'yes' }, { value: 'no', label: 'no' }],
  })
  await promptChoice(page, 'no').click()
  await page.getByRole('button', { name: '提交' }).click()
  await expect(page.getByRole('status')).toContainText('处理中')
  await expect(promptChoice(page, 'yes')).toBeDisabled()
  await expect(promptChoice(page, 'no')).toBeDisabled()
  await expect(page.getByRole('button', { name: '提交' })).toBeDisabled()
  await expect.poll(() => promptResponses.length).toBe(1)
  releaseResponse?.()
  await expect(page.locator('.prompt-card')).toHaveCount(0)

  await page.unroute('**/api/prompt-response')
  await page.route('**/api/prompt-response', (route) => {
    promptResponses.push(route.request().postDataJSON())
    promptSnapshot = { pending: false }
    return route.fulfill({ json: { ok: true, payload: { accepted: true } } })
  })
  promptSnapshot = {
    pending: true,
    prompt_id: 'p-requirement',
    prompt_type: 'select',
    payload: {
      title: '选择已有需求或创建新需求',
      allow_back: true,
      back_value: '__canopy_back__',
      options: [{ value: '需求A', label: '需求A' }, { value: '__create_new__', label: '创建新需求' }],
    },
  }
  await emitPrompt(page, {
    id: 'p-requirement',
    prompt_type: 'select',
    title: '选择已有需求或创建新需求',
    allow_back: true,
    back_value: '__canopy_back__',
    options: [{ value: '需求A', label: '需求A' }, { value: '__create_new__', label: '创建新需求' }],
  })
  await page.locator('.prompt-card').getByRole('button', { name: '上一步' }).click()
  await expect.poll(() => promptResponses.length).toBe(2)
  expect(promptResponses[1]).toEqual({ prompt_id: 'p-requirement', value: '__canopy_back__' })
})

test('shows previous-step action for clarification reuse prompts driven by allow_back', async ({ page }) => {
  await installFakeBridge(page)
  let promptSnapshot: Record<string, unknown> = { pending: false }
  await mockBridgeApi(page, () => promptSnapshot, idleSnapshotsPayload)
  let promptResponse: unknown
  await page.route('**/api/prompt-response', (route) => {
    promptResponse = route.request().postDataJSON()
    promptSnapshot = { pending: false }
    return route.fulfill({ json: { ok: true, payload: { accepted: true } } })
  })

  await page.goto('/')
  promptSnapshot = {
    pending: true,
    prompt_id: 'p-clarification-reuse',
    prompt_type: 'select',
    payload: {
      title: '是否直接复用已有的需求澄清并跳入需求评审阶段',
      prompt_text: '是否直接复用已有的需求澄清并跳入需求评审阶段',
      allow_back: true,
      back_value: '__canopy_back__',
      options: [{ value: 'yes', label: 'yes' }, { value: 'no', label: 'no' }],
    },
  }
  await emitPrompt(page, {
    id: 'p-clarification-reuse',
    prompt_type: 'select',
    title: '是否直接复用已有的需求澄清并跳入需求评审阶段',
    prompt_text: '是否直接复用已有的需求澄清并跳入需求评审阶段',
    allow_back: true,
    back_value: '__canopy_back__',
    options: [{ value: 'yes', label: 'yes' }, { value: 'no', label: 'no' }],
  })

  await expect(page.locator('.prompt-card .primary-actions').getByRole('button', { name: '上一步' })).toBeVisible()
  await page.locator('.prompt-card .primary-actions').getByRole('button', { name: '上一步' }).click()
  expect(promptResponse).toEqual({ prompt_id: 'p-clarification-reuse', value: '__canopy_back__' })
})

test('hosts the TUI prompt sequence without a frontend parameter wizard', async ({ page }) => {
  await installFakeBridge(page)
  let promptSnapshot: Record<string, unknown> = { pending: false }
  await mockBridgeApi(page, () => promptSnapshot, idleSnapshotsPayload)
  const promptResponses: unknown[] = []
  await page.route('**/api/prompt-response', (route) => {
    promptResponses.push(route.request().postDataJSON())
    promptSnapshot = { pending: false }
    return route.fulfill({ json: { ok: true, payload: { accepted: true } } })
  })

  const showPrompt = async (payload: Record<string, unknown>) => {
    promptSnapshot = {
      pending: true,
      prompt_id: payload.id,
      prompt_type: payload.prompt_type,
      payload,
    }
    await emitPrompt(page, payload)
  }
  let expectedResponseCount = 0
  const submitAndWait = async () => {
    expectedResponseCount += 1
    await page.getByRole('button', { name: '提交' }).click()
    await expect.poll(() => promptResponses.length).toBe(expectedResponseCount)
    await expect(page.locator('.prompt-card')).toHaveCount(0)
  }

  await page.goto('/')

  await showPrompt({ id: 'p-project', prompt_type: 'text', prompt_text: '请输入项目目录', default: '/Users/me/Desktop/my_test_1' })
  await expect(page.locator('.prompt-card').getByRole('button', { name: '上一步' })).toHaveCount(0)
  await expect(page.getByLabel('请输入项目目录')).toHaveValue('/Users/me/Desktop/my_test_1')
  await page.getByLabel('请输入项目目录').fill('/Users/chenjunming/Desktop/my_test_1')
  await submitAndWait()

  await showPrompt({
    id: 'p-init',
    prompt_type: 'select',
    title: '是否执行 AGENTS 初始化',
    default_value: 'yes',
    allow_back: true,
    back_value: '__canopy_back__',
    options: [{ value: 'yes', label: '执行' }, { value: 'no', label: '跳过' }],
  })
  await expect(page.locator('.prompt-card .primary-actions').getByRole('button', { name: '上一步' })).toBeVisible()
  await page.locator('.prompt-card').getByRole('button', { name: '上一步' }).click()
  expectedResponseCount += 1
  await expect.poll(() => promptResponses.length).toBe(expectedResponseCount)
  await expect(page.locator('.prompt-card')).toHaveCount(0)
  expect(promptResponses[promptResponses.length - 1]).toEqual({ prompt_id: 'p-init', value: '__canopy_back__' })

  await showPrompt({ id: 'p-project-back', prompt_type: 'text', prompt_text: '请输入项目目录', default: '/Users/chenjunming/Desktop/my_test_1' })
  await expect(page.locator('.prompt-card').getByRole('button', { name: '上一步' })).toHaveCount(0)
  await submitAndWait()

  await showPrompt({
    id: 'p-init-2',
    prompt_type: 'select',
    title: '是否执行 AGENTS 初始化',
    default_value: 'yes',
    allow_back: true,
    back_value: '__canopy_back__',
    options: [{ value: 'yes', label: '执行' }, { value: 'no', label: '跳过' }],
  })
  await expect(promptChoice(page, '执行')).toBeChecked()
  await submitAndWait()

  await showPrompt({ id: 'p-targets', prompt_type: 'text', prompt_text: '输入额外目标目录，多个用逗号分隔，可留空', default_value: '' })
  await submitAndWait()

  await showPrompt({
    id: 'p-vendor',
    prompt_type: 'select',
    title: '选择厂商',
    allow_back: true,
    back_value: '__canopy_back__',
    options: [{ value: 'codex', label: 'codex' }, { value: 'gemini', label: 'gemini' }],
  })
  await promptChoice(page, 'gemini').click()
  await expect(promptChoice(page, 'gemini')).toBeChecked()
  await submitAndWait()

  await showPrompt({
    id: 'p-model',
    prompt_type: 'select',
    title: '选择 gemini 模型',
    options: [{ value: '__back__', label: '上一步' }, { value: 'auto', label: 'auto' }, { value: 'flash', label: 'flash' }],
  })
  await expect(promptChoice(page, '上一步')).toHaveCount(0)
  await expect(page.locator('.prompt-card .primary-actions').getByRole('button', { name: '上一步' })).toBeVisible()
  await promptChoice(page, 'flash').click()
  await expect(promptChoice(page, 'flash')).toBeChecked()
  await submitAndWait()

  await showPrompt({
    id: 'p-effort',
    prompt_type: 'select',
    title: '选择 flash 推理强度',
    default_value: 'medium',
    options: [{ value: 'low', label: 'low' }, { value: 'medium', label: 'medium' }, { value: 'high', label: 'high' }],
  })
  await expect(promptChoice(page, 'medium')).toBeChecked()
  await submitAndWait()

  await showPrompt({
    id: 'p-proxy',
    prompt_type: 'select',
    title: '选择代理端口',
    options: [{ value: '', label: '不使用' }, { value: '10900', label: '10900' }, { value: '__custom__', label: '自定义' }],
  })
  await promptChoice(page, '自定义').click()
  await expect(promptChoice(page, '自定义')).toBeChecked()
  await submitAndWait()

  await showPrompt({ id: 'p-proxy-custom', prompt_type: 'text', prompt_text: '输入代理端口或完整代理 URL（可留空）' })
  await page.getByLabel('输入代理端口或完整代理 URL（可留空）').fill('10900')
  await expect(page.getByLabel('输入代理端口或完整代理 URL（可留空）')).toHaveValue('10900')
  await submitAndWait()

  await showPrompt({ id: 'p-confirm', prompt_type: 'confirm', title: '确认开始执行', default: true })
  await expect(promptChoice(page, '是')).toBeChecked()
  await submitAndWait()

  await showPrompt({
    id: 'p-requirement',
    prompt_type: 'select',
    title: '选择已有需求或创建新需求',
    options: [{ value: '需求A', label: '需求A' }, { value: '__create_new__', label: '创建新需求' }],
  })
  await promptChoice(page, '需求A').click()
  await submitAndWait()

  await showPrompt({
    id: 'p-input-mode',
    prompt_type: 'select',
    title: '选择输入方式',
    options: [{ value: 'text', label: '直接输入' }, { value: 'file', label: '文件' }],
  })
  await submitAndWait()

  await showPrompt({
    id: 'p-body',
    prompt_type: 'multiline',
    title: '请输入原始需求正文',
    empty_retry_message: '需求正文不能为空',
    question_path: '/tmp/question.md',
  })
  await page.getByRole('button', { name: '预览' }).click()
  await expect(page.getByText('task doc')).toBeVisible()
  await page.getByRole('button', { name: 'close' }).click()
  await page.getByRole('button', { name: '提交' }).click()
  await expect(page.getByText('需求正文不能为空')).toBeVisible()
  await page.getByLabel('请输入原始需求正文').fill('第一行需求\n第二行需求')
  await submitAndWait()

  expect(promptResponses).toEqual([
    { prompt_id: 'p-project', value: '/Users/chenjunming/Desktop/my_test_1' },
    { prompt_id: 'p-init', value: '__canopy_back__' },
    { prompt_id: 'p-project-back', value: '/Users/chenjunming/Desktop/my_test_1' },
    { prompt_id: 'p-init-2', value: 'yes' },
    { prompt_id: 'p-targets', value: '' },
    { prompt_id: 'p-vendor', value: 'gemini' },
    { prompt_id: 'p-model', value: 'flash' },
    { prompt_id: 'p-effort', value: 'medium' },
    { prompt_id: 'p-proxy', value: '__custom__' },
    { prompt_id: 'p-proxy-custom', value: '10900' },
    { prompt_id: 'p-confirm', value: 'yes' },
    { prompt_id: 'p-requirement', value: '需求A' },
    { prompt_id: 'p-input-mode', value: 'text' },
    { prompt_id: 'p-body', value: '第一行需求\n第二行需求' },
  ])
})

test('shows project-dir validation inline and keeps the last typed value', async ({ page }) => {
  await installFakeBridge(page)
  const promptSnapshot = {
    pending: true,
    prompt_id: 'p-project-invalid',
    prompt_type: 'text',
    payload: {
      prompt_text: '输入项目工作目录',
      default: 'relative/path',
      error_message: '目录无效: 请输入绝对路径',
    },
  }
  await mockBridgeApi(page, () => promptSnapshot, idleSnapshotsPayload)

  await page.goto('/')
  await expect(page.getByLabel('输入项目工作目录')).toHaveValue('relative/path')
  await expect(page.locator('.prompt-card .form-error')).toContainText('目录无效: 请输入绝对路径')
})

test('switches app tabs, previews files in a sheet, and accepts named SSE updates', async ({ page }) => {
  await installFakeBridge(page)
  let promptSnapshot: Record<string, unknown> = { pending: false }
  await mockBridgeApi(page, () => promptSnapshot)
  let promptResponse: unknown
  await page.route('**/api/prompt-response', (route) => {
    promptResponse = route.request().postDataJSON()
    promptSnapshot = { pending: false }
    return route.fulfill({ json: { ok: true, payload: { accepted: true } } })
  })

  await page.goto('/')
  await page.locator('.bottom-tabs').getByRole('button', { name: '文件' }).click()
  await expect(page.getByText('Artifacts')).toBeVisible()
  await page.getByRole('button', { name: /^M1 ready/ }).click()
  await expect(page.getByText('task doc')).toBeVisible()
  await page.getByRole('button', { name: 'close' }).click()

  promptSnapshot = { pending: true, prompt_id: 'prompt_1', prompt_type: 'text', payload: { prompt_text: '请输入项目目录' } }
  await page.evaluate(() => {
    const source = (window as any).FakeEventSource.instances[0]
    source.emit('log.append', { text: 'hello from sse\n' })
    source.emit('prompt.request', { id: 'prompt_1', prompt_type: 'text', prompt_text: '请输入项目目录' })
  })

  await expect(page.getByText('需要你输入')).toBeVisible()
  await page.locator('.prompt-card input').fill('/tmp/project')
  await page.getByRole('button', { name: '提交' }).click()
  expect(promptResponse).toEqual({ prompt_id: 'prompt_1', value: '/tmp/project' })

  await page.locator('.bottom-tabs').getByRole('button', { name: '日志' }).click()
  await expect(page.getByText('hello from sse')).toBeVisible()
})

test('phone viewport keeps the prompt host app-like', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await installFakeBridge(page)
  let bridgeRequest: Record<string, unknown> | undefined
  let promptSnapshot: Record<string, unknown> = {
    pending: true,
    prompt_id: 'prompt_mobile',
    prompt_type: 'text',
    payload: { prompt_text: '输入需求名称', default: '手机端测试需求' },
  }
  await mockBridgeApi(page, () => promptSnapshot, idleSnapshotsPayload, (request) => {
    bridgeRequest = request
  })
  let promptResponse: unknown
  await page.route('**/api/prompt-response', (route) => {
    promptResponse = route.request().postDataJSON()
    promptSnapshot = { pending: false }
    return route.fulfill({ json: { ok: true, payload: { accepted: true } } })
  })

  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'CANOPY' })).toBeVisible()
  await expect(page.getByRole('navigation', { name: 'main tabs' })).toBeVisible()
  await expect(page.getByText('需要你输入')).toBeVisible()

  const hasHorizontalOverflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth)
  expect(hasHorizontalOverflow).toBe(false)

  await expect(page.getByLabel('输入需求名称')).toHaveValue('手机端测试需求')
  await page.getByRole('button', { name: '提交' }).click()
  expect(promptResponse).toEqual({ prompt_id: 'prompt_mobile', value: '手机端测试需求' })

  const startButton = page.getByRole('button', { name: '启动' })
  await expect(startButton).toBeEnabled()
  await startButton.click()
  expect(bridgeRequest).toEqual({ action: 'workflow.a00.start', payload: { argv: [] } })

  await page.locator('.bottom-tabs').getByRole('button', { name: '阶段' }).click()
  await expect(page.getByText('Timeline')).toBeVisible()
  await page.locator('.bottom-tabs').getByRole('button', { name: '文件' }).click()
  await page.locator('.bottom-tabs').getByRole('button', { name: '日志' }).click()
  await expect(page.getByText('已请求从路由层开始')).toBeVisible()
})
