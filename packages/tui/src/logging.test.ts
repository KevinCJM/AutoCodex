import { expect, test } from 'bun:test'
import {
  MAX_LINES_PER_ENTRY,
  appendEntryWithMerge,
  buildLogEntry,
  classifyTextLog,
  formatPayloadLines,
} from './logging'

test('classifyTextLog recognizes stage dividers, summaries, warnings, runtime info and plain text', () => {
  expect(classifyTextLog('====== AGENT初始化阶段 ======').kind).toBe('stage')
  expect(classifyTextLog('执行摘要:\nproject_dir: /tmp/demo').kind).toBe('summary')
  expect(classifyTextLog('警告：文件不存在 -> /tmp/demo').kind).toBe('warning')
  expect(classifyTextLog('run_id: run_123\nruntime_dir: /tmp/runtime').kind).toBe('runtime')
  expect(classifyTextLog('HITL 第 2 轮\nHITL 问题文档: /tmp/question.md', 'log.append').kind).toBe('hitl')
  expect(classifyTextLog('普通日志一行').kind).toBe('plain')
})

test('classifyTextLog preserves hitl round metadata from backend payload', () => {
  const entry = classifyTextLog('HITL 问题文档: /tmp/question.md\n- [阻断] 需要补充碰撞规则', 'log.append', {
    log_kind: 'hitl',
    hitl_round: 3,
    log_title: 'HITL 第 3 轮',
  })
  expect(entry.kind).toBe('hitl')
  expect(entry.title).toBe('HITL 第 3 轮')
  expect(entry.hitlRound).toBe(3)
  expect(entry.lines).toEqual(['HITL 问题文档: /tmp/question.md', '- [阻断] 需要补充碰撞规则'])
})

test('appendEntryWithMerge merges plain entries from the same source and preserves summary extension', () => {
  const first = classifyTextLog('普通日志A', 'log.append')
  const second = classifyTextLog('普通日志B', 'log.append')
  const summary = classifyTextLog('执行摘要:', 'log.append')
  const detail = classifyTextLog('project_dir: /tmp/project', 'log.append')

  const mergedPlain = appendEntryWithMerge([first], second)
  expect(mergedPlain).toHaveLength(1)
  expect(mergedPlain[0]?.lines).toEqual(['普通日志A', '普通日志B'])

  const mergedSummary = appendEntryWithMerge([summary], detail)
  expect(mergedSummary).toHaveLength(1)
  expect(mergedSummary[0]?.kind).toBe('summary')
  expect(mergedSummary[0]?.lines).toEqual(['project_dir: /tmp/project'])
})

test('appendEntryWithMerge avoids creating oversized merged entries', () => {
  const lines = Array.from({ length: MAX_LINES_PER_ENTRY }, (_, index) => `line_${index}`)
  const full = buildLogEntry({
    kind: 'plain',
    sourceEventType: 'log.append',
    title: '日志',
    lines,
  })
  const extra = classifyTextLog('tail_line', 'log.append')

  const merged = appendEntryWithMerge([full], extra)
  expect(merged).toHaveLength(2)
  expect(merged[0]?.lines).toHaveLength(MAX_LINES_PER_ENTRY)
  expect(merged[1]?.lines).toEqual(['tail_line'])
})

test('appendEntryWithMerge splits a single oversized entry into continuation blocks', () => {
  const lines = Array.from({ length: MAX_LINES_PER_ENTRY + 5 }, (_, index) => `line_${index}`)
  const oversized = buildLogEntry({
    kind: 'runtime',
    sourceEventType: 'request.action',
    title: '运行时事件',
    lines,
  })

  const result = appendEntryWithMerge([], oversized)
  expect(result).toHaveLength(2)
  expect(result[0]?.title).toBe('运行时事件')
  expect(result[1]?.title).toBe('运行时事件（续）')
  expect(result[0]?.lines).toHaveLength(MAX_LINES_PER_ENTRY)
  expect(result[1]?.lines).toHaveLength(5)
})

test('formatPayloadLines pretty prints runtime payload objects', () => {
  expect(formatPayloadLines({})).toEqual(['(empty)'])
  expect(formatPayloadLines({ action: 'run', ok: true })).toEqual(['{', '  "action": "run",', '  "ok": true', '}'])
})
