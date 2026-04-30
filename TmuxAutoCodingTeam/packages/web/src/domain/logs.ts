import type { LogEntry, LogKind } from './types'

let nextId = 1

export function classifyLog(text: string, eventType = 'log.append', payload: Record<string, unknown> = {}): LogEntry {
  const lines = String(text || '').split(/\r?\n/).filter((line) => line.length > 0)
  const explicitKind = String(payload.log_kind ?? '').trim() as LogKind
  const kind: LogKind = explicitKind || (
    eventType === 'error' || /^ERROR[:：]/i.test(lines[0] || '')
      ? 'error'
      : /HITL|人工介入/.test(text)
        ? 'hitl'
        : /=====|阶段|stage/i.test(lines[0] || '')
          ? 'stage'
          : 'plain'
  )
  const title = String(payload.log_title ?? '').trim() || (
    kind === 'plain' ? 'log' : kind
  )
  return {
    id: nextId++,
    kind,
    title,
    lines: lines.length > 0 ? lines.slice(0, 80) : [''],
    createdAt: new Date().toISOString(),
  }
}

export function appendLog(entries: LogEntry[], entry: LogEntry, limit = 240): LogEntry[] {
  return [...entries, entry].slice(-limit)
}
