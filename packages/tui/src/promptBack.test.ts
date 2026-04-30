import { expect, test } from 'bun:test'

import { PROMPT_BACK_VALUE, promptAllowsBack, resolvePromptBackValue, withPromptBackOption } from './promptBack'

test('withPromptBackOption appends previous-step option when backend allows back', () => {
  const options = withPromptBackOption([{ value: 'yes', label: 'yes' }], {
    allow_back: true,
    back_value: '__custom_back__',
  })

  expect(options).toEqual([
    { value: 'yes', label: 'yes' },
    { value: '__custom_back__', label: '[上一步]' },
  ])
})

test('withPromptBackOption keeps choices unchanged when back is not allowed', () => {
  const options = withPromptBackOption([{ value: 'yes', label: 'yes' }], {})

  expect(options).toEqual([{ value: 'yes', label: 'yes' }])
  expect(promptAllowsBack({ allowBack: 'true' })).toBe(true)
  expect(resolvePromptBackValue({})).toBe(PROMPT_BACK_VALUE)
})

test('withPromptBackOption does not duplicate an existing back value', () => {
  const options = withPromptBackOption([
    { value: 'yes', label: 'yes' },
    { value: PROMPT_BACK_VALUE, label: '[上一步]' },
  ], {
    allow_back: true,
  })

  expect(options).toEqual([
    { value: 'yes', label: 'yes' },
    { value: PROMPT_BACK_VALUE, label: '[上一步]' },
  ])
})
