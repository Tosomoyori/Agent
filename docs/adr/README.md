# 架构决策记录（ADR）

这里记录的是**做过的技术决策**：问题是什么、有哪些选项、选了什么、**代价是什么**。

写 ADR 而不是写设计文档，是因为设计文档只讲「现在长什么样」，
而 ADR 保留「当时为什么不选另一条路」。面试里被追问的几乎全是后者。

每份 ADR 的「代价」一节是刻意的——没有代价的决策通常意味着没想清楚。

| 编号 | 决策 | 一句话 |
| --- | --- | --- |
| [0001](0001-content-block-messages.md) | 内部消息用内容块模型 | 为了转 Anthropic 是直接映射，代价是自己写双向转换和配对不变量校验 |
| [0002](0002-typed-event-stream.md) | 引擎只产出类型化事件 | 四个消费者共享一条流；意外收获是追踪变成了纯函数 |
| [0003](0003-native-tool-calling.md) | 原生 function calling | 文本解析式 ReAct 有四个静默降级点，且表达不了并行调用 |
| [0004](0004-stream-normalization.md) | 流式增量在适配器层归一化 | 实测证伪了社区流传的「累积重发」说法；留了兜底但要求留痕 |
| [0005](0005-session-persistence.md) | 会话持久化自造 | 不满足委托 Temporal 的前提；明确承认 run 级恢复没做 |
| [0006](0006-command-policy.md) | 命令安全用三分裁决 | 黑名单必然漏也会误杀；并明确承认允许清单不是沙箱 |
| [0007](0007-retry-policy.md) | 重试只在首个 chunk 之前 | 已吐出的内容收不回来；SDK 重试必须关掉否则次数相乘 |
| [0008](0008-evaluation-metrics.md) | 主指标用 pass^k 和状态判据 | pass@k 衡量潜力，pass^k 衡量可靠性，上线的关键是后者 |

## 相关的其他文档

- [docs/provider-notes.md](../provider-notes.md) —— 实测出来的 provider 流式行为
- [docs/evaluation-notes.md](../evaluation-notes.md) —— 评测结果，以及判据写错五次的记录
- [docs/memory-design.md](../memory-design.md) —— 记忆系统做到哪、为什么停在这
- [docs/a2a-scope.md](../a2a-scope.md) —— Agent 发现做到哪、为什么停在这
