import {
  normalizeAgentCatalog,
  normalizeBootstrapPayload,
  normalizeFilePreview,
  normalizePromptSnapshot,
  normalizeRequirementsList,
  normalizeSnapshotsPayload,
} from '../domain/normalize'
import type { AgentCatalog, BootstrapPayload, BridgeEvent, FilePreview, PromptSnapshot, RequirementsList, SnapshotsPayload } from '../domain/types'

type ApiEnvelope<T> = {
  ok: boolean
  payload?: T
  error?: string
}

type EventSourceLike = {
  addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void
  close(): void
}

export const BRIDGE_EVENT_TYPES = [
  'log.append',
  'progress.start',
  'progress.update',
  'progress.stop',
  'prompt.request',
  'stage.changed',
  'snapshot.app',
  'snapshot.stage',
  'snapshot.control',
  'snapshot.hitl',
  'snapshot.artifacts',
] as const

async function readEnvelope<T>(response: Response): Promise<T> {
  const text = await response.text()
  if (!text.trim()) {
    throw new Error(`后端响应为空 (HTTP ${response.status})。请确认 WebBackend 已启动并已重启到最新代码。`)
  }
  let envelope: ApiEnvelope<T>
  try {
    envelope = JSON.parse(text) as ApiEnvelope<T>
  } catch {
    const preview = text.trim().slice(0, 160)
    throw new Error(`后端返回非 JSON 响应 (HTTP ${response.status})。请确认 WebBackend 已启动在 8765，响应: ${preview}`)
  }
  if (!response.ok || !envelope.ok) {
    throw new Error(String(envelope.error || `HTTP ${response.status}`))
  }
  return envelope.payload as T
}

export async function getBootstrap(): Promise<BootstrapPayload> {
  return normalizeBootstrapPayload(await readEnvelope<unknown>(await fetch('/api/bootstrap')))
}

export async function getSnapshots(): Promise<SnapshotsPayload> {
  return normalizeSnapshotsPayload(await readEnvelope<unknown>(await fetch('/api/snapshots')))
}

export async function getPrompt(): Promise<PromptSnapshot> {
  return normalizePromptSnapshot(await readEnvelope<unknown>(await fetch('/api/prompt')))
}

export async function getAgentCatalog(): Promise<AgentCatalog> {
  return normalizeAgentCatalog(await readEnvelope<unknown>(await fetch('/api/agent-catalog')))
}

export async function getRequirements(projectDir: string): Promise<RequirementsList> {
  const query = new URLSearchParams({ project_dir: projectDir })
  return normalizeRequirementsList(await readEnvelope<unknown>(await fetch(`/api/requirements?${query.toString()}`)))
}

export async function getFilePreview(path: string): Promise<FilePreview> {
  const query = new URLSearchParams({ path })
  return normalizeFilePreview(await readEnvelope<unknown>(await fetch(`/api/file-preview?${query.toString()}`)))
}

export async function postBridgeRequest(action: string, payload: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
  return await readEnvelope<Record<string, unknown>>(await fetch('/api/request', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, payload }),
  }))
}

export async function submitPromptResponse(promptId: string, value: unknown): Promise<Record<string, unknown>> {
  return await readEnvelope<Record<string, unknown>>(await fetch('/api/prompt-response', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prompt_id: promptId, value }),
  }))
}

export function parseBridgeEvent(raw: string): BridgeEvent {
  const parsed = JSON.parse(raw) as { type?: unknown; payload?: unknown }
  return {
    type: String(parsed.type || 'message'),
    payload: parsed.payload && typeof parsed.payload === 'object' ? parsed.payload as Record<string, unknown> : {},
  }
}

export function connectBridgeEvents(
  onEvent: (event: BridgeEvent) => void,
  options: {
    onError?: () => void
    eventSourceFactory?: (url: string) => EventSourceLike
  } = {},
): () => void {
  const source = options.eventSourceFactory?.('/api/events') ?? new EventSource('/api/events')
  for (const eventType of BRIDGE_EVENT_TYPES) {
    source.addEventListener(eventType, (message) => {
      onEvent(parseBridgeEvent(message.data))
    })
  }
  source.addEventListener('error', (message) => {
    const data = (message as MessageEvent<string>).data
    if (typeof data === 'string' && data.trim()) {
      onEvent(parseBridgeEvent(data))
      return
    }
    options.onError?.()
  })
  return () => source.close()
}
