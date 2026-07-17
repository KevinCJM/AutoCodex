import type { LogEntry, LogKind } from './types'

let nextId = 1

export function classifyLog(text: string, eventType = 'log.append', payload: Record<string, unknown> = {}): LogEntry {
  const lines = String(text || '').split(/\r?\n/).filter((line) => line.length > 0)
  const firstLine = lines.find((line) => line.trim())?.trim() || ''
  const explicitKind = String(payload.log_kind ?? '').trim() as LogKind
  const kind: LogKind = explicitKind || (
    eventType === 'error' || /^(ERROR|错误)[:：]/i.test(firstLine)
      ? 'error'
      : /HITL|人工介入/i.test(firstLine)
        ? 'hitl'
        : /^执行摘要[:：]/.test(firstLine)
          ? 'summary'
          : /^警告[:：]/.test(firstLine)
            ? 'warning'
            : /^\s*=+\s*.+\s*=+\s*$/.test(firstLine) || /阶段|stage/i.test(firstLine)
          ? 'stage'
              : lines.some((line) => /^(run_id:|runtime_dir:|tmux sessions:)/.test(line.trim()) || line.includes('tmux attach -t') || line.trim().endsWith('已启动'))
                ? 'runtime'
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
