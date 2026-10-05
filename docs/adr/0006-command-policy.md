# ADR 0006：命令安全用三分裁决，不用正则黑名单

**状态**：已采纳

## 问题

`run_command` 工具能让模型执行任意 shell 命令。怎么防止它把系统搞坏？

## 选项

**A. 正则黑名单。** 拦掉已知的危险模式：

```python
DANGEROUS_PATTERNS = [r"\brm\s+-rf\b", r"\bformat\s+[A-Za-z]:", r"\bshutdown\b", ...]
```

**B. 三分裁决。** 明确破坏性的**拒绝**、已知安全的**放行**、其余一律**转人工审批**。

## 决定

选 **B**。

## 理由

A 有两个问题，而且都很致命：

**必然漏。** `rm -rf` 的等价写法有无穷多：`rm -r -f`、`rm${IFS}-rf`、
`rm -f -r`、`python -c "import shutil; shutil.rmtree(...)"`、
PowerShell 里的一堆等价形式。黑名单只能拦住**你已经想到的**那些。

**会误杀。** `echo "rm -rf /"` 只是打印一段字符串，黑名单会把它拦下来。
这在 agent 场景里不是小事——工具被莫名其妙拦住，模型会反复重试。

B 的默认方向也更重要：**默认不信任**。看不明白的一律转人工，而不是默认放行。

## 实现细节

* **按段解析可执行文件，不对整条命令串做正则。**
  [`split_command_segments`](../../src/agentkit/tools/policy.py) 会按 `&&` `||` `;` `|`
  拆段，**尊重引号**——所以 `echo "rm -rf /"` 不会被误杀。
* **判据是段首的可执行文件名**，不是命令里出现了什么词。
* **拒绝清单刻意保持很短**（`mkfs` / `diskpart` / `shutdown` / `format` 等），
  因为这些是真的没有安全用法。剩下的交给审批。
* **管道到 shell 永远转审批**（`curl ... | sh` 是远程代码执行的标准路径）。

## 代价

* **白名单会误伤正常任务**，需要审批流程兜底。在无人值守的场景（`serve`）
  默认是 **DenyApprover**，意味着这些任务会失败并交回给人。
* **审批是异步的**，需要从 API 一路传到工具执行，状态机复杂度上升
  （见 [`runtime/approval.py`](../../src/agentkit/runtime/approval.py)）。
* **超时按拒绝处理**（默认 300 秒）。安全方向上的默认值只能往保守那边倒。

## 一个必须说清楚的限制

**命令允许清单是能力限制器，不是沙箱。**

`python -c '...'` 可以做任何事，而 `python` 必须在允许清单里——agent 本来就要用它
跑脚本。同理 `python evil.py` 也是任意代码执行。**这不是加特例能解决的问题。**

真正的隔离要靠容器（或者 seccomp / 命名空间）。这一条被固定成了一条测试
（`test_interpreter_inline_code_is_a_documented_limitation`），
就是为了防止以后有人误以为清单提供了它并不提供的保证。

## 顺带修掉的一个真实漏洞

第一版的路径边界检查用 `os.path.abspath` + `startswith`：

* `abspath` **不解析符号链接**——工作区内放一个指向 `C:\Windows\System32` 的
  symlink，检查照样通过，实际写到了外面；
* 字符串前缀比较不严谨——`C:\foo` 是 `C:\foobar` 的前缀，但二者不是父子关系。

现在用 `realpath`（解析所有 symlink 和 `..`）+ `commonpath`（真正的路径包含判断）。
两条都有对应的测试，包括 symlink 逃逸那条。
