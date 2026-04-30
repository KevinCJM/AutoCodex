import clipboard from 'clipboardy'

export async function copyToClipboard(text: string): Promise<boolean> {
  const value = text.trim()
  if (!value) return false
  await clipboard.write(value)
  return true
}
