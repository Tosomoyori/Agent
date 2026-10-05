"""对 HTTP 服务做一次简单压测。

**测量目标**：给出**有边界、可复现**的性能数据，而非无依据的 QPS 数值。

**需注意该数值的构成。** 单次请求的耗时绝大部分用于等待模型响应，
框架自身（消息转换、事件分发、SSE 编码）占比很小。
脚本同时报告两者——将总延迟表述为框架性能是不准确的。

用法::

    # 先起服务
    uv run agentkit serve --port 8125 --db .agentkit/loadtest.db
    # 再压
    uv run python scripts/loadtest.py --port 8125 --levels 1,5,10 --runs-per-level 8
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

#: 一个简单但需要真实调用工具的任务——避免只用纯文本回答把工具链路跳过去。
PROMPT = "列出当前目录的文件，然后告诉我一共有几项。只回答数字。"


@dataclass
class Sample:
    """一次请求的观测结果。"""

    ok: bool
    total_ms: float = 0.0
    #: 从 run_completed 事件里读到的时间——引擎自己统计的耗时
    run_ms: int = 0
    steps: int = 0
    tokens: int = 0
    error: str | None = None


@dataclass
class LevelResult:
    concurrency: int
    samples: list[Sample] = field(default_factory=list)
    wall_s: float = 0.0

    @property
    def ok_count(self) -> int:
        return sum(1 for s in self.samples if s.ok)

    @property
    def failure_rate(self) -> float:
        return 1 - self.ok_count / len(self.samples) if self.samples else 0.0

    def percentiles(self, key: str) -> tuple[float, float, float]:
        values = sorted(
            getattr(s, key) for s in self.samples if s.ok and getattr(s, key)
        )
        if not values:
            return 0.0, 0.0, 0.0
        return (
            statistics.median(values),
            _pct(values, 0.95),
            max(values),
        )

    @property
    def throughput(self) -> float:
        return len(self.samples) / self.wall_s if self.wall_s else 0.0


def _pct(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    position = fraction * (len(values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = position - lower
    return float(values[lower] * (1 - weight) + values[upper] * weight)


async def _one_run(client, base: str, index: int) -> Sample:
    """跑一次完整流程：建 run → 订阅 SSE → 读完。"""
    started = time.perf_counter()
    try:
        response = await client.post(f"{base}/runs", json={"prompt": PROMPT})
        response.raise_for_status()
        run_id = response.json()["run_id"]

        sample = Sample(ok=False)
        async with client.stream("GET", f"{base}/runs/{run_id}/events") as stream:
            async for line in stream.aiter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                if event["type"] == "run_completed":
                    sample.ok = True
                    sample.run_ms = event["duration_ms"]
                    sample.steps = event["steps"]
                    sample.tokens = event["usage"]["input_tokens"] + event["usage"]["output_tokens"]
                elif event["type"] in ("run_failed", "run_cancelled"):
                    sample.error = f"{event['type']}: {event.get('message') or event.get('reason')}"

        sample.total_ms = (time.perf_counter() - started) * 1000
        return sample
    except Exception as exc:  # noqa: BLE001 - 压测里任何异常都算一次失败
        return Sample(
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            total_ms=(time.perf_counter() - started) * 1000,
        )


async def run_level(client, base: str, concurrency: int, runs: int) -> LevelResult:
    """在给定并发下跑若干次，用信号量限流。"""
    result = LevelResult(concurrency=concurrency)
    semaphore = asyncio.Semaphore(concurrency)

    async def guarded(index: int) -> Sample:
        async with semaphore:
            return await _one_run(client, base, index)

    started = time.perf_counter()
    result.samples = await asyncio.gather(*(guarded(i) for i in range(runs)))
    result.wall_s = time.perf_counter() - started
    return result


def render(results: list[LevelResult]) -> str:
    header = (
        "并发   成功/总数   失败率   墙钟(s)  吞吐(次/秒)   "
        "总延迟P50/P95/最大(ms)   引擎耗时P50(ms)"
    )
    lines = ["", header, "-" * len(header)]
    for result in results:
        p50, p95, worst = result.percentiles("total_ms")
        run_p50, _, _ = result.percentiles("run_ms")
        lines.append(
            f"{result.concurrency:>4}   "
            f"{result.ok_count:>4}/{len(result.samples):<6} "
            f"{result.failure_rate:>7.1%} "
            f"{result.wall_s:>8.1f} "
            f"{result.throughput:>11.2f} "
            f"{p50:>10.0f}/{p95:>6.0f}/{worst:>7.0f} "
            f"{run_p50:>18.0f}"
        )

    errors = [s.error for r in results for s in r.samples if s.error]
    if errors:
        lines += ["", "失败明细（前 5 条）:"]
        for error in errors[:5]:
            lines.append(f"  - {error}")

    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--levels", default="1,5,10", help="逗号分隔的并发档位")
    parser.add_argument("--runs-per-level", type=int, default=8, help="每档跑多少次")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    try:
        import httpx
    except ImportError:  # pragma: no cover
        print("需要 httpx（openai 的依赖里已有）", file=sys.stderr)
        return 2

    base = f"http://{args.host}:{args.port}"
    levels = [int(x) for x in args.levels.split(",") if x.strip()]

    async with httpx.AsyncClient(timeout=args.timeout) as client:
        try:
            health = await client.get(f"{base}/healthz")
            health.raise_for_status()
            print(f"服务正常: {health.json()}")
        except Exception as exc:  # noqa: BLE001
            print(f"连不上 {base}: {exc}", file=sys.stderr)
            return 1

        results = []
        for concurrency in levels:
            print(f"\n并发 {concurrency}：跑 {args.runs_per_level} 次…")
            # 每档之间留一点间隔，避免前一档的余温影响后一档
            await asyncio.sleep(1)
            results.append(await run_level(client, base, concurrency, args.runs_per_level))

    print(render(results))
    print(
        "\n注：总延迟里绝大部分是**等模型返回**的时间。"
        "「引擎耗时」是引擎自己统计的那一段，两者的差是 HTTP + SSE 传输开销。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
