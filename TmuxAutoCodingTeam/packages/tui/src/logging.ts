export type LogKind = 'stage' | 'summary' | 'runtime' | 'warning' | 'error' | 'hitl' | 'plain'
export type LogLevel = 'info' | 'warning' | 'error'

export type LogEntry = {
  id: string
  kind: LogKind
  sourceEventType: string
  lines: string[]
  level: LogLevel
  title: string
  receivedAt: number
  hitlRound?: number
}

export const LOG_ENTRY_LIMIT = 180
export const MAX_LINES_PER_ENTRY = 48

let logSequence = 0

export function nextLogId() {
  logSequence += 1
  return `log_${logSequence}`
}

export function normalizeLogLines(text: string) {
  const normalized = String(text ?? '').replace(/\r\n?/g, '\n')
  const lines = normalized.split('\n')
  while (lines.length > 1 && lines[lines.length - 1] === '') {
    lines.pop()
  }
  return lines.length > 0 ? lines : ['']
}

export function firstNonEmptyLine(lines: string[]) {
  return lines.find((line) => line.trim().length > 0) ?? ''
}

export function stripStageTitle(line: string) {
  const stripped = line.replace(/^\s*=+\s*/, '').replace(/\s*=+\s*$/, '').trim()
  return stripped || '阶段切换'
}

export function stripLeadingLabel(line: string, label: string) {
  return line.replace(new RegExp(`^${label}[：:]?\\s*`), '').trim()
}

export function isStageDividerLine(line: string) {
  return /^\s*=+\s*.+\s*=+\s*$/.test(line)
}

export function isRuntimeLog(lines: string[]) {
  return lines.some((line) => {
    const text = line.trim()
    return (
      text.startsWith('run_id:') ||
      text.startsWith('runtime_dir:') ||
      text.startsWith('tmux sessions:') ||
      text.startsWith('可使用以下命令进入某个会话：') ||
      text.includes('tmux attach -t') ||
      text.endsWith('已启动')
    )
  })
}

export function buildLogEntry(input: {
  kind: LogKind
  sourceEventType: string
  title: string
  level?: LogLevel
  lines: string[]
  hitlRound?: number
}) {
  return {
    id: nextLogId(),
    kind: input.kind,
    sourceEventType: input.sourceEventType,
    lines: input.lines,
    level: input.level ?? (input.kind === 'error' ? 'error' : input.kind === 'warning' ? 'warning' : 'info'),
    title: input.title,
    receivedAt: Date.now(),
    hitlRound: input.hitlRound,
  } satisfies LogEntry
}

function extractHitlRound(value: unknown) {
  if (typeof value === 'number') {
    return Number.isFinite(value) && value > 0 ? Math.trunc(value) : undefined
  }
  const text = String(value ?? '').trim()
  if (!text) return undefined
  const match = text.match(/HITL\s*第\s*(\d+)\s*轮/i)
  if (!match) return undefined
  const parsed = Number.parseInt(match[1] ?? '', 10)
  return Number.isFinite(parsed) && parsed > 0 ? parsed : undefined
}

export function classifyTextLog(text: string, sourceEventType = 'log.append', payload: Record<string, unknown> = {}) {
  const lines = normalizeLogLines(text)
  const firstLine = firstNonEmptyLine(lines)
  const payloadKind = String(payload.log_kind ?? '').trim().toLowerCase()
  const payloadTitle = String(payload.log_title ?? '').trim()
  const hitlRound = extractHitlRound(payload.hitl_round ?? payloadTitle ?? firstLine)
  const titleLineRound = extractHitlRound(firstLine)
  if (payloadKind === 'hitl' || titleLineRound !== undefined) {
    const title = payloadTitle || (hitlRound !== undefined ? `HITL 第 ${hitlRound} 轮` : 'HITL')
    const bodyLines = titleLineRound !== undefined ? lines.slice(1) : lines
    return buildLogEntry({
      kind: 'hitl',
      sourceEventType,
      title,
      lines: bodyLines.length > 0 ? bodyLines : [''],
      hitlRound,
    })
  }
  if (isStageDividerLine(firstLine)) {
    return buildLogEntry({
      kind: 'stage',
      sourceEventType,
      title: stripStageTitle(firstLine),
      lines: lines.slice(1),
    })
  }
  if (firstLine.startsWith('执行摘要:')) {
    const detail = stripLeadingLabel(firstLine, '执行摘要')
    return buildLogEntry({
      kind: 'summary',
      sourceEventType,
      title: '执行摘要',
      lines: detail ? [detail, ...lines.slice(1)] : lines.slice(1),
    })
  }
  if (firstLine.startsWith('警告:') || firstLine.startsWith('警告：')) {
    const detail = stripLeadingLabel(firstLine, '警告')
    return buildLogEntry({
      kind: 'warning',
      sourceEventType,
      title: '警告',
      lines: detail ? [detail, ...lines.slice(1)] : lines.slice(1),
    })
  }
  if (firstLine.startsWith('ERROR:') || firstLine.startsWith('错误:') || firstLine.startsWith('错误：')) {
    const detail = firstLine.replace(/^(ERROR|错误)[：:]?\s*/, '').trim()
    return buildLogEntry({
      kind: 'error',
      sourceEventType,
      title: '错误',
      lines: detail ? [detail, ...lines.slice(1)] : lines.slice(1),
    })
  }
  if (isRuntimeLog(lines)) {
    return buildLogEntry({
      kind: 'runtime',
      sourceEventType,
      title: '运行时信息',
      lines,
    })
  }
  return buildLogEntry({
    kind: 'plain',
    sourceEventType,
    title: '日志',
    lines,
  })
}

export function formatPayloadLines(payload: Record<string, unknown>) {
  const keys = Object.keys(payload)
  if (keys.length === 0) return ['(empty)']
  return JSON.stringify(payload, null, 2).split('\n')
}

function splitOversizedEntry(entry: LogEntry) {
  if (entry.lines.length <= MAX_LINES_PER_ENTRY) return [entry]
  const chunks: LogEntry[] = []
  for (let index = 0; index < entry.lines.length; index += MAX_LINES_PER_ENTRY) {
    const lines = entry.lines.slice(index, index + MAX_LINES_PER_ENTRY)
    chunks.push({
      ...entry,
      id: nextLogId(),
      title: index === 0 ? entry.title : `${entry.title}（续）`,
      lines,
    })
  }
  return chunks
}

export function appendEntryWithMerge(entries: LogEntry[], entry: LogEntry) {
  let next = [...entries]
  for (const chunk of splitOversizedEntry(entry)) {
    next = appendSingleEntry(next, chunk)
  }
  return next.slice(-LOG_ENTRY_LIMIT)
}

function appendSingleEntry(entries: LogEntry[], entry: LogEntry) {
  const next = [...entries]
  const last = next.at(-1)
  const canMergePlain =
    last &&
    last.kind === 'plain' &&
    entry.kind === 'plain' &&
    last.sourceEventType === entry.sourceEventType &&
    (last.lines.at(-1)?.trim() ?? '') !== '' &&
    (entry.lines[0]?.trim() ?? '') !== ''
  const canExtendSummary =
    last &&
    last.kind === 'summary' &&
    entry.kind === 'plain' &&
    last.sourceEventType === entry.sourceEventType &&
    (entry.lines[0]?.trim() ?? '') !== ''
  const canExtendRuntime =
    last &&
    last.kind === 'runtime' &&
    entry.kind === 'plain' &&
    last.sourceEventType === entry.sourceEventType &&
    (entry.lines[0]?.trim() ?? '') !== ''
  const shouldMerge = canMergePlain || canExtendSummary || canExtendRuntime

  if (shouldMerge && last) {
    const mergedLines = [...last.lines, ...entry.lines]
    if (mergedLines.length <= MAX_LINES_PER_ENTRY) {
      next[next.length - 1] = {
        ...last,
        lines: mergedLines,
      }
      return next
    }
  }

  next.push(entry)
  return next
}
