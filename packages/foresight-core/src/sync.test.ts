import { describe, test, expect, beforeEach, vi } from 'vitest';
import { SyncManager, InMemoryOperationStorage } from './sync';
import { SyncStatus, OperationType } from './types';

describe('SyncManager', () => {
  let manager: SyncManager;
  let storage: InMemoryOperationStorage;

  beforeEach(() => {
    storage = new InMemoryOperationStorage();
    manager = new SyncManager({ storage, nodeId: 'test-node' });
  });

  test('enqueueOperation', async () => {
    const opId = await manager.enqueueOperation({
      type: OperationType.Create,
      entityType: 'memory',
      entityId: 'mem-123',
      payload: { content: 'test' },
    });

    expect(opId).toBeDefined();
    expect(await storage.count()).toBe(1);
  });

  test('sync success', async () => {
    await manager.enqueueOperation({
      type: OperationType.Create,
      entityType: 'memory',
      entityId: 'mem-123',
      payload: {},
    });

    const syncCallback = vi.fn().mockResolvedValue(true);
    const progressManager = new SyncManager({ storage, syncCallback });
    
    const progress = await progressManager.sync();
    expect(progress.status).toBe(SyncStatus.Idle);
    expect(await storage.count()).toBe(0);
    expect(syncCallback).toHaveBeenCalled();
  });

  test('sync failure and retry', async () => {
    await manager.enqueueOperation({
      type: OperationType.Create,
      entityType: 'memory',
      entityId: 'mem-123',
      payload: {},
    });

    const syncCallback = vi.fn().mockRejectedValue(new Error('Network error'));
    const retryManager = new SyncManager({ storage, syncCallback, maxRetries: 3 });

    const progress = await retryManager.sync();
    expect(progress.status).toBe(SyncStatus.Error);
    expect(await storage.count()).toBe(1);
    
    const ops = await storage.peek();
    expect(ops[0].retryCount).toBe(1);
  });

  test('offline status', async () => {
    manager.setOnline(false);
    const progress = await manager.getProgress();
    expect(progress.status).toBe(SyncStatus.Offline);

    await manager.enqueueOperation({
      type: OperationType.Create,
      entityType: 'memory',
      entityId: 'mem-123',
      payload: {},
    });

    // Should not sync when offline
    const syncResult = await manager.sync();
    expect(syncResult.status).toBe(SyncStatus.Offline);
    expect(await storage.count()).toBe(1);
  });
});
