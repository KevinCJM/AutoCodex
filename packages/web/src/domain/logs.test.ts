import { expect, test } from 'bun:test'
import { classifyLog } from './logs'

test('web uses the TUI log categories for summary warning runtime and hitl', () => {
  expect(classifyLog('执行摘要: done').kind).toBe('summary')
  expect(classifyLog('警告: retrying').kind).toBe('warning')
  expect(classifyLog('run_id: run-1\ntmux attach -t agent').kind).toBe('runtime')
  expect(classifyLog('HITL 第 1 轮').kind).toBe('hitl')
})
