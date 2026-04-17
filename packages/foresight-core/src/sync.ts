/**
 * Offline-First Synchronization
 */
import {
  SyncStatus,
  OperationType,
  Operation,
  SyncProgress,
} from './types';
import { VectorClock } from './crdt';

// =============================================================================
// Storage Interface
// =============================================================================

export interface OperationStorage {
  enqueue(operation: Operation): Promise<void>;
  dequeue(): Promise<Operation | null>;
  remove(operationId: string): Promise<void>;
  peek(): Promise<Operation[]>;
  count(): Promise<number>;
  clear(): Promise<void>;
}

/**
 * In-memory implementation of operation storage.
 */
export class InMemoryOperationStorage implements OperationStorage {
  private queue: Operation[] = [];

  async enqueue(operation: Operation): Promise<void> {
    const index = this.queue.findIndex(op => op.id === operation.id);
    if (index !== -1) {
      this.queue[index] = operation;
    } else {
      this.queue.push(operation);
      // Sort by createdAt
      this.queue.sort((a, b) => a.createdAt.localeCompare(b.createdAt));
    }
  }

  async dequeue(): Promise<Operation | null> {
    return this.queue.shift() || null;
  }

  async remove(operationId: string): Promise<void> {
    this.queue = this.queue.filter(op => op.id !== operationId);
  }

  async peek(): Promise<Operation[]> {
    return [...this.queue];
  }

  async count(): Promise<number> {
    return this.queue.length;
  }

  async clear(): Promise<void> {
    this.queue = [];
  }
}

/**
 * LocalStorage implementation of operation storage for browser environments.
 */
export class LocalStorageOperationStorage implements OperationStorage {
  private key: string;

  constructor(key: string = 'foresight_operations') {
    this.key = key;
  }

  private async getQueue(): Promise<Operation[]> {
    if (typeof localStorage === 'undefined') return [];
    const data = localStorage.getItem(this.key);
    return data ? JSON.parse(data) : [];
  }

  private async saveQueue(queue: Operation[]): Promise<void> {
    if (typeof localStorage === 'undefined') return;
    localStorage.setItem(this.key, JSON.stringify(queue));
  }

  async enqueue(operation: Operation): Promise<void> {
    const queue = await this.getQueue();
    const index = queue.findIndex(op => op.id === operation.id);
    if (index !== -1) {
      queue[index] = operation;
    } else {
      queue.push(operation);
      queue.sort((a, b) => a.createdAt.localeCompare(b.createdAt));
    }
    await this.saveQueue(queue);
  }

  async dequeue(): Promise<Operation | null> {
    const queue = await this.getQueue();
    const op = queue.shift() || null;
    await this.saveQueue(queue);
    return op;
  }

  async remove(operationId: string): Promise<void> {
    let queue = await this.getQueue();
    queue = queue.filter(op => op.id !== operationId);
    await this.saveQueue(queue);
  }

  async peek(): Promise<Operation[]> {
    return this.getQueue();
  }

  async count(): Promise<number> {
    const queue = await this.getQueue();
    return queue.length;
  }

  async clear(): Promise<void> {
    if (typeof localStorage === 'undefined') return;
    localStorage.removeItem(this.key);
  }
}

// =============================================================================
// Sync Manager
// =============================================================================

export type SyncCallback = (operation: Operation) => Promise<boolean>;
export type ProgressCallback = (progress: SyncProgress) => void;

export class SyncManager {
  private nodeId: string;
  private maxRetries: number;
  private retryDelay: number;
  private storage: OperationStorage;
  private syncCallback?: SyncCallback;
  private status: SyncStatus = SyncStatus.Idle;
  private errors: string[] = [];
  private lastSync?: string;
  private progressCallbacks: ProgressCallback[] = [];

  constructor(options: {
    nodeId?: string;
    maxRetries?: number;
    retryDelay?: number;
    storage?: OperationStorage;
    syncCallback?: SyncCallback;
  } = {}) {
    this.nodeId = options.nodeId || 'default';
    this.maxRetries = options.maxRetries || 3;
    this.retryDelay = options.retryDelay || 1.0;
    this.storage = options.storage || new InMemoryOperationStorage();
    this.syncCallback = options.syncCallback;
  }

  setOnline(online: boolean): void {
    if (!online) {
      this.status = SyncStatus.Offline;
    } else if (this.status === SyncStatus.Offline) {
      this.status = SyncStatus.Idle;
    }
    this.notifyProgress();
  }

  async enqueueOperation(params: {
    type: OperationType;
    entityType: string;
    entityId: string;
    payload: Record<string, any>;
  }): Promise<string> {
    const id = typeof crypto !== 'undefined' && crypto.randomUUID 
      ? crypto.randomUUID() 
      : Math.random().toString(36).substring(2, 15) + Math.random().toString(36).substring(2, 15);
    const vc = new VectorClock();
    vc.increment(this.nodeId);

    const operation: Operation = {
      id,
      type: params.type,
      entityType: params.entityType,
      entityId: params.entityId,
      payload: params.payload,
      createdAt: new Date().toISOString(),
      retryCount: 0,
      vectorClock: vc.toDict(),
    };

    await this.storage.enqueue(operation);
    this.notifyProgress();
    return id;
  }

  async sync(): Promise<SyncProgress> {
    if (this.status === SyncStatus.Syncing) {
      return this.getProgress();
    }

    if (this.status === SyncStatus.Offline) {
      return this.getProgress();
    }

    this.status = SyncStatus.Syncing;
    this.notifyProgress();

    const pending = await this.storage.peek();
    const errors: string[] = [];

    for (const operation of pending) {
      if (operation.retryCount >= this.maxRetries) {
        errors.push(`Max retries exceeded for ${operation.id}`);
        await this.storage.remove(operation.id);
        continue;
      }

      try {
        if (this.syncCallback) {
          const success = await this.syncCallback(operation);
          if (success) {
            await this.storage.remove(operation.id);
          } else {
            throw new Error('Sync callback returned false');
          }
        } else {
          // Simulated success
          await this.storage.remove(operation.id);
        }
        this.lastSync = new Date().toISOString();
      } catch (e: any) {
        operation.retryCount += 1;
        operation.lastAttempt = new Date().toISOString();
        await this.storage.enqueue(operation);
        errors.push(`Operation ${operation.id} failed: ${e.message}`);
      }
    }

    this.status = errors.length === 0 ? SyncStatus.Idle : SyncStatus.Error;
    this.errors = errors;
    this.notifyProgress();

    return this.getProgress();
  }

  async getProgress(): Promise<SyncProgress> {
    const pendingCount = await this.storage.count();
    return {
      status: this.status,
      totalOperations: pendingCount,
      pendingOperations: pendingCount,
      syncedOperations: 0,
      errors: this.errors,
      lastSync: this.lastSync,
    };
  }

  onProgress(callback: ProgressCallback): void {
    this.progressCallbacks.push(callback);
  }

  private async notifyProgress(): Promise<void> {
    const progress = await this.getProgress();
    for (const callback of this.progressCallbacks) {
      try {
        callback(progress);
      } catch (e) {
        console.error('Progress callback error:', e);
      }
    }
  }

  async getStatus(): Promise<any> {
    return this.getProgress();
  }
}

// =============================================================================
// Global Sync Manager
// =============================================================================

let globalSyncManager: SyncManager | null = null;

export function getSyncManager(nodeId: string = 'default'): SyncManager {
  if (!globalSyncManager) {
    globalSyncManager = new SyncManager({ nodeId });
  }
  return globalSyncManager;
}

export function resetSyncManager(): void {
  globalSyncManager = null;
}
