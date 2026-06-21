import { describe, expect, it } from 'vitest'

import { BlockManager } from './blocks'
import { MemoryInjector } from './blocks'
import { InjectionPoint, MergeStrategy, RetentionPolicy } from './types'

describe('BlockManager', () => {
  it('registers schemas from minimal options and creates blocks', () => {
    const manager = new BlockManager()

    manager.register({ label: 'project_context', description: 'Project scope' })
    const block = manager.createBlock('project_context', 'Use focused tests')

    expect(manager.getSchema('project_context')).toMatchObject({
      label: 'project_context',
      description: 'Project scope',
      retentionPolicy: RetentionPolicy.ShortTerm,
      mergeStrategy: MergeStrategy.Append,
    })
    expect(block.content).toBe('Use focused tests')
    expect(block.version).toBe(0)
    expect(manager.get('project_context')).toBe(block)
  })

  it('rejects duplicate schemas and unknown block labels', () => {
    const manager = new BlockManager()

    manager.register({ label: 'guidance' })

    expect(() => manager.register({ label: 'guidance' })).toThrow(
      "Block schema 'guidance' already registered",
    )
    expect(() => manager.createBlock('missing')).toThrow(
      "Block schema 'missing' not found",
    )
  })

  it('charLimit is enforced at injection time, not storage time', () => {
    const manager = new BlockManager()

    manager.register({ label: 'pending_items', charLimit: 5 })

    // Should NOT throw - charLimit is checked at injection time, not storage
    manager.createBlock('pending_items', '123456')

    // Also should NOT throw for updates
    manager.updateContent('pending_items', '789')

    // Verify the block was created/updated with the full content
    expect(manager.get('pending_items')?.content).toBe('123456\n789')

    // But when MemoryInjector processes it, content is truncated
    const injector = new MemoryInjector(manager)
    const result = injector.inject('User prompt')

    // Content should be truncated to 4 chars + ellipsis (5 - 1 = 4 chars)
    expect(result).toContain('1234…')
  })

  it('uses append and replace merge strategies for updates', () => {
    const manager = new BlockManager()

    manager.register({ label: 'append_block' })
    manager.createBlock('append_block', 'first')
    manager.updateContent('append_block', 'second')

    expect(manager.get('append_block')?.content).toBe('first\nsecond')
    expect(manager.get('append_block')?.version).toBe(1)

    manager.register({
      label: 'replace_block',
      mergeStrategy: MergeStrategy.Replace,
    })
    manager.createBlock('replace_block', 'first')
    manager.updateContent('replace_block', 'second')

    expect(manager.get('replace_block')?.content).toBe('second')
  })

  it('clears blocks without removing registered schemas', () => {
    const manager = new BlockManager()

    manager.register({ label: 'session_patterns' })
    manager.createBlock('session_patterns', 'keep schemas')
    manager.clear()

    expect(manager.list()).toEqual([])
    expect(manager.listSchemas()).toHaveLength(1)
    expect(manager.getSchema('session_patterns')?.label).toBe(
      'session_patterns',
    )
  })
})

describe('MemoryInjector', () => {
  it('prepends PrePrompt blocks to user prompt', () => {
    const manager = new BlockManager()
    manager.register({
      label: 'guidance',
      injectionPoint: InjectionPoint.PrePrompt,
    })
    manager.createBlock('guidance', 'Be concise.')

    const injector = new MemoryInjector(manager)
    const result = injector.inject('Tell me about AI')

    expect(result).toBe('Be concise.\n\nTell me about AI')
  })

  it('appends PostPrompt blocks to user prompt', () => {
    const manager = new BlockManager()
    manager.register({
      label: 'context',
      injectionPoint: InjectionPoint.PostPrompt,
    })
    manager.createBlock('context', 'End with a question.')

    const injector = new MemoryInjector(manager)
    const result = injector.inject('What is ML?')

    expect(result).toBe('What is ML?\n\nEnd with a question.')
  })

  it('excludes WhisperOnly blocks from inject() result', () => {
    const manager = new BlockManager()
    manager.register({
      label: 'reminder',
      injectionPoint: InjectionPoint.WhisperOnly,
    })
    manager.createBlock('reminder', 'User prefers short answers.')

    const injector = new MemoryInjector(manager)
    const result = injector.inject('Hello')

    expect(result).toBe('Hello') // WhisperOnly not included
  })

  it('returns WhisperOnly content from getWhisperOnly()', () => {
    const manager = new BlockManager()
    manager.register({
      label: 'reminder',
      injectionPoint: InjectionPoint.WhisperOnly,
    })
    manager.createBlock('reminder', 'User prefers short answers.')

    const injector = new MemoryInjector(manager)
    const whisper = injector.getWhisperOnly()

    expect(whisper).toBe('User prefers short answers.')
  })

  it('combines multiple PrePrompt blocks', () => {
    const manager = new BlockManager()
    manager.register({
      label: 'system',
      injectionPoint: InjectionPoint.PrePrompt,
    })
    manager.createBlock('system', 'You are a helpful assistant.')
    manager.register({
      label: 'rules',
      injectionPoint: InjectionPoint.PrePrompt,
    })
    manager.createBlock('rules', 'Always verify facts.')

    const injector = new MemoryInjector(manager)
    const result = injector.inject('Hello')

    expect(result).toContain('You are a helpful assistant.')
    expect(result).toContain('Always verify facts.')
    expect(result).toContain('Hello')
  })

  it('handles both PrePrompt and PostPrompt blocks', () => {
    const manager = new BlockManager()
    manager.register({
      label: 'prefix',
      injectionPoint: InjectionPoint.PrePrompt,
    })
    manager.createBlock('prefix', 'PRE:')
    manager.register({
      label: 'suffix',
      injectionPoint: InjectionPoint.PostPrompt,
    })
    manager.createBlock('suffix', 'POST:')

    const injector = new MemoryInjector(manager)
    const result = injector.inject('USER_INPUT')

    expect(result).toBe('PRE:\n\nUSER_INPUT\n\nPOST:')
  })

  it('truncates content exceeding charLimit', () => {
    const manager = new BlockManager()
    manager.register({
      label: 'strict',
      injectionPoint: InjectionPoint.PrePrompt,
      charLimit: 10,
    })
    manager.createBlock(
      'strict',
      'This is a very long guidance text that should be truncated',
    )

    const injector = new MemoryInjector(manager)
    const result = injector.inject('Hi')

    // Should truncate to 10 chars
    expect(result.length).toBeLessThanOrEqual(10 + 'Hi'.length + 3) // +3 for \n\n
  })

  it('skips blocks with empty content', () => {
    const manager = new BlockManager()
    manager.register({
      label: 'empty',
      injectionPoint: InjectionPoint.PrePrompt,
    })
    manager.createBlock('empty', '')

    const injector = new MemoryInjector(manager)
    const result = injector.inject('Hi')

    expect(result).toBe('Hi') // Empty block not added
  })

  it('handles missing blocks gracefully', () => {
    const manager = new BlockManager()
    // No blocks registered

    const injector = new MemoryInjector(manager)
    const result = injector.inject('Hi')

    expect(result).toBe('Hi') // Just returns user prompt
  })
})
