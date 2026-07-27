import { expect, test } from 'bun:test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

test('web overview renders project Graphify status and report preview without changing agent labels', () => {
  const app = readFileSync(join(import.meta.dir, 'App.tsx'), 'utf8')
  const agents = readFileSync(join(import.meta.dir, 'domain/agents.ts'), 'utf8')
  expect(app.includes('label="代码图谱"')).toBe(true)
  expect(app.includes('graphify.version')).toBe(true)
  expect(app.includes('Graphify evidence report')).toBe(true)
  expect(agents.includes('graphify')).toBe(false)
})
