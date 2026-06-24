export const COMPOSER_PLACEHOLDERS = [
  '从这里开始...',
  '输入 "/" 选择 Skill。',
  '用 "/" 快速调用 Skill。',
  '输入问题或目标。',
  '上传文件开始分析。',
  '拖拽文件到这里上传。',
  '把文件拖进发送框。',
  '直接说你的需求。',
  '想做什么？直接说。',
] as const;

export function pickComposerPlaceholder(random: () => number = Math.random): string {
  const index = Math.floor(random() * COMPOSER_PLACEHOLDERS.length);
  return COMPOSER_PLACEHOLDERS[Math.min(index, COMPOSER_PLACEHOLDERS.length - 1)];
}
