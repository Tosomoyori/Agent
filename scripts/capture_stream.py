"""抓取 provider 的原始流式响应，并分析其 delta 语义。

**这个脚本存在的理由**：流式 tool_call 的增量语义各家不同，而且文档里通常不写。
具体到 DeepSeek，社区反馈它可能以「累积重发」的方式返回 ``arguments``——
每个 chunk 重发「到目前为止的全部内容」而不是只发增量。如果是这样，客户端无脑
``+=`` 就会拼出 ``{...}{...}``，``json.loads`` 失败，然后被静默吞成空参数。

这类行为**用 mock 证明不了**，只能真跑一次抓下来。跑完把原始 chunk 存成 fixture，
之后测试重放 fixture 即可——既不需要联网，也不需要每次花 API 费用。

用法::

    uv run python scripts/capture_stream.py            # 抓取并保存 fixture
    uv run python scripts/capture_stream.py --show     # 同时打印原始 chunk
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

# 允许直接以脚本方式运行（无需先安装包）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agentkit.core.config import load_settings  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

#: 触发一次带两个参数的工具调用，用于观察 arguments 是怎么分片的。
TOOL_CALL_PROMPT = "用 read_file 读取 /etc/hosts，编码用 utf-8。"

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取指定文件的内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "文件路径"},
                    "encoding": {"type": "string", "description": "文本编码，默认 utf-8"},
                },
                "required": ["file_path"],
            },
        },
    }
]


def _client():
    from openai import AsyncOpenAI

    settings = load_settings()
    return AsyncOpenAI(
        api_key=settings.require_api_key(),
        base_url=settings.base_url,
        max_retries=0,
    )


async def _capture(
    name: str, *, tools: list[dict] | None = None, **request: Any
) -> list[dict]:
    """跑一次流式请求，返回原始 chunk 的 JSON 列表。"""
    client = _client()
    try:
        stream = await client.chat.completions.create(
            stream=True,
            tools=tools,
            **request,
        )
        chunks: list[dict] = []
        async for chunk in stream:
            # exclude_none 让 fixture 只保留服务端真正发来的字段——
            # 这对判断「某个字段是否出现」很关键
            chunks.append(json.loads(chunk.model_dump_json(exclude_none=True)))
        return chunks
    finally:
        await client.close()


# ---------------------------------------------------------------- 分析


def analyze_tool_call_deltas(chunks: list[dict]) -> dict[str, Any]:
    """判断 tool_call 的 arguments 是增量还是累积重发。

    判据：把所有 ``arguments`` 片段按顺序拼起来，
      * 若拼接结果能解析成 JSON，而**单独任一片段**不能 → 增量语义；
      * 若每个片段自身就是不断增长的前缀 → 累积重发语义。
    """
    fragments: list[str] = []
    indices: list[int | None] = []
    ids: list[str | None] = []
    names: list[str | None] = []

    for chunk in chunks:
        for choice in chunk.get("choices") or []:
            for call in (choice.get("delta") or {}).get("tool_calls") or []:
                fn = call.get("function") or {}
                if fn.get("arguments") is not None:
                    fragments.append(fn["arguments"])
                indices.append(call.get("index"))
                ids.append(call.get("id"))
                names.append(fn.get("name"))

    joined = "".join(fragments)

    def parses_object(text: str) -> bool:
        """片段本身是不是一个完整的 JSON **对象**。

        只认对象，不认任意合法 JSON——``json.loads("8")`` 是合法的，那只是个
        数字片段，用它判断「参数是否完整」会得出错误的结论。
        """
        try:
            return isinstance(json.loads(text), dict)
        except (json.JSONDecodeError, TypeError):
            return False

    cumulative = (
        len(fragments) > 1
        and all(b.startswith(a) for a, b in zip(fragments, fragments[1:], strict=False))
        and len({len(f) for f in fragments}) == len(fragments)
    )

    return {
        "fragment_count": len(fragments),
        "fragments": fragments,
        "joined": joined,
        "joined_parses": parses_object(joined),
        "any_fragment_is_complete_object": any(
            parses_object(f) for f in fragments[:-1]
        ),
        "looks_cumulative": cumulative,
        "distinct_indices": sorted({i for i in indices if i is not None}),
        "ids_seen": [i for i in ids if i],
        "id_repeated_in_later_chunks": len([i for i in ids if i]) > len(set(i for i in ids if i)),
        "names_seen": [n for n in names if n],
        "finish_reasons": [
            c.get("finish_reason") for ch in chunks for c in (ch.get("choices") or [])
            if c.get("finish_reason")
        ],
    }


def summarize_usage(chunks: list[dict]) -> dict[str, Any]:
    """看 usage 出现在哪些 chunk 里、字段长什么样。"""
    seen = [ch["usage"] for ch in chunks if ch.get("usage")]
    return {
        "chunks_with_usage": len(seen),
        "position": (
            "最后一个 chunk" if seen and chunks[-1].get("usage") else "非末尾位置"
        ),
        "shape": seen[-1] if seen else None,
    }


# ---------------------------------------------------------------- 主流程


async def run(show: bool = False) -> int:
    settings = load_settings()
    print(f"模型: {settings.model}    端点: {settings.base_url}\n")

    results: dict[str, Any] = {}

    # --- 场景 1：流式工具调用 ---
    print("=" * 72)
    print("场景 1：流式工具调用（观察 arguments 的分片方式）")
    print("=" * 72)
    chunks = await _capture(
        "tool_call",
        tools=TOOLS,
        model=settings.model,
        messages=[{"role": "user", "content": TOOL_CALL_PROMPT}],
    )
    if show:
        for i, chunk in enumerate(chunks, 1):
            print(f"[{i:03d}] {json.dumps(chunk, ensure_ascii=False)[:300]}")

    analysis = analyze_tool_call_deltas(chunks)
    results["tool_call"] = {"chunks": chunks, "analysis": analysis}

    print(f"  chunk 总数        : {len(chunks)}")
    print(f"  arguments 片段数  : {analysis['fragment_count']}")
    for i, frag in enumerate(analysis["fragments"], 1):
        print(f"    片段 {i}: {frag!r}")
    print(f"  拼接后是完整对象  : {analysis['joined_parses']}")
    print(f"  中间片段即完整对象: {analysis['any_fragment_is_complete_object']}")
    print(f"  疑似累积重发      : {analysis['looks_cumulative']}")
    print(f"  id 是否在后续重复 : {analysis['id_repeated_in_later_chunks']}")
    print(f"  finish_reason     : {analysis['finish_reasons']}")
    print(f"  usage 情况        : {summarize_usage(chunks)}")

    # --- 场景 2：纯文本流式 + include_usage ---
    print()
    print("=" * 72)
    print("场景 2：纯文本流式，显式要求 usage")
    print("=" * 72)
    chunks2 = await _capture(
        "text",
        model=settings.model,
        messages=[{"role": "user", "content": "数到五，只输出数字。"}],
        stream_options={"include_usage": True},
    )
    if show:
        for i, chunk in enumerate(chunks2, 1):
            print(f"[{i:03d}] {json.dumps(chunk, ensure_ascii=False)[:300]}")

    text = "".join(
        (c.get("delta") or {}).get("content") or ""
        for ch in chunks2
        for c in (ch.get("choices") or [])
    )
    usage = summarize_usage(chunks2)
    results["text"] = {"chunks": chunks2, "text": text, "usage": usage}

    print(f"  chunk 总数   : {len(chunks2)}")
    print(f"  拼出的文本   : {text!r}")
    print(f"  usage        : {usage}")

    # --- 保存 fixture ---
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    out = FIXTURE_DIR / f"{settings.model}_stream.json"
    out.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n原始 chunk 已保存: {out.relative_to(FIXTURE_DIR.parent.parent)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--show", action="store_true", help="打印每个原始 chunk")
    args = parser.parse_args()
    return asyncio.run(run(show=args.show))


if __name__ == "__main__":
    raise SystemExit(main())
