import type { ApiClient } from '../api/client';
import type { ConversationModelSelection, ConversationModelSettings } from '../api/types';

class ModelSettingsError extends Error {
  get userMessage() { return this.message; }
}

export type ModelSettingsStatus = 'draft' | 'saved' | 'saving' | 'unconfirmed' | 'conflict';
export interface ModelSettingsState {
  desired: ConversationModelSelection;
  status: ModelSettingsStatus;
}

interface Entry extends ModelSettingsState {
  exists: boolean;
  revision: number;
  confirmed: ConversationModelSelection | null;
  pending?: Promise<void>;
  uncertain?: { selection: ConversationModelSelection; revision: number };
}

export function sameModelSelection(a: ConversationModelSelection | null, b: ConversationModelSelection | null): boolean {
  return a === b || Boolean(a && b && a.model_edition === b.model_edition
    && a.reasoning_effort === b.reasoning_effort && a.deep_thinking === b.deep_thinking);
}

// Each queue captures its conversation ID and revision; switching chats only changes
// which queue's status the view displays. No preference is stored in localStorage.
export class ConversationModelSettingsStore {
  private entries = new Map<string, Entry>();
  private loads = new Map<string, number>();
  private disposed = false;

  constructor(
    private api: Pick<ApiClient, 'getConversationModelSettings' | 'putConversationModelSettings'>,
    private changed: (conversationId: string, state: ModelSettingsState) => void,
  ) {}

  dispose() { this.disposed = true; }
  activate() { this.disposed = false; }

  private notify(id: string, entry: Entry) {
    if (!this.disposed) this.changed(id, { desired: entry.desired, status: entry.status });
  }

  async restore(id: string, exists: boolean, defaults: ConversationModelSelection, discardLocal = false): Promise<ConversationModelSelection> {
    const generation = (this.loads.get(id) ?? 0) + 1;
    this.loads.set(id, generation);
    const previous = this.entries.get(id);
    await previous?.pending;
    if (previous && !discardLocal && (previous.status === 'conflict' || previous.status === 'unconfirmed')) {
      this.notify(id, previous);
      return previous.desired;
    }
    const saved = exists ? (await this.api.getConversationModelSettings(id)).settings : null;
    if (this.disposed || this.loads.get(id) !== generation) throw new Error('会话配置加载已失效。');
    const entry: Entry = {
      exists, revision: saved?.revision ?? 0, desired: saved ?? defaults,
      confirmed: saved, status: exists ? 'saved' : 'draft',
    };
    if (!this.disposed) this.entries.set(id, entry);
    this.notify(id, entry);
    return entry.desired;
  }

  select(id: string, selection: ConversationModelSelection) {
    const entry = this.entries.get(id);
    if (!entry || this.disposed) return;
    entry.desired = selection;
    if (!entry.exists) {
      entry.status = 'draft';
      this.notify(id, entry);
    } else if (entry.status === 'conflict' || entry.status === 'unconfirmed') {
      // An unresolved write must be reconciled before using a newer revision.
      this.notify(id, entry);
    } else {
      this.start(id, entry);
    }
  }

  materializedByUpload(id: string) {
    const entry = this.entries.get(id);
    if (!entry) throw new ModelSettingsError('模型配置尚未加载。');
    if (!entry.exists) {
      entry.exists = true;
      this.start(id, entry);
    }
  }

  async materializedByMessage(id: string, submitted: ConversationModelSelection) {
    const entry = this.entries.get(id);
    if (!entry || entry.exists || this.disposed) return;
    entry.exists = true;
    // The admission transaction already saved the frozen selection. Only read it;
    // replaying a message must never overwrite a more recent preference.
    entry.uncertain = { selection: submitted, revision: 0 };
    entry.status = 'saving';
    this.notify(id, entry);
    entry.pending = this.confirmMessage(id, entry).finally(() => { entry.pending = undefined; });
    await entry.pending;
  }

  private async confirmMessage(id: string, entry: Entry) {
    if (await this.reconcile(id, entry)) {
      entry.status = sameModelSelection(entry.confirmed, entry.desired) ? 'saved' : 'unconfirmed';
      if (entry.confirmed && entry.status !== 'saved') await this.save(id, entry);
    }
    this.notify(id, entry);
  }

  async requireConfirmed(id: string) {
    const entry = this.entries.get(id);
    if (!entry || this.disposed) throw new ModelSettingsError('模型配置尚未加载。');
    if (!entry.exists) return; // First text admission creates and initializes it.
    await entry.pending;
    if (entry.status === 'unconfirmed') {
      if (await this.reconcile(id, entry)) {
        if (sameModelSelection(entry.confirmed, entry.desired)) entry.status = 'saved';
        else this.start(id, entry);
        this.notify(id, entry);
      }
      await entry.pending;
    }
    if (entry.status !== 'saved') throw new ModelSettingsError(entry.status === 'conflict'
      ? '模型配置已在其他页面修改，请重新加载已保存配置后再发送。'
      : '模型配置保存尚未确认，请重试保存后再发送。');
  }

  retry(id: string) { return this.requireConfirmed(id); }

  private start(id: string, entry: Entry) {
    if (entry.pending || this.disposed) return;
    entry.status = 'saving';
    this.notify(id, entry);
    entry.pending = this.save(id, entry).finally(() => { entry.pending = undefined; });
  }

  private async save(id: string, entry: Entry) {
    while (!this.disposed) {
      const attempt = entry.uncertain ?? { selection: entry.desired, revision: entry.revision };
      entry.status = 'saving';
      this.notify(id, entry);
      try {
        const result = await this.api.putConversationModelSettings(id, attempt.selection, attempt.revision);
        if (!result.settings || !sameModelSelection(result.settings, attempt.selection)) throw new Error('Invalid saved model settings');
        this.confirm(entry, result.settings);
      } catch {
        entry.uncertain = attempt;
        if (!await this.reconcile(id, entry)) {
          this.notify(id, entry);
          return;
        }
        // An unchanged revision permits an explicit retry of the original request,
        // but never an automatic unbounded loop after a failed PUT.
        if (entry.uncertain) {
          entry.status = 'unconfirmed';
          this.notify(id, entry);
          return;
        }
      }
      if (sameModelSelection(entry.confirmed, entry.desired)) {
        entry.status = 'saved';
        this.notify(id, entry);
        return;
      }
    }
  }

  private confirm(entry: Entry, saved: ConversationModelSettings) {
    entry.revision = saved.revision;
    entry.confirmed = saved;
    entry.uncertain = undefined;
  }

  private async reconcile(id: string, entry: Entry): Promise<boolean> {
    try {
      const saved = (await this.api.getConversationModelSettings(id)).settings;
      if (saved && (sameModelSelection(saved, entry.desired) || sameModelSelection(saved, entry.uncertain?.selection ?? null))) {
        this.confirm(entry, saved);
        return true;
      }
      if ((saved?.revision ?? 0) === entry.uncertain?.revision) {
        entry.status = 'unconfirmed';
        return true;
      }
      entry.status = 'conflict';
    } catch {
      entry.status = 'unconfirmed';
    }
    return false;
  }
}
