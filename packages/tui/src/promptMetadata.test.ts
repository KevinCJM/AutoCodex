import { expect, test } from 'bun:test'
import { buildPromptMetadataHintLines, promptAnswerOptions, promptCanSubmit, promptRecoveryMessage, resolvePromptMetadata } from './promptMetadata'

test('Grill prompt metadata accepts snake case and builds human-friendly hints', () => {
  const payload = {
    interaction_kind: 'grill',
    question_index: 3,
    reason_text: '该选择会改变数据边界',
    recommendation: '保留现有边界',
  }

  expect(resolvePromptMetadata(payload)).toMatchObject({
    interactionKind: 'grill',
    questionIndex: 3,
    recommendation: '保留现有边界',
    reasonText: '该选择会改变数据边界',
    isGrill: true,
  })
  expect(buildPromptMetadataHintLines(payload)).toEqual([
    'Grill 第 3 轮',
    '为什么需要决定: 该选择会改变数据边界',
    '推荐答案: 保留现有边界',
  ])
})

test('legacy reason remains compatible and grill recovery metadata is recognized', () => {
  expect(buildPromptMetadataHintLines({ reasonText: '需要登录' })).toEqual(['原因: 需要登录'])
  expect(resolvePromptMetadata({ recovery_kind: 'grill_decision' }).isGrill).toBe(true)
  expect(resolvePromptMetadata({ interactionKind: 'grill-with-docs', questionIndex: '4' }).questionIndex).toBe(4)
})

test('read-only recovery metadata blocks submission and exposes its status message', () => {
  expect(promptCanSubmit({})).toBe(true)
  expect(promptCanSubmit({ can_submit: false })).toBe(false)
  expect(promptCanSubmit({ canSubmit: false })).toBe(false)
  expect(promptRecoveryMessage({ recovery_message: '等待 runner 恢复' })).toBe('等待 runner 恢复')
  expect(promptAnswerOptions({ answer_options: ['方案 A', '方案 B'] })).toEqual(['方案 A', '方案 B'])
})
