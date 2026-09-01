export type SpaceId =
  | "conversation"
  | "history"
  | "memory"
  | "controls"
  | "settings"
  | "developer";

export type PrototypeState =
  | "daily"
  | "first-run"
  | "empty"
  | "no-provider"
  | "offline"
  | "recovering"
  | "version-conflict"
  | "update-ready"
  | "safe-mode";

export interface ConversationSummary {
  id: string;
  title: string;
  preview: string;
  time: string;
  active?: boolean;
  unread?: boolean;
  branchCount?: number;
}

export interface MemoryItem {
  id: string;
  kind: string;
  title: string;
  gist: string;
  source: string;
  updated: string;
  sensitivity: "低" | "中" | "高";
  confidence: number;
  lifecycle: "热" | "温" | "冷" | "归档";
  surfaced: boolean;
}

export const conversations: ConversationSummary[] = [
  {
    id: "today",
    title: "今晚想慢一点",
    preview: "那我们就不赶路，慢慢说。",
    time: "22:18",
    active: true,
    branchCount: 2,
  },
  {
    id: "cat",
    title: "关于小猫",
    preview: "你说它最近总爱睡在窗边。",
    time: "昨天",
    unread: true,
  },
  {
    id: "work",
    title: "项目复盘",
    preview: "我把我们刚才梳理的三个问题记下了。",
    time: "周二",
  },
  {
    id: "rain",
    title: "下雨的晚上",
    preview: "你把窗户推开了一点，雨声就进来了。",
    time: "8月24日",
    branchCount: 3,
  },
];

export const memories: MemoryItem[] = [
  {
    id: "mem_01",
    kind: "长期偏好",
    title: "不喜欢被连续追问",
    gist: "当话题比较私人时，更希望 Sora 先回应而不是马上追问。",
    source: "你在 3 次对话中明确表达",
    updated: "今天 21:42",
    sensitivity: "中",
    confidence: 0.96,
    lifecycle: "热",
    surfaced: true,
  },
  {
    id: "mem_02",
    kind: "共同经历",
    title: "第一次一起听完整场夏雨",
    gist: "那天没有安排话题，只是断断续续聊到雨停。",
    source: "对话证据 4 条",
    updated: "8月24日",
    sensitivity: "低",
    confidence: 0.93,
    lifecycle: "温",
    surfaced: true,
  },
  {
    id: "mem_03",
    kind: "人物与关系",
    title: "窗边的小猫叫团子",
    gist: "团子是你的猫，最近喜欢睡在窗边的旧毛毯上。",
    source: "你直接确认",
    updated: "8月27日",
    sensitivity: "低",
    confidence: 0.99,
    lifecycle: "热",
    surfaced: false,
  },
  {
    id: "mem_04",
    kind: "渐变偏好",
    title: "夜晚更愿意聊得深入",
    gist: "近一个月的多轮对话显示，夜晚通常接受更长、更慢的回应。",
    source: "跨 7 个 episode 聚合",
    updated: "8月26日",
    sensitivity: "中",
    confidence: 0.82,
    lifecycle: "温",
    surfaced: true,
  },
];

export const providers = [
  {
    name: "DeepSeek",
    model: "deepseek-v4-flash",
    status: "可用",
    latency: "620 ms",
    workloads: "快速理解 · 语义判断",
    accent: "blue",
  },
  {
    name: "DeepSeek",
    model: "deepseek-v4-pro",
    status: "可用",
    latency: "1.8 s",
    workloads: "生成回应 · 整理记忆 · 生成摘要",
    accent: "violet",
  },
  {
    name: "Local Embedding",
    model: "bge-m3",
    status: "本机",
    latency: "38 ms",
    workloads: "向量化 · 记忆召回",
    accent: "mint",
  },
];

export const timelineSpans = [
  { name: "对话入口接收", start: 0, width: 4, duration: "12 ms", tone: "neutral" },
  { name: "可打断性策略", start: 4, width: 7, duration: "28 ms", tone: "mint" },
  { name: "轮次理解", start: 11, width: 15, duration: "118 ms", tone: "blue" },
  { name: "召回规划", start: 26, width: 5, duration: "34 ms", tone: "amber" },
  { name: "混合召回", start: 31, width: 13, duration: "96 ms", tone: "violet" },
  { name: "呈现策略", start: 44, width: 6, duration: "41 ms", tone: "mint" },
  { name: "回应模型", start: 50, width: 38, duration: "1.42 s", tone: "blue" },
  { name: "持久化与 Outbox", start: 88, width: 8, duration: "67 ms", tone: "neutral" },
];

export const debugCommands = [
  { id: "memory.recall", label: "memory.recall", risk: "L1", description: "以当前会话上下文预览混合召回结果" },
  { id: "memory.explain", label: "memory.explain", risk: "L1", description: "解释某条记忆为什么进入 Context Frame" },
  { id: "lifecycle.dry_run", label: "lifecycle.dry_run", risk: "L1", description: "预览热度、巩固与归档计划" },
  { id: "projection.rebuild", label: "projection.rebuild", risk: "L3", description: "为测试 profile 重建投影状态" },
  { id: "eval.run", label: "eval.run", risk: "L2", description: "运行当前变更关联的 Golden Set" },
];
