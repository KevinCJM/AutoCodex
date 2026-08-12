import { expect, test } from 'bun:test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

test('web overview renders project CodeGraph status without evidence reports or per-agent gates', () => {
  const app = readFileSync(join(import.meta.dir, 'App.tsx'), 'utf8')
  const agents = readFileSync(join(import.meta.dir, 'domain/agents.ts'), 'utf8')
  expect(app.includes('label="代码图谱"')).toBe(true)
  expect(app.includes('codegraphStatusSummary(codegraph)')).toBe(true)
  expect(app.includes('CodeGraph evidence report')).toBe(false)
  const configLabelBody = agents.split('export function buildAgentConfigLabel', 2)[1] ?? ''
  expect(configLabelBody.toLowerCase().includes('codegraph')).toBe(false)
  expect(agents.includes('buildCodeGraphUsageLabel')).toBe(false)
  expect(app.includes('codegraphUsageLabel')).toBe(false)
})

test('stage snapshots cannot replace project CodeGraph status', () => {
  const app = readFileSync(join(import.meta.dir, 'App.tsx'), 'utf8')
  const stageBranch = app.split("if (event.type === 'snapshot.stage')", 2)[1]?.split("if (event.type === 'snapshot.control')", 1)[0] ?? ''
  expect(stageBranch.includes('codegraph')).toBe(false)
})
