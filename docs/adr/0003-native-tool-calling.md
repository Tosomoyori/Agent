# ADR 0003：用原生 function calling，不用文本解析式 ReAct

**状态**：已采纳

## 问题

模型怎么表达「我要调用某个工具」？

## 选项

**A. 文本式 ReAct。** 提示词里规定一套 JSON 输出格式，模型把 `{"thought": ...,
"action": {...}}` 写在正文里，客户端用正则抠出来、`json.loads`、再执行。

**B. 原生 function calling。** 把工具 schema 通过 `tools` 参数传给 API，
模型通过结构化的 `tool_calls` 字段返回。

## 决定

选 **B**。

## 理由

选 A 的实现（这个项目的第一版）是这样的：

```python
fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
...
try:
    return json.loads(candidate)
except json.JSONDecodeError:
    try:
        return ast.literal_eval(candidate)   # 兜底 1
    except Exception:
        return {"thought": raw, "_raw": raw}  # 兜底 2
```

这条路径上有**四个静默降级点**，每一个都会把模型的错误吞掉：抠不到代码块就退化成
「原始输出」，JSON 不合法就退化成「原始输出」，最后统一被当成一次 observation
塞回下一轮。模型看到的是自己上一轮的胡言乱语，然后继续胡言乱语。

业界对文本式 ReAct 的格式错误率估计在 **17–23%**。而且它有一个结构性缺陷：
表达不了**并行工具调用**——一个 JSON 对象里只有一个 `action`。

原生 function calling 把这些全消掉了：格式由 API 层保证，参数由 API 保证是
结构化 JSON，`tool_calls` 天然是个数组。

## 代价

* 绑定了支持 function calling 的模型（2026 年这已不是限制）；
* 各家 provider 的工具调用细节仍有差异（流式 delta 语义、并行开关），
  要适配——见 [ADR 0004](0004-stream-normalization.md)；
* 提示词里无法再「教」模型复杂的多步格式。但这是好事：那本来就不该由提示词承担。

## 副作用：提示词短了一大截

第一版的 `prompt_template.py` 有近 60 行在教模型**怎么输出 JSON**：两种模式的
格式、示例、"必须包含 thought 字段"、转义规则。现在这些全没了，
系统提示词只说清楚**任务与边界**。

不是"提示词变简单了"，是**把本就不该由提示词承担的职责还给了协议层**。
