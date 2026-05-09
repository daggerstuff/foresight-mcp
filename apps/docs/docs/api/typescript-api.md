 ---
sidebar_label: TypeScript API
title: TypeScript API Reference
---

# TypeScript API Reference

Complete TypeScript SDK documentation.

## ForesightClient

### Constructor

```typescript
constructor(options?: ForesightClientOptions)
```

**Options:**

```typescript
interface ForesightClientOptions {
  serverUrl?: string
  userId?: string
  bankId?: string
  timeout?: number
}
```

### Methods

#### storeMemory

```typescript
async storeMemory(
  content: string,
  options?: {
    category?: string;
    scope?: MemoryScope;
    retention?: RetentionPolicy;
  }
): Promise<StoreMemoryResponse>
```

#### queryMemories

```typescript
async queryMemories(
  query: string,
  options?: { limit?: number; offset?: number }
): Promise<MemoryObject[]>
```

#### listMemories

```typescript
async listMemories(options?: {
  limit?: number;
  offset?: number;
}): Promise<MemoryObject[]>
```

#### getMemory

```typescript
async getMemory(memoryId: string): Promise<MemoryObject>
```

#### updateMemory

```typescript
async updateMemory(
  memoryId: string,
  updates: Partial<{
    content: string;
    category: string;
    scope: string;
    retention: string;
    tags: string[];
  }>
): Promise<void>
```

#### deleteMemory

```typescript
async deleteMemory(memoryId: string): Promise<void>
```

## BlockManager

```typescript
class BlockManager {
  register(schema: MemoryBlockSchema): void
  get(label: string): MemoryBlock | undefined
  list(): MemoryBlock[]
  createBlock(label: string, content: string): MemoryBlock
  updateContent(label: string, content: string): void
  delete(label: string): boolean
}
```

## HookManager

```typescript
class HookManager {
  registerHook(options: RegisterHookOptions): Promise<HookRegistration>
  listHooks(): Promise<HookRegistration[]>
  unregisterHook(hookId: string): Promise<void>
}
```

## Types

### MemoryScope

```typescript
enum MemoryScope {
  Session = 'session',
  Arc = 'arc',
  Trait = 'trait',
  Fact = 'fact',
}
```

### RetentionPolicy

```typescript
enum RetentionPolicy {
  Ephemeral = 'ephemeral',
  ShortTerm = 'short_term',
  LongTerm = 'long_term',
  Permanent = 'permanent',
}
```

### EventType

```typescript
enum EventType {
  MemoryStored = 'memory.stored',
  MemoryRetrieved = 'memory.retrieved',
  MemoryUpdated = 'memory.updated',
  MemoryDeleted = 'memory.deleted',
  BlockCreated = 'block.created',
  BlockUpdated = 'block.updated',
  BlockDeleted = 'block.deleted',
  AnomalyDetected = 'anomaly.detected',
  SystemError = 'system.error',
}
```

## Related

- [Python API](./python-api)
- [CLI Reference](./cli-reference)
