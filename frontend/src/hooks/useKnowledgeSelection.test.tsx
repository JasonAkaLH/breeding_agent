import { act, renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { KnowledgeApi } from '../api/client';
import { useKnowledgeSelection } from './useKnowledgeSelection';

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => { resolve = done; });
  return { promise, resolve };
}
function makeApi(overrides: Partial<KnowledgeApi> = {}) {
  return {
    listKnowledgeLibraries: vi.fn(async () => ({ libraries: ['a', 'b', 'new', 'saved'].map((id) => ({ library_id: id, name: id, description: '', owner_username: 'alice', role: 'owner', revision: 1, index_revision: 1, status: 'active' })) })),
    getKnowledgeSelection: vi.fn(async () => ({ mode: 'auto' as const, library_ids: [] })),
    saveKnowledgeSelection: vi.fn(async (_id, selection) => selection),
    ...overrides,
  } as unknown as KnowledgeApi;
}

describe('useKnowledgeSelection', () => {
  it('does not send a queued previous-account save with the next account credentials', async () => {
    const pending = deferred();
    const api = makeApi({ saveKnowledgeSelection: vi.fn(async (_id, selection) => { await pending.promise; return selection; }) });
    const { result, rerender } = renderHook(({ username }) => useKnowledgeSelection(api, username, 'conv'), { initialProps: { username: 'alice' } });
    await waitFor(() => expect(result.current.loading).toBe(false));
    act(() => {
      result.current.change({ mode: 'selected', library_ids: ['a'] });
      result.current.change({ mode: 'selected', library_ids: ['b'] });
    });
    await waitFor(() => expect(api.saveKnowledgeSelection).toHaveBeenCalledTimes(1));
    rerender({ username: 'bob' });
    await waitFor(() => expect(result.current.loading).toBe(false));
    await act(async () => pending.resolve());
    expect(api.saveKnowledgeSelection).toHaveBeenCalledTimes(1);
    expect(result.current.selection.mode).toBe('auto');
  });
  it('keeps a failed requested selection when refreshing library availability', async () => {
    const api = makeApi({ saveKnowledgeSelection: vi.fn(async () => { throw new Error('保存失败'); }) });
    const { result } = renderHook(() => useKnowledgeSelection(api, 'alice', 'conv'));
    await waitFor(() => expect(result.current.loading).toBe(false));
    act(() => result.current.change({ mode: 'selected', library_ids: ['a'] }));
    await waitFor(() => expect(result.current.error).toBe('保存失败'));
    act(() => result.current.reload());
    await waitFor(() => expect(api.listKnowledgeLibraries).toHaveBeenCalledTimes(2));
    expect(result.current.selection).toEqual({ mode: 'selected', library_ids: ['a'] });
    await expect(result.current.ensureSaved()).rejects.toThrow('保存失败');
    expect(api.getKnowledgeSelection).toHaveBeenCalledTimes(1);
  });

  it('requires reselection when every saved selected library is unavailable', async () => {
    const api = makeApi({ getKnowledgeSelection: vi.fn(async () => ({ mode: 'selected', library_ids: ['removed'] })) });
    const { result } = renderHook(() => useKnowledgeSelection(api, 'alice', 'conv'));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.requiresReselection).toBe(true);
    await expect(result.current.ensureSaved()).rejects.toThrow('全部失效');
    expect(api.saveKnowledgeSelection).not.toHaveBeenCalled();
  });
  it('serializes rapid selection writes and waits for the last saved value', async () => {
    const first = deferred();
    const second = deferred();
    const order: string[] = [];
    const api = makeApi({ saveKnowledgeSelection: vi.fn(async (_id, selection) => {
      const name = selection.library_ids[0]; order.push(name);
      await (name === 'a' ? first.promise : second.promise);
      return selection;
    }) });
    const { result } = renderHook(() => useKnowledgeSelection(api, 'alice', 'conv'));
    await waitFor(() => expect(result.current.loading).toBe(false));
    act(() => {
      result.current.change({ mode: 'selected', library_ids: ['a'] });
      result.current.change({ mode: 'selected', library_ids: ['b'] });
    });
    const finished = vi.fn();
    void result.current.ensureSaved().then(finished);
    await waitFor(() => expect(order).toEqual(['a']));
    await act(async () => first.resolve());
    expect(order).toEqual(['a', 'b']);
    expect(finished).not.toHaveBeenCalled();
    await act(async () => second.resolve());
    expect(finished).toHaveBeenCalledWith(true);
    expect(result.current.selection.library_ids).toEqual(['b']);
  });

  it('discards late selection loads after account or conversation changes', async () => {
    const late = deferred();
    const api = makeApi({ getKnowledgeSelection: vi.fn(async (id) => {
      if (id === 'old') await late.promise;
      return { mode: 'selected', library_ids: [id] };
    }) });
    const { result, rerender } = renderHook(({ username, id }) => useKnowledgeSelection(api, username, id), { initialProps: { username: 'alice', id: 'old' } });
    await waitFor(() => expect(api.getKnowledgeSelection).toHaveBeenCalledWith('old'));
    rerender({ username: 'bob', id: 'new' });
    await waitFor(() => expect(result.current.selection.library_ids).toEqual(['new']));
    await act(async () => late.resolve());
    expect(result.current.selection.library_ids).toEqual(['new']);
  });

  it('does not save auto as a retry for a failed initial load', async () => {
    const api = makeApi({ getKnowledgeSelection: vi.fn().mockRejectedValueOnce(new Error('加载失败')).mockResolvedValue({ mode: 'selected', library_ids: ['saved'] }) });
    const { result } = renderHook(() => useKnowledgeSelection(api, 'alice', 'conv'));
    await waitFor(() => expect(result.current.error).toBe('加载失败'));
    await expect(result.current.ensureSaved()).rejects.toThrow('加载失败');
    act(() => result.current.retry());
    await waitFor(() => expect(result.current.selection.library_ids).toEqual(['saved']));
    expect(api.saveKnowledgeSelection).not.toHaveBeenCalled();
  });
});
