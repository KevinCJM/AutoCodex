import { expect, test } from 'bun:test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

test('ui component files exist with expected exports', () => {
  const root = import.meta.dir
  const files = [
    ['ui/PromptTextarea.tsx', 'PromptTextarea'],
    ['ui/PromptInputPanel.tsx', 'PromptInputPanel'],
    ['ui/DialogSelect.tsx', 'DialogSelect'],
    ['ui/DialogPrompt.tsx', 'DialogPrompt'],
    ['ui/DialogConfirm.tsx', 'DialogConfirm'],
  ] as const
  for (const [relativePath, exportName] of files) {
    const content = readFileSync(join(root, relativePath), 'utf8')
    expect(content.includes(`export function ${exportName}`)).toBe(true)
  }
})

test('PromptInputPanel centralizes title, optional helper lines, and textarea wiring for text prompts', () => {
  const content = readFileSync(join(import.meta.dir, 'ui/PromptInputPanel.tsx'), 'utf8')
  expect(content.includes("type Props = {")).toBe(true)
  expect(content.includes("mode?: 'singleline' | 'multiline'")).toBe(true)
  expect(content.includes('hintLines?: string[]')).toBe(true)
  expect(content.includes('showSubmitHelper?: boolean')).toBe(true)
  expect(content.includes("props.showSubmitHelper && multiline() ? 'Enter 提交，Shift+Enter / Meta+Enter / Ctrl+J 换行' : ''")).toBe(true)
  expect(content.includes('<PromptTextarea')).toBe(true)
  expect(content.includes('focusToken={props.focusToken}')).toBe(true)
  expect(content.includes('focused={props.focused}')).toBe(true)
  expect(content.includes('height={multiline() ? 5 : 1}')).toBe(true)
  expect(content.includes('<For each={props.hintLines ?? []}>')).toBe(true)
})

test('DialogPrompt delegates to PromptInputPanel instead of assembling its own helper and textarea layout', () => {
  const content = readFileSync(join(import.meta.dir, 'ui/DialogPrompt.tsx'), 'utf8')
  expect(content.includes("import { PromptInputPanel } from './PromptInputPanel'")).toBe(true)
  expect(content.includes('<PromptInputPanel')).toBe(true)
  expect(content.includes("mode={props.multiline ? 'multiline' : 'singleline'}")).toBe(true)
  expect(content.includes('showSubmitHelper={Boolean(props.multiline)}')).toBe(true)
  expect(content.includes('<PromptTextarea')).toBe(false)
})

test('TUI startup enables mouse capture, alternate-screen mode, and console copy-selection wiring', () => {
  const content = readFileSync(join(import.meta.dir, 'index.tsx'), 'utf8')
  expect(content.includes('useMouse: true')).toBe(true)
  expect(content.includes("screenMode: 'alternate-screen'")).toBe(true)
  expect(content.includes('consoleOptions: {')).toBe(true)
  expect(content.includes("keyBindings: [{ name: 'y', ctrl: true, action: 'copy-selection' }]")).toBe(true)
  expect(content.includes('onCopySelection: (text) => {')).toBe(true)
  expect(content.includes('void copyToClipboard(text)')).toBe(true)
})

test('PromptTextarea binds Shift+Enter and Ctrl+J to newline for multiline input', () => {
  const content = readFileSync(join(import.meta.dir, 'ui/PromptTextarea.tsx'), 'utf8')
  expect(content.includes("{ name: 'return', shift: true, action: 'newline' }")).toBe(true)
  expect(content.includes("{ name: 'j', ctrl: true, action: 'newline' }")).toBe(true)
})

test('PromptTextarea explicitly focuses the textarea on mount and reset', () => {
  const content = readFileSync(join(import.meta.dir, 'ui/PromptTextarea.tsx'), 'utf8')
  expect(content.includes('textarea.focus()')).toBe(true)
  expect(content.includes('textarea.gotoLineEnd()')).toBe(true)
  expect(content.includes('textarea.blur()')).toBe(true)
  expect(content.includes('props.focusToken')).toBe(true)
  expect(content.includes('const timer = setInterval(() => {')).toBe(true)
  expect(content.includes('scrollY: textarea.scrollY')).toBe(true)
  expect(content.includes('const scrollTextareaBy = (delta: number) => {')).toBe(true)
  expect(content.includes('textarea.scrollY = Math.max(0, Math.min(maxScroll, textarea.scrollY + delta))')).toBe(true)
})

test('PromptTextarea persists draft and remembers submitted history for prompt reuse', () => {
  const content = readFileSync(join(import.meta.dir, 'ui/PromptTextarea.tsx'), 'utf8')
  expect(content.includes('readPromptDraft')).toBe(true)
  expect(content.includes('writePromptDraft')).toBe(true)
  expect(content.includes('rememberPromptValue')).toBe(true)
  expect(content.includes('<box flexDirection="row" width="100%">')).toBe(true)
  expect(content.includes("{row.active ? '#' : '|'}")).toBe(true)
  expect(content.includes('onMouseScroll={(event) => {')).toBe(true)
  expect(content.includes('scrollTextareaBy(direction === \'up\' ? -1 : 1)')).toBe(true)
})

test('DialogSelect uses terminal-aware sizing and width clipping so long option lists remain navigable inside the dialog border', () => {
  const content = readFileSync(join(import.meta.dir, 'ui/DialogSelect.tsx'), 'utf8')
  expect(content.includes('type { ScrollBoxRenderable }')).toBe(true)
  expect(content.includes('useTerminalDimensions')).toBe(true)
  expect(content.includes('active?: boolean')).toBe(true)
  expect(content.includes("if (props.active === false) return")).toBe(true)
  expect(content.includes('const maxVisibleRows = createMemo(() => Math.max(2, Math.floor(dimensions().height / 2) - 6))')).toBe(true)
  expect(content.includes('const visibleRows = createMemo(() => (shouldScroll() ? maxVisibleRows() : expandedRows()))')).toBe(true)
  expect(content.includes('width="100%"')).toBe(true)
  expect(content.includes('overflow="hidden" wrapMode="none"')).toBe(true)
  expect(content.includes('fallback={')).toBe(true)
  expect(content.includes('<box flexDirection="column" width="100%" height={visibleRows()} minHeight={visibleRows()}>')).toBe(true)
  expect(content.includes('<scrollbox')).toBe(true)
  expect(content.includes('height={visibleRows()}')).toBe(true)
  expect(content.includes('maxHeight={visibleRows()}')).toBe(true)
})

test('DialogConfirm forwards dialog active state to the shared select renderer', () => {
  const content = readFileSync(join(import.meta.dir, 'ui/DialogConfirm.tsx'), 'utf8')
  expect(content.includes('active?: boolean')).toBe(true)
  expect(content.includes('active={props.active}')).toBe(true)
})

test('clipboard helper writes trimmed text to the system clipboard', () => {
  const content = readFileSync(join(import.meta.dir, 'clipboard.ts'), 'utf8')
  expect(content.includes("import clipboard from 'clipboardy'")).toBe(true)
  expect(content.includes('export async function copyToClipboard(text: string): Promise<boolean> {')).toBe(true)
  expect(content.includes('const value = text.trim()')).toBe(true)
  expect(content.includes('await clipboard.write(value)')).toBe(true)
})
