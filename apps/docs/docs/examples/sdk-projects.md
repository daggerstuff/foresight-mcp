---
sidebar_label: SDK Projects
title: React, Next.js, and Node CLI Examples
---

# SDK Project Examples

These examples show the pnpm-first TypeScript SDK in three common project
shapes.

## React app

```typescript
import { useEffect, useState } from 'react'
import { ForesightClient, MemoryScope, RetentionPolicy } from '@foresight/core'

const client = new ForesightClient({
  serverUrl: import.meta.env.VITE_FORESIGHT_URL,
  userId: 'demo-user',
  retry: { attempts: 3, initialDelayMs: 100 },
})

export function MemoryPanel() {
  const [count, setCount] = useState(0)

  useEffect(() => {
    void client.getStatus().then((status) => setCount(status.memoryCount))
  }, [])

  return (
    <button
      onClick={async () => {
        await client.storeMemory('Prefers React for UI work', {
          scope: MemoryScope.Fact,
          retention: RetentionPolicy.LongTerm,
          category: 'preference',
        })
      }}
    >
      Memories stored: {count}
    </button>
  )
}
```

## Next.js route handler

```typescript
import { NextResponse } from 'next/server'
import { ForesightClient } from '@foresight/core'

const client = new ForesightClient({
  serverUrl: process.env.FORESIGHT_URL,
  userId: process.env.FORESIGHT_USER_ID,
})

export async function GET() {
  const memories = await client.queryMemories('context blocks', { limit: 5 })
  return NextResponse.json({ memories })
}
```

## Node CLI

```typescript
#!/usr/bin/env node
import { ForesightClient } from '@foresight/core'

const client = new ForesightClient({
  serverUrl: process.env.FORESIGHT_URL,
  userId: process.env.FORESIGHT_USER_ID,
})

const status = await client.getStatus()
console.log(JSON.stringify(status, null, 2))
```

## Packaging check

For local package verification, use pnpm:

```bash
cd packages/foresight-core
pnpm build
pnpm pack --pack-destination /tmp/foresight-core-pack
```

## Related

- [Basic Usage](./basic-usage)
- [TypeScript API Reference](../api/typescript-api)
