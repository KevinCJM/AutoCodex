const draftStore = new Map<string, string>()
const historyStore = new Map<string, string[]>()

export function readPromptDraft(key?: string) {
  if (!key) return ''
  return draftStore.get(key) ?? ''
}

export function writePromptDraft(key: string | undefined, value: string) {
  if (!key) return
  draftStore.set(key, value)
}

export function clearPromptDraft(key?: string) {
  if (!key) return
  draftStore.delete(key)
}

export function latestPromptHistory(key?: string) {
  if (!key) return ''
  return historyStore.get(key)?.[0] ?? ''
}

export function rememberPromptValue(key: string | undefined, value: string) {
  if (!key) return
  const normalized = String(value ?? '')
  if (!normalized.trim()) return
  const previous = historyStore.get(key) ?? []
  const next = [normalized, ...previous.filter((item) => item !== normalized)].slice(0, 6)
  historyStore.set(key, next)
  clearPromptDraft(key)
}
