import { describe, expect, it, vi } from 'vitest'

import { ForesightClient, FetchLike, ForesightHttpError } from './client'
import { MemoryScope, RetentionPolicy } from './types'

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { 'content-type': 'application/json', ...init.headers },
  })
}

describe('ForesightClient', () => {
  it('posts snake_case payloads and validates store responses', async () => {
    const fetchMock = vi.fn<FetchLike>().mockResolvedValue(
      jsonResponse({
        id: 'mem_1',
        content: 'Use TypeScript',
        decision: 'stored',
        reason: 'new memory',
        tags: ['preference'],
      }),
    )
    const client = new ForesightClient({
      serverUrl: 'https://foresight.example/mcp',
      userId: 'user_1',
      fetch: fetchMock,
      retry: { attempts: 1 },
    })

    const result = await client.storeMemory('Use TypeScript', {
      category: 'preference',
      scope: MemoryScope.Fact,
      retention: RetentionPolicy.LongTerm,
    })

    expect(result.id).toBe('mem_1')
    expect(fetchMock).toHaveBeenCalledWith(
      'https://foresight.example/mcp/tools/store_memory',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          content: 'Use TypeScript',
          category: 'preference',
          scope: MemoryScope.Fact,
          retention: RetentionPolicy.LongTerm,
          user_id: 'user_1',
        }),
      }),
    )
  })

  it('retries transient failures with configurable backoff', async () => {
    const fetchMock = vi
      .fn<FetchLike>()
      .mockResolvedValueOnce(jsonResponse({ error: 'busy' }, { status: 503 }))
      .mockResolvedValueOnce(
        jsonResponse({
          status: 'ok',
          database: 'sqlite',
          bankId: 'default',
          userId: 'user_1',
          memoryCount: 2,
          crisisSignals: 0,
          byScope: { fact: 2 },
        }),
      )
    const client = new ForesightClient({
      serverUrl: 'https://foresight.example/mcp',
      userId: 'user_1',
      fetch: fetchMock,
      retry: { attempts: 2, initialDelayMs: 0 },
    })

    await expect(client.getStatus()).resolves.toMatchObject({
      status: 'ok',
      memoryCount: 2,
    })
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('does not retry validation failures', async () => {
    const fetchMock = vi.fn<FetchLike>().mockResolvedValue(
      jsonResponse({
        id: 'mem_1',
        content: 'missing required fields',
      }),
    )
    const client = new ForesightClient({
      serverUrl: 'https://foresight.example/mcp',
      fetch: fetchMock,
      retry: { attempts: 3, initialDelayMs: 0 },
    })

    await expect(client.storeMemory('invalid')).rejects.toThrow()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('throws typed HTTP errors for non-retryable failures', async () => {
    const fetchMock = vi
      .fn<FetchLike>()
      .mockResolvedValue(
        jsonResponse({ error: 'bad request' }, { status: 400 }),
      )
    const client = new ForesightClient({
      serverUrl: 'https://foresight.example/mcp',
      fetch: fetchMock,
      retry: { attempts: 3, initialDelayMs: 0 },
    })

    await expect(client.getStatus()).rejects.toBeInstanceOf(ForesightHttpError)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})
