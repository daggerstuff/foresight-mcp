import { describe, test, expect, beforeEach } from 'vitest'

import { VectorClock, LWWRegister, ORSet, LWWMap } from './crdt'

describe('VectorClock', () => {
  test('increment', () => {
    const vc = new VectorClock()
    vc.increment('node1')
    expect(vc.toDict()).toEqual({ node1: 1 })
    vc.increment('node1')
    expect(vc.toDict()).toEqual({ node1: 2 })
    vc.increment('node2')
    expect(vc.toDict()).toEqual({ node1: 2, node2: 1 })
  })

  test('merge', () => {
    const vc1 = new VectorClock({ node1: 2, node2: 1 })
    const vc2 = new VectorClock({ node1: 1, node2: 3, node3: 5 })
    vc1.merge(vc2)
    expect(vc1.toDict()).toEqual({ node1: 2, node2: 3, node3: 5 })
  })

  test('happensBefore', () => {
    const vc1 = new VectorClock({ node1: 1 })
    const vc2 = new VectorClock({ node1: 2 })
    const vc3 = new VectorClock({ node1: 1, node2: 1 })
    const vc4 = new VectorClock({ node2: 2 })

    expect(vc1.happensBefore(vc2)).toBe(true)
    expect(vc1.happensBefore(vc3)).toBe(true)
    expect(vc2.happensBefore(vc1)).toBe(false)
    expect(vc2.happensBefore(vc3)).toBe(false)
    expect(vc3.happensBefore(vc4)).toBe(false)
    expect(vc4.happensBefore(vc3)).toBe(false)
  })
})

describe('LWWRegister', () => {
  test('set and get', () => {
    const reg = new LWWRegister<string>()
    reg.set('value1', 'node1', 100)
    expect(reg.get()).toBe('value1')

    // Newer timestamp wins
    reg.set('value2', 'node2', 200)
    expect(reg.get()).toBe('value2')

    // Older timestamp ignored
    reg.set('value3', 'node3', 150)
    expect(reg.get()).toBe('value2')
  })

  test('merge', () => {
    const reg1 = new LWWRegister<string>('val1', 100, 'node1')
    const reg2 = new LWWRegister<string>('val2', 200, 'node2')

    reg1.merge(reg2)
    expect(reg1.get()).toBe('val2')
    expect(reg1.toDict().nodeId).toBe('node2')
  })
})

describe('ORSet', () => {
  test('add and contains', () => {
    const set = new ORSet<string>()
    set.setNodeId('node1')
    set.add('item1')
    expect(set.contains('item1')).toBe(true)
    expect(set.contains('item2')).toBe(false)
  })

  test('remove', () => {
    const set = new ORSet<string>()
    set.setNodeId('node1')
    set.add('item1')
    expect(set.contains('item1')).toBe(true)
    set.remove('item1')
    expect(set.contains('item1')).toBe(false)
  })

  test('merge', () => {
    const set1 = new ORSet<string>()
    set1.setNodeId('node1')
    set1.add('item1')

    const set2 = new ORSet<string>()
    set2.setNodeId('node2')
    set2.add('item2')

    set1.merge(set2)
    expect(set1.contains('item1')).toBe(true)
    expect(set1.contains('item2')).toBe(true)
  })
})

describe('LWWMap', () => {
  test('set and get', () => {
    const map = new LWWMap<string>()
    map.setNodeId('node1')
    map.set('key1', 'val1')
    expect(map.get('key1')).toBe('val1')
  })

  test('delete', () => {
    const map = new LWWMap<string>()
    map.setNodeId('node1')
    map.set('key1', 'val1')
    map.delete('key1')
    expect(map.get('key1')).toBe(null)
  })

  test('merge', () => {
    const map1 = new LWWMap<string>()
    map1.setNodeId('node1')
    map1.set('key1', 'val1')

    const map2 = new LWWMap<string>()
    map2.setNodeId('node2')
    map2.set('key2', 'val2')

    map1.merge(map2)
    expect(map1.get('key1')).toBe('val1')
    expect(map1.get('key2')).toBe('val2')
  })
})
