/**
 * Memory block management
 */
import {
  BlockScope,
  InjectionPoint,
  MemoryBlock,
  MemoryBlockSchemaSchema,
  MemoryBlockSchemaType,
  MergeStrategy,
  RetentionPolicy,
} from './types'

export interface CreateBlockOptions {
  label: string
  description?: string
  retentionPolicy?: RetentionPolicy
  mergeStrategy?: MergeStrategy
  injectionPoint?: InjectionPoint
  scope?: BlockScope
  charLimit?: number
  metadata?: Record<string, unknown>
}

function assertWithinCharLimit(
  _schema: MemoryBlockSchemaType,
  _content: string,
): void {
  // Char limit is handled during injection, not at storage time
}

export class BlockManager {
  private readonly schemas: Map<string, MemoryBlockSchemaType> = new Map()
  private readonly blocks: Map<string, MemoryBlock> = new Map()

  /**
   * Create a schema with SDK defaults and runtime validation.
   */
  createSchema(options: CreateBlockOptions): MemoryBlockSchemaType {
    return MemoryBlockSchemaSchema.parse({
      label: options.label,
      description: options.description ?? '',
      retentionPolicy: options.retentionPolicy ?? RetentionPolicy.ShortTerm,
      mergeStrategy: options.mergeStrategy ?? MergeStrategy.Append,
      injectionPoint: options.injectionPoint ?? InjectionPoint.PrePrompt,
      scope: options.scope ?? BlockScope.Session,
      charLimit: options.charLimit ?? 0,
      metadata: options.metadata ?? {},
    })
  }

  /**
   * Register a new block schema.
   */
  register(schema: MemoryBlockSchemaType | CreateBlockOptions): void {
    const parsedSchema = this.isCompleteSchema(schema)
      ? MemoryBlockSchemaSchema.parse(schema)
      : this.createSchema(schema)

    if (this.schemas.has(parsedSchema.label)) {
      throw new Error(`Block schema '${parsedSchema.label}' already registered`)
    }

    this.schemas.set(parsedSchema.label, parsedSchema)
  }

  /**
   * Get a registered schema by label.
   */
  getSchema(label: string): MemoryBlockSchemaType | undefined {
    return this.schemas.get(label)
  }

  /**
   * List all registered schemas.
   */
  listSchemas(): MemoryBlockSchemaType[] {
    return Array.from(this.schemas.values())
  }

  /**
   * Get a block by label.
   */
  get(label: string): MemoryBlock | undefined {
    return this.blocks.get(label)
  }

  /**
   * List all materialized blocks.
   */
  list(): MemoryBlock[] {
    return Array.from(this.blocks.values())
  }

  /**
   * Create a new block instance from a registered schema.
   */
  createBlock(label: string, content: string = ''): MemoryBlock {
    const schema = this.schemas.get(label)
    if (!schema) {
      throw new Error(`Block schema '${label}' not found`)
    }

    assertWithinCharLimit(schema, content)

    const timestamp = new Date().toISOString()
    const block: MemoryBlock = {
      schema,
      content,
      createdAt: timestamp,
      updatedAt: timestamp,
      version: 0,
    }

    this.blocks.set(label, block)
    return block
  }

  /**
   * Update block content according to the schema merge strategy.
   */
  updateContent(label: string, content: string): void {
    const block = this.blocks.get(label)
    if (!block) {
      throw new Error(`Block '${label}' not found`)
    }

    const nextContent =
      block.schema.mergeStrategy === MergeStrategy.Append && block.content
        ? `${block.content}\n${content}`
        : content

    assertWithinCharLimit(block.schema, nextContent)

    block.content = nextContent
    block.updatedAt = new Date().toISOString()
    block.version += 1
  }

  /**
   * Delete a block.
   */
  delete(label: string): boolean {
    return this.blocks.delete(label)
  }

  /**
   * Clear materialized blocks while keeping registered schemas.
   */
  clear(): void {
    this.blocks.clear()
  }

  private isCompleteSchema(
    schema: MemoryBlockSchemaType | CreateBlockOptions,
  ): schema is MemoryBlockSchemaType {
    return (
      'retentionPolicy' in schema &&
      'mergeStrategy' in schema &&
      'injectionPoint' in schema &&
      'scope' in schema &&
      'charLimit' in schema &&
      'metadata' in schema
    )
  }
}

/**
 * Injects memory blocks into user prompts based on their InjectionPoint.
 */
export class MemoryInjector {
  private readonly manager: BlockManager

  constructor(manager: BlockManager) {
    this.manager = manager
  }

  /**
   * Inject memory blocks into a user prompt based on their InjectionPoint.
   * PrePrompt blocks are prepended, PostPrompt blocks are appended.
   * WhisperOnly blocks are NOT included in the returned string.
   */
  inject(userPrompt: string): string {
    const blocks = this.manager.list()
    const prePromptParts: string[] = []
    const postPromptParts: string[] = []

    for (const block of blocks) {
      // Skip blocks with empty or whitespace-only content
      if (!block.content.trim()) {
        continue
      }

      const injectionPoint = block.schema.injectionPoint
      let content = block.content

      // Apply charLimit truncation if charLimit > 0 and content exceeds it
      if (
        block.schema.charLimit > 0 &&
        content.length > block.schema.charLimit
      ) {
        content = content.substring(0, block.schema.charLimit - 1) + '…'
      }

      if (injectionPoint === InjectionPoint.PrePrompt) {
        prePromptParts.push(content)
      } else if (injectionPoint === InjectionPoint.PostPrompt) {
        postPromptParts.push(content)
      }
      // WhisperOnly: skip in inject()
    }

    const prePromptText = prePromptParts.join('\n')
    const postPromptText = postPromptParts.join('\n')

    let result = userPrompt
    if (prePromptText) {
      result = `${prePromptText}\n\n${result}`
    }
    if (postPromptText) {
      result = `${result}\n\n${postPromptText}`
    }
    return result
  }

  /**
   * Get WhisperOnly block content separately (not injected into prompt).
   */
  getWhisperOnly(): string {
    const blocks = this.manager.list()
    const whisperParts: string[] = []

    for (const block of blocks) {
      // Only WhisperOnly blocks
      if (block.schema.injectionPoint !== InjectionPoint.WhisperOnly) {
        continue
      }

      // Skip blocks with empty or whitespace-only content
      if (!block.content.trim()) {
        continue
      }

      let content = block.content

      // Apply charLimit truncation if charLimit > 0 and content exceeds it
      if (
        block.schema.charLimit > 0 &&
        content.length > block.schema.charLimit
      ) {
        content = content.substring(0, block.schema.charLimit - 1) + '…'
      }

      whisperParts.push(content)
    }

    return whisperParts.join('\n')
  }
}
