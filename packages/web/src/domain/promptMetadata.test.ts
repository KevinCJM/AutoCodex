import { expect, test } from 'bun:test'
import { grillPromptKicker, promptAnswerOptions, promptCanSubmit, promptRecoveryMessage, resolvePromptMetadata } from './promptMetadata'

test('prompt metadata accepts snake and camel case with payload fallback', () => {
  const metadata = resolvePromptMetadata(
    { interaction_kind: 'grill', question_index: '5' },
    { recommendation: '保留现有边界', reasonText: '影响数据契约' },
  )

  expect(metadata).toMatchObject({
    interactionKind: 'grill',
    questionIndex: 5,
    recommendation: '保留现有边界',
    reasonText: '影响数据契约',
    isGrill: true,
  })
  expect(grillPromptKicker(metadata)).toBe('Grill 第 5 轮')
})

test('legacy prompts keep their original kicker', () => {
  const metadata = resolvePromptMetadata({}, {})
  expect(metadata.isGrill).toBe(false)
  expect(grillPromptKicker(metadata)).toBe('需要你输入')
  expect(resolvePromptMetadata({ recovery_kind: 'grill_decision' }).isGrill).toBe(true)
})

test('read-only recovery metadata blocks submission and exposes its status message', () => {
  expect(promptCanSubmit({})).toBe(true)
  expect(promptCanSubmit({ can_submit: false })).toBe(false)
  expect(promptCanSubmit({ canSubmit: false })).toBe(false)
  expect(promptRecoveryMessage({ recoveryMessage: '等待 runner 恢复' })).toBe('等待 runner 恢复')
  expect(promptAnswerOptions({ answerOptions: ['方案 A', '方案 B'] })).toEqual(['方案 A', '方案 B'])
})
