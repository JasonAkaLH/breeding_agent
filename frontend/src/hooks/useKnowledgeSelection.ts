import { useCallback, useEffect, useRef, useState } from 'react';
import type { KnowledgeApi } from '../api/client';
import type { KnowledgeLibrary, KnowledgeSelection } from '../api/knowledgeTypes';

interface SelectionState {
  libraries: KnowledgeLibrary[];
  selection: KnowledgeSelection;
  loading: boolean;
  saving: boolean;
  error: string | null;
}
interface QueueEntry {
  promise: Promise<void>;
  error: Error | null;
  selection: KnowledgeSelection;
  loaded: boolean;
  libraries: KnowledgeLibrary[];
}
const initialState = (): SelectionState => ({ libraries: [], selection: { mode: 'auto', library_ids: [] }, loading: true, saving: false, error: null });
const errorMessage = (error: unknown) => error instanceof Error ? error.message : '知识库选择保存失败，请重试。';

export function useKnowledgeSelection(api: KnowledgeApi, username: string | undefined, conversationId: string) {
  const key = JSON.stringify([username, conversationId]);
  const currentKey = useRef(key);
  const currentUsername = useRef(username);
  const mountedRef = useRef(true);
  currentKey.current = key;
  currentUsername.current = username;
  const queues = useRef(new Map<string, QueueEntry>());
  const [state, setState] = useState<SelectionState>(initialState);
  const [refreshId, setRefreshId] = useState(0);
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  useEffect(() => {
    let mounted = true;
    if (!username || !conversationId) {
      setState(initialState());
      return;
    }
    setState(initialState());
    const previous = queues.current.get(key);
    const entry: QueueEntry = { promise: Promise.resolve(), error: null, selection: { mode: 'auto', library_ids: [] }, loaded: false, libraries: [] };
    queues.current.set(key, entry);
    entry.promise = (previous?.promise ?? Promise.resolve()).then(() => {
      if (currentUsername.current !== username) throw new Error('账号已切换');
      return Promise.all([api.listKnowledgeLibraries(), api.getKnowledgeSelection(conversationId)]);
    })
      .then(([libraries, selection]) => {
        entry.selection = selection;
        entry.loaded = true;
        entry.libraries = libraries.libraries;
        if (mounted && currentKey.current === key) setState({ libraries: libraries.libraries, selection, loading: false, saving: false, error: null });
      })
      .catch((error: unknown) => {
        entry.error = error instanceof Error ? error : new Error(errorMessage(error));
        if (mounted && currentKey.current === key) setState((old) => ({ ...old, loading: false, error: errorMessage(error) }));
      });
    return () => { mounted = false; };
  }, [api, username, conversationId, key, refreshId]);

  const change = useCallback((selection: KnowledgeSelection) => {
    const entry = queues.current.get(key);
    if (!entry || currentKey.current !== key) return;
    entry.selection = selection;
    setState((old) => ({ ...old, selection, saving: true, error: null }));
    const promise = entry.promise.then(async () => {
      if (!mountedRef.current || currentUsername.current !== username) return;
      try {
        await api.saveKnowledgeSelection(conversationId, selection);
        entry.error = null;
      } catch (error) {
        entry.error = error instanceof Error ? error : new Error(errorMessage(error));
      }
    });
    entry.promise = promise;
    void promise.then(() => {
      if (!mountedRef.current || currentKey.current !== key || entry.promise !== promise) return;
      setState((old) => ({ ...old, saving: false, error: entry.error?.message ?? null }));
    });
  }, [api, conversationId, key, username]);

  const ensureSaved = useCallback(async () => {
    for (;;) {
      const entry = queues.current.get(key);
      if (!entry) throw new Error('知识库选择尚未加载，请稍后重试。');
      const pending = entry.promise;
      await pending;
      if (!mountedRef.current || currentKey.current !== key) return false;
      if (entry.promise !== pending || queues.current.get(key) !== entry) continue;
      if (entry.error) throw entry.error;
      if (entry.selection.mode === 'selected' && entry.selection.library_ids.length === 0) throw new Error('请至少选择一个文献库，或返回全部可访问库。');
      if (entry.selection.mode === 'selected' && !entry.selection.library_ids.some((id) => entry.libraries.some((library) => library.library_id === id && library.status === 'active'))) throw new Error('所选文献库全部失效，请重新选择文献库。');
      return true;
    }
  }, [key]);

  const reload = useCallback(() => {
    const entry = queues.current.get(key);
    if (!entry?.loaded) { setRefreshId((id) => id + 1); return; }
    void entry.promise.then(() => {
      if (currentUsername.current !== username) throw new Error('账号已切换');
      return api.listKnowledgeLibraries();
    }).then((result) => {
      if (!mountedRef.current || currentKey.current !== key || queues.current.get(key) !== entry) return;
      entry.libraries = result.libraries;
      setState((old) => ({ ...old, libraries: result.libraries }));
    }).catch((error: unknown) => {
      if (mountedRef.current && currentKey.current === key && queues.current.get(key) === entry) {
        entry.error = error instanceof Error ? error : new Error(errorMessage(error));
        setState((old) => ({ ...old, error: errorMessage(error) }));
      }
    });
  }, [api, key, username]);

  const requiresReselection = state.selection.mode === 'selected' && !state.selection.library_ids.some((id) => state.libraries.some((library) => library.library_id === id && library.status === 'active'));
  return { ...state, requiresReselection, change, ensureSaved, retry: () => {
    if (queues.current.get(key)?.loaded) change(state.selection);
    else setRefreshId((id) => id + 1);
  }, reload };
}
