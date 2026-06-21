/**
 * Foresight client for memory operations
 */
import { z } from 'zod'

import { SyncManager } from './sync'
import {
  ArchiveMemoryRequest,
  DeleteMemoryRequest,
  GetMemoryRequest,
  ListMemoriesRequest,
  MemoryObject,
  MemoryObjectSchema,
  MemoryScope,
  MemoryStatus,
  OperationType,
  QueryMemoriesRequest,
  RetentionPolicy,
  StoreMemoryRequest,
  StoreMemoryResponse,
  SynthesizeMemoriesRequest,
  UpdateMemoryRequest,
} from './types'

export interface RetryOptions {
  /** Total attempts including the initial request. */
  attempts?: number
  /** Initial retry delay in milliseconds. */
  initialDelayMs?: number
  /** Maximum retry delay in milliseconds. */
  maxDelayMs?: number
  /** Exponential backoff multiplier. */
  backoffFactor?: number
}

export type FetchLike = (
  input: string | URL | Request,
  init?: RequestInit,
) => Promise<Response>

export interface ForesightClientOptions {
  /** Base URL for MCP server or gateway */
  serverUrl?: string
  /** User ID for memory operations */
  userId?: string
  /** Bank ID (default: "default") */
  bankId?: string
  /** Timeout in milliseconds */
  timeout?: number
  /** Optional sync manager for offline support */
  syncManager?: SyncManager
  /** Fetch implementation for non-browser runtimes and tests */
  fetch?: FetchLike
  /** Retry/backoff behavior for transient HTTP failures */
  retry?: RetryOptions
}

const StoreMemoryResponseSchema = z.object({
  id: z.string(),
  content: z.string(),
  decision: z.string(),
  reason: z.string(),
  tags: z.array(z.string()).optional(),
  anomalyDetected: z.boolean().optional(),
})

const MemoryListSchema = z.array(MemoryObjectSchema)

const MemoryStatusSchema = z.object({
  status: z.string(),
  database: z.string(),
  bankId: z.string(),
  userId: z.string(),
  memoryCount: z.number(),
  crisisSignals: z.number(),
  byScope: z.record(z.string(), z.number()),
})

const SynthesizeMemoriesResponseSchema = z.object({
  mergedIds: z.array(z.string()),
  newMemoryId: z.string(),
  compressionRatio: z.number(),
  stanceShifts: z.array(z.unknown()),
})

const EmptyResponseSchema = z.void().or(z.object({}).transform(() => undefined))

type SynthesizeMemoriesResponse = z.infer<
  typeof SynthesizeMemoriesResponseSchema
>

type OperationName =
  | 'store_memory'
  | 'query_memories'
  | 'list_memories'
  | 'get_memory'
  | 'update_memory'
  | 'delete_memory'
  | 'synthesize_memories'
  | 'archive_memory'
  | 'memory_status'

interface RequestOptions<TOutput> {
  operation: OperationName
  payload: Record<string, unknown>
  schema: z.ZodType<TOutput>
}

interface NormalizedRetryOptions {
  attempts: number
  initialDelayMs: number
  maxDelayMs: number
  backoffFactor: number
}

export class ForesightClient {
  public readonly serverUrl?: string
  public readonly userId: string
  public readonly bankId: string
  public readonly timeout: number
  public readonly sync?: SyncManager

  private readonly fetchImpl: FetchLike
  private readonly retry: NormalizedRetryOptions

  constructor(options: ForesightClientOptions = {}) {
    this.serverUrl = options.serverUrl
    this.userId = options.userId ?? process.env.FORESIGHT_USER_ID ?? 'default'
    this.bankId = options.bankId ?? process.env.FORESIGHT_BANK_ID ?? 'default'
    this.timeout = options.timeout ?? 30000
    this.sync = options.syncManager
    this.fetchImpl = options.fetch ?? fetch
    this.retry = {
      attempts: options.retry?.attempts ?? 3,
      initialDelayMs: options.retry?.initialDelayMs ?? 100,
      maxDelayMs: options.retry?.maxDelayMs ?? 2000,
      backoffFactor: options.retry?.backoffFactor ?? 2,
    }
  }

  /**
   * Store a new memory
   */
  async storeMemory(
    content: string,
    options: Partial<{
      category: string
      scope: MemoryScope
      retention: RetentionPolicy
      offline: boolean
    }> = {},
  ): Promise<StoreMemoryResponse> {
    if (options.offline && this.sync) {
      const opId = await this.sync.enqueueOperation({
        type: OperationType.Create,
        entityType: 'memory',
        entityId: 'pending', // Will be assigned by server
        payload: { content, ...options },
      })
      return {
        id: opId,
        content,
        decision: 'queued',
        reason: 'Client in offline mode, operation queued',
      }
    }

    const request: StoreMemoryRequest = {
      content,
      category: options.category,
      scope: options.scope,
      retention: options.retention,
      userId: this.userId,
    }

    return this.request({
      operation: 'store_memory',
      payload: this.toSnakePayload(request),
      schema: StoreMemoryResponseSchema,
    })
  }

  /**
   * Query memories by content
   */
  async queryMemories(
    query: string,
    options: { limit?: number; offset?: number } = {},
  ): Promise<MemoryObject[]> {
    const request: QueryMemoriesRequest = {
      query,
      userId: this.userId,
      limit: options.limit,
      offset: options.offset,
    }

    return this.request({
      operation: 'query_memories',
      payload: this.toSnakePayload(request),
      schema: MemoryListSchema,
    })
  }

  /**
   * List all memories
   */
  async listMemories(
    options: {
      limit?: number
      offset?: number
    } = {},
  ): Promise<MemoryObject[]> {
    const request: ListMemoriesRequest = {
      userId: this.userId,
      limit: options.limit,
      offset: options.offset,
    }

    return this.request({
      operation: 'list_memories',
      payload: this.toSnakePayload(request),
      schema: MemoryListSchema,
    })
  }

  /**
   * Get a specific memory by ID
   */
  async getMemory(memoryId: string): Promise<MemoryObject> {
    const request: GetMemoryRequest = { memoryId, userId: this.userId }

    return this.request({
      operation: 'get_memory',
      payload: this.toSnakePayload(request),
      schema: MemoryObjectSchema,
    })
  }

  /**
   * Update an existing memory
   */
  async updateMemory(
    memoryId: string,
    updates: Partial<{
      content: string
      category: string
      scope: string
      retention: string
      tags: string[]
    }>,
  ): Promise<void> {
    const request: UpdateMemoryRequest = {
      memoryId,
      ...updates,
      userId: this.userId,
    }

    return this.request({
      operation: 'update_memory',
      payload: this.toSnakePayload(request),
      schema: EmptyResponseSchema,
    })
  }

  /**
   * Delete a memory
   */
  async deleteMemory(memoryId: string): Promise<void> {
    const request: DeleteMemoryRequest = { memoryId, userId: this.userId }

    return this.request({
      operation: 'delete_memory',
      payload: this.toSnakePayload(request),
      schema: EmptyResponseSchema,
    })
  }

  /**
   * Run synthesis on all memories
   */
  async synthesizeMemories(): Promise<SynthesizeMemoriesResponse> {
    const request: SynthesizeMemoriesRequest = { userId: this.userId }

    return this.request({
      operation: 'synthesize_memories',
      payload: this.toSnakePayload(request),
      schema: SynthesizeMemoriesResponseSchema,
    })
  }

  /**
   * Archive a memory to a ghost node
   */
  async archiveMemory(memoryId: string): Promise<void> {
    const request: ArchiveMemoryRequest = { memoryId, userId: this.userId }

    return this.request({
      operation: 'archive_memory',
      payload: this.toSnakePayload(request),
      schema: EmptyResponseSchema,
    })
  }

  /**
   * Get system status
   */
  async getStatus(): Promise<MemoryStatus> {
    return this.request({
      operation: 'memory_status',
      payload: { user_id: this.userId },
      schema: MemoryStatusSchema,
    })
  }

  private async request<TOutput>({
    operation,
    payload,
    schema,
  }: RequestOptions<TOutput>): Promise<TOutput> {
    if (!this.serverUrl) {
      throw new Error('ForesightClient requires serverUrl for online requests')
    }

    const url = new URL(
      `tools/${operation}`,
      this.withTrailingSlash(this.serverUrl),
    )
    const attempts = Math.max(1, this.retry.attempts)
    let lastError: unknown

    for (let attempt = 1; attempt <= attempts; attempt++) {
      try {
        const data = await this.fetchJson(url, payload)
        return schema.parse(data)
      } catch (error) {
        lastError = error
        if (attempt === attempts || !this.shouldRetry(error)) {
          break
        }
        await this.delay(this.delayForAttempt(attempt))
      }
    }

    throw lastError instanceof Error ? lastError : new Error(String(lastError))
  }

  private async fetchJson(
    url: URL,
    payload: Record<string, unknown>,
  ): Promise<unknown> {
    const controller = new AbortController()
    const timeoutId = setTimeout(() => controller.abort(), this.timeout)

    try {
      const response = await this.fetchImpl(url.toString(), {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(payload),
        signal: controller.signal,
      })

      const data = await this.readResponse(response)
      if (!response.ok) {
        throw new ForesightHttpError(response.status, data)
      }
      return data
    } finally {
      clearTimeout(timeoutId)
    }
  }

  private async readResponse(response: Response): Promise<unknown> {
    const text = await response.text()
    if (!text) {
      return undefined
    }

    const parsed = JSON.parse(text) as unknown
    if (typeof parsed === 'string') {
      return JSON.parse(parsed) as unknown
    }
    return parsed
  }

  private shouldRetry(error: unknown): boolean {
    if (error instanceof ForesightHttpError) {
      return error.status === 408 || error.status === 429 || error.status >= 500
    }
    return error instanceof TypeError || error instanceof DOMException
  }

  private delayForAttempt(attempt: number): number {
    const exponentialDelay =
      this.retry.initialDelayMs * this.retry.backoffFactor ** (attempt - 1)
    return Math.min(exponentialDelay, this.retry.maxDelayMs)
  }

  private async delay(ms: number): Promise<void> {
    if (ms <= 0) {
      return
    }
    await new Promise((resolve) => setTimeout(resolve, ms))
  }

  private toSnakePayload(input: object): Record<string, unknown> {
    return Object.fromEntries(
      Object.entries(input)
        .filter(([, value]) => value !== undefined)
        .map(([key, value]) => [this.toSnakeCase(key), value]),
    )
  }

  private toSnakeCase(value: string): string {
    return value.replace(/[A-Z]/g, (letter) => `_${letter.toLowerCase()}`)
  }

  private withTrailingSlash(value: string): string {
    return value.endsWith('/') ? value : `${value}/`
  }
}

export class ForesightHttpError extends Error {
  constructor(
    public readonly status: number,
    public readonly body: unknown,
  ) {
    super(`Foresight request failed with HTTP ${status}`)
  }
}
