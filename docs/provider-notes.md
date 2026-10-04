# Provider 实测记录

这里的每条结论都是**跑出来的**，不是从文档抄的。复现方式：

```bash
uv run python scripts/capture_stream.py --show
```

脚本会把原始 SSE chunk 存进 `tests/fixtures/`，之后测试重放 fixture，
既不用联网也不重复花 API 费用。

---

## 为什么要有这份记录

流式 tool_call 的 delta 语义，各家文档基本不写，而社区里的说法互相矛盾。
我在设计流式归一化层之前查到的说法是：

> DeepSeek 以「累积重发」方式返回 `tool_calls`——每个 chunk 重发「到目前为止的
> 全部 arguments」，客户端无脑 `+=` 会拼出 `{...}{...}`，`json.loads` 失败后被
> 静默吞成空参数。

如果我照这个说法去写，就会加一层根本没必要的启发式；反过来，如果我照「增量」
写而实际是累积，工具参数就会全部解析失败。**只能实测。**

---

## 实测结果（`deepseek-flash`，2026-10-05）

### 1. `arguments` 是**真增量**，不是累积重发

一次带两个参数的工具调用被切成 24 个片段：

```
片段 1: ''
片段 2: '{'
片段 3: '"'
片段 4: 'file'
片段 5: '_path'
片段 6: '"'
...
片段 22: '8'
片段 23: '"'
片段 24: '}'
```

按顺序拼接得到合法的 JSON 对象；每个片段**不是**前一个的超集。
所以社区流传的「累积重发」在当前版本上**不成立**。

**但仍然保留了兜底。** `join_argument_fragments()` 先用增量拼接，拼不出合法对象时
再试「取最长片段」这条退路，并把走了退路这件事记进备注。理由：这个行为是
**服务端实现细节**，没有契约保证它不变；而写错的代价（参数静默变成 `{}`）
比多写十行兜底代码严重得多。

### 2. `id` / `name` 只在首个增量出现

```json
// 第一个携带 tool_call 的 chunk
{"delta": {"tool_calls": [{"index": 0,
                           "id": "call_00_623kPRBF8UPjhiqkznpL3225",
                           "function": {"arguments": "", "name": "read_file"},
                           "type": "function"}]}}
```

后续 chunk 里 `id` 和 `name` **整个字段都不存在**，只有 `function.arguments`。

后果：`id`/`name` 是**赋值**语义，`arguments` 是**追加**语义。混用 `+=`
会把 id 拼成一串垃圾，或者在下一次覆盖成 `None`。
`StreamAccumulator` 因此只在字段非 `None` 时覆盖。

### 3. 并行调用靠 `index` 区分

多个工具调用会在**同一个 chunk** 里同时给出不同的 `index`，之后各自的参数片段
交错到达。按出现顺序分组会把两个调用的参数混在一起，必须按 `index` 分组。

### 4. 思维链占了输出 token 的绝大多数

让模型「数到五，只输出数字」，结果：

| 指标 | 值 |
| --- | --- |
| chunk 总数 | 1023 |
| 其中含 `reasoning_content` | 1013 |
| 其中含正文 `content` | 10 |
| 输出 token | 1022 |
| 其中 reasoning token | 1012 |

**正文只有 10 个 token，思维链有 1012 个。** 两个直接结论：

* 不做流式渲染的话，用户要盯着空白界面等一秒钟才能看到「1 2 3 4 5」；
* 如果按正文长度估算成本，会低估两个数量级。

### 5. usage 在最后一个 chunk，且那个 chunk 的 `choices` 是**空列表**

```json
{"choices": [],
 "usage": {"prompt_tokens": 332, "completion_tokens": 81,
           "prompt_tokens_details": {"cached_tokens": 0},
           "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 332,
           "completion_tokens_details": {"reasoning_tokens": 21}}}
```

两个坑：

* 把「`choices` 为空」当成「没有内容」直接跳过，就永远拿不到 usage；
* 缓存命中数**同时**出现在 `prompt_tokens_details.cached_tokens` 和顶层
  `prompt_cache_hit_tokens` 两处。

### 6. 不需要 `stream_options={"include_usage": True}`

OpenAI 必须显式开这个开关才给 usage，**DeepSeek 默认就给**（场景 1 没传该参数，
usage 照样出现在最后一个 chunk）。

这个差异是 `ModelCapabilities.usage_in_stream` 存在的原因。本项目统一显式打开
该开关（实测 DeepSeek 接受它），让「有没有 usage」不再是各家差异。

---

## 对设计的影响

| 实测结论 | 落到哪个设计决策 |
| --- | --- |
| 增量语义 | `StreamAccumulator` 直接拼接；`join_argument_fragments` 额外兜底累积重发 |
| id/name 只出现一次 | 累加器对这两个字段用赋值而非追加 |
| `index` 区分并行调用 | 累加器用 `dict[int, _PartialCall]` 按 index 分组 |
| 思维链占大头 | 引擎单独发 `reasoning_delta` 事件；CLI 默认只计数避免刷屏 |
| usage 在空 choices 的 chunk 里 | `to_stream_chunk` 优先处理这种情况，不当作无内容跳过 |
| 缓存字段重复 | `normalize_usage` 按「常见程度」依次尝试多个字段名 |
