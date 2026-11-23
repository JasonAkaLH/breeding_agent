import { describe, expect, it, vi } from 'vitest';
import { ConversationModelSettingsStore } from './conversationModelSettings';
import type { ConversationModelSelection, ConversationModelSettings } from '../api/types';

const defaults: ConversationModelSelection = { model_edition: 'default', reasoning_effort: 'high', deep_thinking: false };
const changed = { ...defaults, model_edition: 'pro', reasoning_effort: 'max', deep_thinking: true };
const saved = (selection = defaults, revision = 1): ConversationModelSettings => ({ ...selection, revision, updated_at: '2026-11-23' });
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => { resolve = r; });
  return { promise, resolve };
}
function setup() {
  const db = new Map<string, ConversationModelSettings>();
  const api = {
    getConversationModelSettings: vi.fn(async (id: string) => ({ conversation_id: id, settings: db.get(id) ?? null })),
    putConversationModelSettings: vi.fn(async (id: string, selection: ConversationModelSelection, revision: number) => {
      if ((db.get(id)?.revision ?? 0) !== revision) throw new Error('conflict');
      const value = saved(selection, revision + 1);
      db.set(id, value);
      return { conversation_id: id, settings: value };
    }),
  };
  const notify = vi.fn();
  return { api, db, notify, store: new ConversationModelSettingsStore(api, notify) };
}

describe('conversation model settings queue', () => {
  it('keeps a blank chat local and permits first text submission without a PUT', async () => {
    const { api, store } = setup();
    await store.restore('new', false, defaults);
    store.select('new', changed);
    await store.requireConfirmed('new');
    expect(api.getConversationModelSettings).not.toHaveBeenCalled();
    expect(api.putConversationModelSettings).not.toHaveBeenCalled();
  });

  it('saves after first upload materializes the parent and gates send on its acknowledgment', async () => {
    const { api, store, notify } = setup();
    await store.restore('upload', false, defaults);
    store.select('upload', changed);
    const ack = deferred<Awaited<ReturnType<typeof api.putConversationModelSettings>>>();
    api.putConversationModelSettings.mockReturnValueOnce(ack.promise);
    store.materializedByUpload('upload');
    let confirmed = false;
    const waiting = store.requireConfirmed('upload').then(() => { confirmed = true; });
    await Promise.resolve();
    expect(confirmed).toBe(false);
    ack.resolve({ conversation_id: 'upload', settings: saved(changed) });
    await waiting;
    expect(notify).toHaveBeenLastCalledWith('upload', { desired: changed, status: 'saved' });
  });

  it('serializes fast edits and never marks the queued selection saved with an older acknowledgment', async () => {
    const { api, store, notify } = setup();
    await store.restore('a', true, defaults);
    const first = deferred<Awaited<ReturnType<typeof api.putConversationModelSettings>>>();
    const second = deferred<Awaited<ReturnType<typeof api.putConversationModelSettings>>>();
    api.putConversationModelSettings.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    store.select('a', changed);
    store.select('a', { ...changed, reasoning_effort: 'low' });
    expect(api.putConversationModelSettings).toHaveBeenCalledTimes(1);
    first.resolve({ conversation_id: 'a', settings: saved(changed) });
    await vi.waitFor(() => expect(api.putConversationModelSettings).toHaveBeenCalledTimes(2));
    expect(api.putConversationModelSettings.mock.calls[1]).toEqual(['a', { ...changed, reasoning_effort: 'low' }, 1]);
    expect(notify.mock.calls.some(([, state]) => state.status === 'saved')).toBe(true); // Initial restore only.
    expect(notify).toHaveBeenLastCalledWith('a', expect.objectContaining({ status: 'saving' }));
    second.resolve({ conversation_id: 'a', settings: saved({ ...changed, reasoning_effort: 'low' }, 2) });
    await store.requireConfirmed('a');
  });

  it('reconciles a committed PUT whose response was lost without another write', async () => {
    const { api, store, db } = setup();
    await store.restore('a', true, defaults);
    api.putConversationModelSettings.mockImplementationOnce(async (id, selection) => {
      db.set(id, saved(selection)); throw new Error('lost response');
    });
    store.select('a', changed);
    await store.requireConfirmed('a');
    expect(api.putConversationModelSettings).toHaveBeenCalledTimes(1);
  });

  it('retains the original revision and tuple when retrying an uncommitted request', async () => {
    const { api, store, notify } = setup();
    await store.restore('a', true, defaults);
    api.putConversationModelSettings.mockRejectedValueOnce(new Error('offline'));
    store.select('a', changed);
    await vi.waitFor(() => expect(notify).toHaveBeenLastCalledWith('a', expect.objectContaining({ status: 'unconfirmed' })));
    const latest = { ...changed, reasoning_effort: 'low' };
    store.select('a', latest);
    await store.retry('a');
    expect(api.putConversationModelSettings.mock.calls.map(([, selection, revision]) => [selection, revision])).toEqual([
      [changed, 0], [changed, 0], [latest, 1],
    ]);
  });

  it('preserves a local selection on multi-tab conflict and refuses to overwrite using a newer revision', async () => {
    const { api, store, db, notify } = setup();
    await store.restore('a', true, defaults);
    db.set('a', saved({ ...defaults, reasoning_effort: 'low' }, 1));
    store.select('a', changed);
    await expect(store.requireConfirmed('a')).rejects.toThrow('其他页面');
    expect(notify).toHaveBeenLastCalledWith('a', { desired: changed, status: 'conflict' });
    store.select('a', { ...changed, reasoning_effort: 'high' });
    await expect(store.retry('a')).rejects.toThrow('其他页面');
    expect(api.putConversationModelSettings).toHaveBeenCalledTimes(1);
    const reloaded = await store.restore('a', true, defaults, true);
    expect(reloaded.reasoning_effort).toBe('low');
  });

  it('keeps unknown results unconfirmed if readback fails', async () => {
    const { api, store, notify } = setup();
    await store.restore('a', true, defaults);
    api.putConversationModelSettings.mockRejectedValue(new Error('offline'));
    api.getConversationModelSettings.mockRejectedValue(new Error('offline'));
    store.select('a', changed);
    await expect(store.requireConfirmed('a')).rejects.toThrow('尚未确认');
    expect(notify).toHaveBeenLastCalledWith('a', expect.objectContaining({ status: 'unconfirmed' }));
  });

  it('keeps queues bound to the original conversation and resets new conversations to defaults', async () => {
    const { api, store, db } = setup();
    await store.restore('a', true, defaults);
    store.select('a', changed);
    expect(await store.restore('b', false, defaults)).toEqual(defaults);
    await store.requireConfirmed('a');
    expect(db.get('a')).toEqual(saved(changed));
    expect(api.putConversationModelSettings.mock.calls.every(([id]) => id === 'a')).toBe(true);
    expect(api.getConversationModelSettings.mock.calls.some(([id]) => id === 'b')).toBe(false);
  });

  it('reads admission-created settings without overwriting a later edit on replay', async () => {
    const { api, store, db } = setup();
    await store.restore('new', false, defaults);
    db.set('new', saved(changed, 2));
    await store.materializedByMessage('new', defaults);
    await expect(store.requireConfirmed('new')).rejects.toThrow('其他页面');
    expect(api.putConversationModelSettings).not.toHaveBeenCalled();
  });
});

it('serializes an edit behind the first message readback', async () => {
  const { api, store, db } = setup();
  await store.restore('new', false, defaults);
  db.set('new', saved(defaults));
  const firstRead = deferred<Awaited<ReturnType<typeof api.getConversationModelSettings>>>();
  api.getConversationModelSettings.mockReturnValueOnce(firstRead.promise);
  const admitted = store.materializedByMessage('new', defaults);
  store.select('new', changed);
  expect(api.putConversationModelSettings).not.toHaveBeenCalled();
  firstRead.resolve({ conversation_id: 'new', settings: saved(defaults) });
  await admitted;
  await store.requireConfirmed('new');
  expect(api.putConversationModelSettings).toHaveBeenCalledWith('new', changed, 1);
});

it('rejects an older settings load completing after a newer restore of the same chat', async () => {
  const { api, store } = setup();
  const firstRead = deferred<Awaited<ReturnType<typeof api.getConversationModelSettings>>>();
  api.getConversationModelSettings.mockReturnValueOnce(firstRead.promise);
  const first = store.restore('a', true, defaults);
  await Promise.resolve();
  await store.restore('a', true, changed);
  firstRead.resolve({ conversation_id: 'a', settings: saved(defaults) });
  await expect(first).rejects.toThrow('已失效');
});

it('recognizes the frozen first submission after a lost POST response while preserving a newer local edit', async () => {
  const { store, db, api } = setup();
  await store.restore('new', false, defaults);
  db.set('new', saved(defaults)); // The original POST committed but its response was lost.
  store.select('new', changed); // A deliberate edit before the exact message retry.
  await store.materializedByMessage('new', defaults);
  await store.requireConfirmed('new');
  expect(api.putConversationModelSettings).toHaveBeenCalledWith('new', changed, 1);
  expect(db.get('new')).toEqual(saved(changed, 2));
});
