export const WELCOME_PROMPTS = [
  '准备开始新的任务。',
  '有什么需要开始处理的吗？',
  '新的工作可以从这里开始。',
  '要开始一次新的分析吗？',
  '当前可以进入新的任务流程。',
  '现在开始看一个新问题吗？',
  '可以开始新的协作。',
  '需要我协助启动新的任务吗？',
  '新的问题可以直接提交。',
  '我们开始新的工作吗？',
  '已准备好接收新的任务。',
  '是否需要开始处理一个新问题？',
  '新的分析可以从目标开始。',
  '现在有什么需要处理？',
  '可以从新的需求开始。',
  '准备进入新的处理流程。',
  '需要开始一次新的查询或分析吗？',
  '这里可以作为新任务的起点。',
  '可以开始处理新的业务问题。',
  '接下来要处理什么？',
] as const;

export function pickWelcomePrompt(random: () => number = Math.random): string {
  const index = Math.floor(random() * WELCOME_PROMPTS.length);
  return WELCOME_PROMPTS[Math.min(index, WELCOME_PROMPTS.length - 1)];
}
