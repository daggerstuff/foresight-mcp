import { describe, expect, it } from 'vitest'

import { BlockManager } from './blocks'
import { MergeStrategy, RetentionPolicy } from './types'

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

  it('enforces character limits when creating and updating blocks', () => {
    const manager = new BlockManager()

    manager.register({ label: 'pending_items', charLimit: 5 })

    expect(() => manager.createBlock('pending_items', '123456')).toThrow(
      "Block 'pending_items' content exceeds 5 character limit",
    )

    manager.createBlock('pending_items', '12')

    expect(() => manager.updateContent('pending_items', '3456')).toThrow(
      "Block 'pending_items' content exceeds 5 character limit",
    )
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
