/**
 * CRDT (Conflict-Free Replicated Data Type) Implementations
 */
import { VectorClockType } from './types'

// =============================================================================
// Vector Clock
// =============================================================================

export class VectorClock {
  private clock: Record<string, number>

  constructor(data?: Record<string, number>) {
    this.clock = data ? { ...data } : {}
  }

  increment(nodeId: string): void {
    this.clock[nodeId] = (this.clock[nodeId] || 0) + 1
  }

  merge(other: VectorClock): void {
    const otherClock = other.toDict()
    const allNodes = new Set([
      ...Object.keys(this.clock),
      ...Object.keys(otherClock),
    ])

    for (const nodeId of allNodes) {
      this.clock[nodeId] = Math.max(
        this.clock[nodeId] || 0,
        otherClock[nodeId] || 0,
      )
    }
  }

  happensBefore(other: VectorClock): boolean {
    const otherClock = other.toDict()
    const allNodes = new Set([
      ...Object.keys(this.clock),
      ...Object.keys(otherClock),
    ])

    let atLeastOneLess = false
    for (const nodeId of allNodes) {
      const selfVal = this.clock[nodeId] || 0
      const otherVal = otherClock[nodeId] || 0

      if (selfVal > otherVal) {
        return false
      }
      if (selfVal < otherVal) {
        atLeastOneLess = true
      }
    }

    return atLeastOneLess
  }

  concurrentWith(other: VectorClock): boolean {
    return !this.happensBefore(other) && !other.happensBefore(this)
  }

  copy(): VectorClock {
    return new VectorClock(this.clock)
  }

  toDict(): Record<string, number> {
    return { ...this.clock }
  }

  static fromDict(data: Record<string, number>): VectorClock {
    return new VectorClock(data)
  }
}

// =============================================================================
// LWW-Register (Last-Writer-Wins Register)
// =============================================================================

export class LWWRegister<T> {
  private value: T | null
  private timestamp: number
  private nodeId: string
  private readonly vectorClock: VectorClock

  constructor(
    value: T | null = null,
    timestamp: number = 0,
    nodeId: string = '',
    vectorClock: VectorClock = new VectorClock(),
  ) {
    this.value = value
    this.timestamp = timestamp
    this.nodeId = nodeId
    this.vectorClock = vectorClock
  }

  set(newValue: T, nodeId: string, timestamp?: number): void {
    const ts = timestamp ?? Date.now() / 1000

    // Accept if timestamp is newer, or same timestamp and nodeId is >= current
    if (
      ts > this.timestamp ||
      (ts === this.timestamp && nodeId >= this.nodeId)
    ) {
      this.value = newValue
      this.timestamp = ts
      this.nodeId = nodeId
      this.vectorClock.increment(nodeId)
    }
  }

  merge(other: LWWRegister<T>): void {
    if (other.timestamp > this.timestamp) {
      this.value = other.value
      this.timestamp = other.timestamp
      this.nodeId = other.nodeId
      this.vectorClock.merge(other.vectorClock)
    } else if (other.timestamp === this.timestamp) {
      if (other.nodeId > this.nodeId) {
        this.value = other.value
        this.nodeId = other.nodeId
      }
      this.vectorClock.merge(other.vectorClock)
    } else {
      this.vectorClock.merge(other.vectorClock)
    }
  }

  get(): T | null {
    return this.value
  }

  toDict(): { value: T | null; timestamp: number; nodeId: string; vectorClock: Record<string, number> } {
    return {
      value: this.value,
      timestamp: this.timestamp,
      nodeId: this.nodeId,
      vectorClock: this.vectorClock.toDict(),
    }
  }

  static fromDict<T>(data: { value: T | null; timestamp?: number; nodeId?: string; vectorClock?: Record<string, number> }): LWWRegister<T> {
    return new LWWRegister<T>(
      data.value,
      data.timestamp ?? 0,
      data.nodeId ?? '',
      data.vectorClock
        ? VectorClock.fromDict(data.vectorClock)
        : new VectorClock(),
    )
  }
}

// =============================================================================
// OR-Set (Observed-Remove Set)
// =============================================================================

export class ORSet<T> {
  private readonly adds: Map<string, Set<string>> // hash -> Set of "timestamp:nodeId"
  private readonly removes: Map<string, Set<string>> // hash -> Set of "timestamp:nodeId"
  private nodeId: string = 'default'
  private vc: VectorClock = new VectorClock()

  constructor() {
    this.adds = new Map()
    this.removes = new Map()
  }

  setNodeId(nodeId: string): void {
    this.nodeId = nodeId
  }

  get vectorClock(): VectorClock {
    return this.vc
  }

  private getHash(element: T): string {
    const str = typeof element === 'string' ? element : JSON.stringify(element)
    let hash = 0
    for (let i = 0; i < str.length; i++) {
      const char = str.charCodeAt(i)
      hash = (hash << 5) - hash + char
      hash |= 0 // Convert to 32bit integer
    }
    return hash.toString(16)
  }

  /**
   * Performs garbage collection by removing tags that are in both adds and removes.
   * If all adds are removed, the element hash entry is deleted.
   */
  cleanup(): void {
    for (const [hash, adds] of this.adds.entries()) {
      const removes = this.removes.get(hash)
      if (removes) {
        for (const tag of adds) {
          if (removes.has(tag)) {
            adds.delete(tag)
            removes.delete(tag)
          }
        }
        if (removes.size === 0) {
          this.removes.delete(hash)
        }
      }
      if (adds.size === 0) {
        this.adds.delete(hash)
      }
    }
  }

  add(element: T): void {
    const ts = Date.now() / 1000
    const elementHash = this.getHash(element)
    const tag = `${ts}:${this.nodeId}`

    if (!this.adds.has(elementHash)) {
      this.adds.set(elementHash, new Set())
    }
    this.adds.get(elementHash)!.add(tag)

    this.vc.increment(this.nodeId)
  }

  remove(element: T): void {
    const elementHash = this.getHash(element)

    // Mark all current adds as removed
    if (this.adds.has(elementHash)) {
      if (!this.removes.has(elementHash)) {
        this.removes.set(elementHash, new Set())
      }
      for (const tag of this.adds.get(elementHash)!) {
        this.removes.get(elementHash)!.add(tag)
      }
    }

    this.vc.increment(this.nodeId)
  }

  contains(element: T): boolean {
    const elementHash = this.getHash(element)
    return this.containsHash(elementHash)
  }

  private containsHash(elementHash: string): boolean {
    if (!this.adds.has(elementHash)) {
      return false
    }

    const adds = this.adds.get(elementHash)!
    const removes = this.removes.get(elementHash) ?? new Set()

    // Element is in set if there's at least one add not matched by remove
    for (const tag of adds) {
      if (!removes.has(tag)) {
        return true
      }
    }

    return false
  }

  /**
   * Returns elements as hashes.
   * Note: In a full implementation, you would store the values alongside the hashes.
   */
  getElements(): string[] {
    const elements: string[] = []
    for (const hash of this.adds.keys()) {
      if (this.containsHash(hash)) {
        elements.push(hash)
      }
    }
    return elements
  }

  merge(other: ORSet<T>): void {
    const otherDict = other.toDict()

    // Merge adds
    for (const [hash, tags] of Object.entries(otherDict.adds)) {
      if (!this.adds.has(hash)) {
        this.adds.set(hash, new Set())
      }
      for (const tag of tags) {
        this.adds.get(hash)!.add(tag)
      }
    }

    // Merge removes
    for (const [hash, tags] of Object.entries(otherDict.removes)) {
      if (!this.removes.has(hash)) {
        this.removes.set(hash, new Set())
      }
      for (const tag of tags) {
        this.removes.get(hash)!.add(tag)
      }
    }

    this.vc.merge(other.vectorClock)
  }

  toDict(): { adds: Record<string, string[]>; removes: Record<string, string[]>; vectorClock: Record<string, number>; nodeId: string } {
    const addsObj: Record<string, string[]> = {}
    for (const [hash, tags] of this.adds.entries()) {
      addsObj[hash] = Array.from(tags)
    }

    const removesObj: Record<string, string[]> = {}
    for (const [hash, tags] of this.removes.entries()) {
      removesObj[hash] = Array.from(tags)
    }

    return {
      adds: addsObj,
      removes: removesObj,
      vectorClock: this.vc.toDict(),
      nodeId: this.nodeId,
    }
  }

  static fromDict<T>(data: { nodeId?: string; vectorClock?: Record<string, number>; adds?: Record<string, string[]>; removes?: Record<string, string[]> }): ORSet<T> {
    const orset = new ORSet<T>()
    orset.nodeId = data.nodeId ?? 'default'
    if (data.vectorClock) {
      orset.vc = VectorClock.fromDict(data.vectorClock)
    }

    if (data.adds) {
      for (const [hash, tags] of Object.entries(data.adds)) {
        orset.adds.set(hash, new Set(tags))
      }
    }

    if (data.removes) {
      for (const [hash, tags] of Object.entries(data.removes)) {
        orset.removes.set(hash, new Set(tags))
      }
    }

    return orset
  }
}

// =============================================================================
// LWW-Map (Last-Writer-Wins Map)
// =============================================================================

export class LWWMap<T> {
  private readonly entries: Map<string, LWWRegister<T | null>>
  private nodeId: string = 'default'
  private vc: VectorClock = new VectorClock()

  constructor() {
    this.entries = new Map()
  }

  setNodeId(nodeId: string): void {
    this.nodeId = nodeId
  }

  get vectorClock(): VectorClock {
    return this.vc
  }

  set(key: string, value: T): void {
    if (!this.entries.has(key)) {
      this.entries.set(key, new LWWRegister<T | null>())
    }
    this.entries.get(key)!.set(value, this.nodeId)
    this.vc.increment(this.nodeId)
  }

  get(key: string): T | null {
    if (!this.entries.has(key)) {
      return null
    }
    return this.entries.get(key)!.get()
  }

  /**
   * Removes null entries (tombstones).
   * Note: In a distributed system, this should be used with caution as it might
   * cause deleted items to reappear if merged with a node that hasn't seen the deletion.
   */
  cleanup(): void {
    for (const [key, reg] of this.entries.entries()) {
      if (reg.get() === null) {
        this.entries.delete(key)
      }
    }
  }

  delete(key: string): void {
    if (this.entries.has(key)) {
      // Set to null with new timestamp (tombstone)
      this.entries.get(key)!.set(null, this.nodeId)
      this.vc.increment(this.nodeId)
    }
  }

  keys(): string[] {
    return Array.from(this.entries.keys())
  }

  merge(other: LWWMap<T>): void {
    const otherDict = other.toDict()

    for (const [key, regData] of Object.entries(otherDict.entries)) {
      const otherRegister = LWWRegister.fromDict<T | null>(regData)
      if (!this.entries.has(key)) {
        this.entries.set(key, otherRegister)
      } else {
        this.entries.get(key)!.merge(otherRegister)
      }
    }
    this.vc.merge(other.vectorClock)
  }

  toDict(): { entries: Record<string, { value: T | null; timestamp: number; nodeId: string; vectorClock: Record<string, number> }>; vectorClock: Record<string, number>; nodeId: string } {
    const entriesObj: Record<string, { value: T | null; timestamp: number; nodeId: string; vectorClock: Record<string, number> }> = {}
    for (const [key, reg] of this.entries.entries()) {
      entriesObj[key] = reg.toDict()
    }

    return {
      entries: entriesObj,
      vectorClock: this.vc.toDict(),
      nodeId: this.nodeId,
    }
  }

  static fromDict<T>(data: { nodeId?: string; vectorClock?: Record<string, number>; entries?: Record<string, { value: T | null; timestamp?: number; nodeId?: string; vectorClock?: Record<string, number> }> }): LWWMap<T> {
    const lwwMap = new LWWMap<T>()
    lwwMap.nodeId = data.nodeId ?? 'default'
    if (data.vectorClock) {
      lwwMap.vc = VectorClock.fromDict(data.vectorClock)
    }

    if (data.entries) {
      for (const [key, regData] of Object.entries(data.entries)) {
        lwwMap.entries.set(key, LWWRegister.fromDict<T | null>(regData))
      }
    }

    return lwwMap
  }
}
