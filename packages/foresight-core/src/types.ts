/**
 * Core type definitions for Foresight Memory Architecture
 */
import { z } from 'zod';

// ============================================================================
// Enums
// ============================================================================

export enum MemoryScope {
  Session = 'session',
  Arc = 'arc',
  Trait = 'trait',
  Fact = 'fact',
}

export enum RetentionPolicy {
  Ephemeral = 'ephemeral',
  ShortTerm = 'short_term',
  LongTerm = 'long_term',
  Permanent = 'permanent',
}

export enum MergeStrategy {
  Append = 'append',
  Replace = 'replace',
  Synthesize = 'synthesize',
}

export enum InjectionPoint {
  PrePrompt = 'pre_prompt',
  PostPrompt = 'post_prompt',
  WhisperOnly = 'whisper_only',
}

export enum BlockScope {
  Global = 'global',
  Project = 'project',
  Session = 'session',
}

export enum EventType {
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

export enum HookType {
  Callable = 'callable',
  HTTP = 'http',
  Async = 'async',
}

export enum SyncStatus {
  Idle = 'idle',
  Syncing = 'syncing',
  Offline = 'offline',
  Error = 'error',
}

export enum OperationType {
  Create = 'create',
  Update = 'update',
  Delete = 'delete',
}

// ============================================================================
// Schemas
// ============================================================================

export const EmotionalMetadataSchema = z.object({
  valence: z.number().optional(),
  arousal: z.number().optional(),
  dominance: z.number().optional(),
  primaryEmotion: z.string().optional(),
  intensity: z.number().optional(),
});

export const EmpathyMetricsSchema = z.object({
  reciprocity: z.number().optional(),
  validationAccuracy: z.number().optional(),
  resistanceLevel: z.number().optional(),
});

export const MemoryObjectSchema = z.object({
  id: z.string(),
  content: z.string(),
  scope: z.nativeEnum(MemoryScope),
  retention: z.nativeEnum(RetentionPolicy),
  category: z.string(),
  userId: z.string(),
  bankId: z.string(),
  createdAt: z.string(),
  updatedAt: z.string().optional(),
  tags: z.array(z.string()),
  emotionalContext: EmotionalMetadataSchema.optional(),
  metrics: EmpathyMetricsSchema.optional(),
  vectorId: z.string().optional(),
  gist: z.string().optional(),
  isGhost: z.boolean(),
  synthesizedFrom: z.array(z.string()),
});

export const MemoryBlockSchemaSchema = z.object({
  label: z.string(),
  description: z.string(),
  retentionPolicy: z.nativeEnum(RetentionPolicy),
  mergeStrategy: z.nativeEnum(MergeStrategy),
  injectionPoint: z.nativeEnum(InjectionPoint),
  scope: z.nativeEnum(BlockScope),
  charLimit: z.number(),
  metadata: z.record(z.unknown()),
});

export const MemoryBlockSchema = z.object({
  schema: MemoryBlockSchemaSchema,
  content: z.string(),
  createdAt: z.string(),
  updatedAt: z.string(),
  version: z.number(),
});

export const HookRegistrationSchema = z.object({
  id: z.string(),
  name: z.string(),
  eventType: z.nativeEnum(EventType),
  hookType: z.nativeEnum(HookType),
  handler: z.string(),
  condition: z.string().optional(),
  retryCount: z.number(),
  timeout: z.number(),
  metadata: z.record(z.unknown()),
  enabled: z.boolean(),
  createdAt: z.string(),
});

export const EventSchema = z.object({
  id: z.string(),
  eventType: z.nativeEnum(EventType),
  timestamp: z.string(),
  actor: z.string(),
  entityId: z.string(),
  payload: z.record(z.unknown()),
  metadata: z.record(z.unknown()),
});

export const VectorClockSchema = z.record(z.number());

export const OperationSchema = z.object({
  id: z.string(),
  type: z.nativeEnum(OperationType),
  entityType: z.string(),
  entityId: z.string(),
  payload: z.record(z.unknown()),
  createdAt: z.string(),
  retryCount: z.number(),
  lastAttempt: z.string().optional(),
  vectorClock: VectorClockSchema,
});

export const SyncProgressSchema = z.object({
  status: z.nativeEnum(SyncStatus),
  totalOperations: z.number(),
  pendingOperations: z.number(),
  syncedOperations: z.number(),
  errors: z.array(z.string()),
  lastSync: z.string().optional(),
});

// ============================================================================
// Type exports
// ============================================================================

export type EmotionalMetadata = z.infer<typeof EmotionalMetadataSchema>;
export type EmpathyMetrics = z.infer<typeof EmpathyMetricsSchema>;
export type MemoryObject = z.infer<typeof MemoryObjectSchema>;
export type MemoryBlockSchemaType = z.infer<typeof MemoryBlockSchemaSchema>;
export type MemoryBlock = z.infer<typeof MemoryBlockSchema>;
export type HookRegistration = z.infer<typeof HookRegistrationSchema>;
export type Event = z.infer<typeof EventSchema>;
export type VectorClockType = z.infer<typeof VectorClockSchema>;
export type Operation = z.infer<typeof OperationSchema>;
export type SyncProgress = z.infer<typeof SyncProgressSchema>;

// ============================================================================
// API Response Types
// ============================================================================

export interface StoreMemoryRequest {
  content: string;
  category?: string;
  scope?: MemoryScope;
  retention?: RetentionPolicy;
  emotionalContext?: EmotionalMetadata;
  metrics?: EmpathyMetrics;
  userId?: string;
}

export interface StoreMemoryResponse {
  id: string;
  content: string;
  decision: string;
  reason: string;
  tags?: string[];
  anomalyDetected?: boolean;
}

export interface QueryMemoriesRequest {
  query: string;
  userId?: string;
  limit?: number;
  offset?: number;
}

export interface ListMemoriesRequest {
  userId?: string;
  limit?: number;
  offset?: number;
}

export interface GetMemoryRequest {
  memoryId: string;
  userId?: string;
}

export interface UpdateMemoryRequest {
  memoryId: string;
  content?: string;
  category?: string;
  scope?: string;
  retention?: string;
  tags?: string[];
  userId?: string;
}

export interface DeleteMemoryRequest {
  memoryId: string;
  userId?: string;
}

export interface SynthesizeMemoriesRequest {
  userId?: string;
}

export interface ArchiveMemoryRequest {
  memoryId: string;
  userId?: string;
}

export interface RegisterHookRequest {
  name: string;
  eventType: EventType;
  url: string;
  retryCount?: number;
  timeout?: number;
}

export interface ListHooksResponse {
  hooks: HookRegistration[];
}

export interface MemoryStatus {
  status: string;
  database: string;
  bankId: string;
  userId: string;
  memoryCount: number;
  crisisSignals: number;
  byScope: Record<string, number>;
}
