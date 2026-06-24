import { describe, expect, it } from 'vitest';
import { COMPOSER_PLACEHOLDERS, pickComposerPlaceholder } from './composerPlaceholders';

describe('composerPlaceholders', () => {
  it('keeps the approved concise composer placeholder pool', () => {
    expect(COMPOSER_PLACEHOLDERS).toEqual([
      '从这里开始...',
      '输入 "/" 选择 Skill。',
      '用 "/" 快速调用 Skill。',
      '输入问题或目标。',
      '上传文件开始分析。',
      '拖拽文件到这里上传。',
      '把文件拖进发送框。',
      '直接说你的需求。',
      '想做什么？直接说。',
    ]);
  });

  it('selects one placeholder by random index', () => {
    expect(pickComposerPlaceholder(() => 0)).toBe('从这里开始...');
    expect(pickComposerPlaceholder(() => 0.5)).toBe('上传文件开始分析。');
    expect(pickComposerPlaceholder(() => 0.999999)).toBe('想做什么？直接说。');
  });
});
