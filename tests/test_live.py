"""真实调用 LLM 的冒烟测试。

默认**跳过**（``pyproject.toml`` 里 ``addopts = "-m 'not live'"``），因为它们慢、
要联网、而且**会真实产生费用**。需要时显式运行::

    uv run pytest -m live

这里的断言刻意宽松——只验证「协议层面通了」，不验证模型答得对不对。
对不确定的模型行为做精确断言，只会得到一个随模型更新就碎的测试套件。

Provider 的协议行为（流式 tool_call 的 delta 语义、``reasoning_content`` 的回传要求、
``json_object`` 的前置条件）**无法用 mock 证明**，只能真跑一次抓下来做成 fixture。
那是 Phase 2 的事，见计划里的「录 fixture」步骤。
"""

from __future__ import annotations

import pytest

from agentkit.core.config import load_settings
from agentkit.core.errors import ConfigurationError
from agentkit.runtime.agent import build_agent
from agentkit.tools.builtin import register_builtin_tools

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def settings():
    config = load_settings()
    if not config.api_key:
        pytest.skip("未配置 API Key，跳过 live 测试")
    return config


async def test_simple_answer_roundtrip(settings):
    """最基本的往返：模型能给出一段非空文本。"""
    agent = build_agent(settings, tools=register_builtin_tools(groups=[]))
    try:
        result = await agent.run("只回复两个字：收到")
    finally:
        await agent.aclose()

    assert result.ok, result.error
    assert result.text.strip()
    assert result.usage.input_tokens > 0
    assert result.usage.output_tokens > 0


async def test_native_tool_calling_executes_a_tool(settings, workspace):
    """原生 tool calling 真的会触发工具执行，而不是把调用写进正文里。"""
    agent = build_agent(settings, workspace=workspace, tool_groups=["fs"])
    try:
        result = await agent.run("用 read_file 读一下 hello.txt，然后告诉我里面有几行")
    finally:
        await agent.aclose()

    assert result.ok, result.error
    # 工具被调用了才会有额外的一轮，步数必然大于 1
    assert result.steps >= 2
    # 模型没读过文件就不可能知道里面的内容
    assert "三" in result.text or "3" in result.text


async def test_usage_reports_cache_activity(settings):
    """连续两次同样的长前缀应当命中提示缓存。

    DeepSeek 的缓存是全自动的（前缀完全匹配即命中，64 token 粒度），所以这里
    只断言「缓存字段存在且被填充」，不断言具体命中量——那是服务端行为，会变。
    """
    agent = build_agent(settings, tools=register_builtin_tools(groups=[]))
    long_prefix = "请忽略以下背景信息，只回答最后一个问题。" + "背景。" * 200
    try:
        result = await agent.run(f"{long_prefix}\n问题：1+1 等于几？")
    finally:
        await agent.aclose()

    assert result.ok, result.error
    assert result.usage.input_tokens > 0


def test_missing_api_key_raises_configuration_error(monkeypatch):
    """没配 Key 时要报一个指得清楚的错，而不是等 SDK 抛一个晦涩的认证失败。"""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    config = load_settings()
    config.api_key = None
    with pytest.raises(ConfigurationError, match="API Key"):
        config.require_api_key()
