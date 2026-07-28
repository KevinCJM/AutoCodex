import { expect, test } from 'bun:test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

test('web overview renders project Graphify status and report preview without changing agent labels', () => {
  const app = readFileSync(join(import.meta.dir, 'App.tsx'), 'utf8')
  const agents = readFileSync(join(import.meta.dir, 'domain/agents.ts'), 'utf8')
  expect(app.includes('label="代码图谱"')).toBe(true)
  expect(app.includes('graphifyStatusSummary(graphify)')).toBe(true)
  expect(app.includes('Graphify evidence report')).toBe(true)
  expect(agents.includes('graphify')).toBe(false)
})

test('stage snapshots cannot replace project Graphify status', () => {
  const app = readFileSync(join(import.meta.dir, 'App.tsx'), 'utf8')
  const stageBranch = app.split("if (event.type === 'snapshot.stage')", 2)[1]?.split("if (event.type === 'snapshot.control')", 1)[0] ?? ''
  expect(stageBranch.includes('graphify')).toBe(false)
})
