# ADR 0001：内部消息用内容块模型，而不是 OpenAI 的 tool_calls 形状

**状态**：已采纳

## 问题

一次模型调用涉及三类内容：正文、工具调用、工具结果。它们怎么在内部表示？

## 选项

**A. 照抄 OpenAI 的形状。** assistant 上挂 `tool_calls[]`（参数是 JSON 字符串），
工具结果是独立的 `role: "tool"` 消息。

**B. 内容块模型。** assistant / user 消息内部是 content block 列表，
工具调用和结果都是块。

## 决定

选 **B**。

## 理由

Anthropic 的 `tool_use` / `tool_result` 是 assistant / user 消息**内部的
content block**，它根本没有 `tool` 这个 role，而且要求同一轮的每个 `tool_use`
都必须在**紧随其后**的那条 user 消息里找到配对的 `tool_result`。

选 B 之后，转 Anthropic 是**直接映射**；选 A 的话每次都要把 `tool_calls[]`
重新分组、把独立的 tool 消息重排回 user 消息里，而且历史裁剪时必须把
`tool_use`/`tool_result` 当作原子对一起处理——A 的形状里这个"对"根本不可见。

## 代价

* 要自己写双向转换（[`llm/openai_compat.py`](../../src/agentkit/llm/openai_compat.py)
  的 `to_openai_messages`），把块模型摊平成 `tool_calls[]` + `role:"tool"`；
* 要自己维护配对不变量。[`core/types.py`](../../src/agentkit/core/types.py) 的
  `validate_conversation` 在每个推理步开头校验，违反时**指到具体下标和 id**——
  否则线上排查只能靠猜。

这个转换复杂度是为可移植性付的税。如果只打算支持 OpenAI 系（DeepSeek / Qwen /
Kimi / GLM 的 tool calling 骨架一致），选 A 更省事。

## 相关

- 实测的 provider 行为差异见 [docs/provider-notes.md](../provider-notes.md)
- 裁剪时必须成对丢弃：[ADR 0005](0005-session-persistence.md) 提到的记忆裁剪
