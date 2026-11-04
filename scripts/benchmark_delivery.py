"""Synthetic queue benchmark, with no Telegram or database traffic.

Run from the repository root: .venv/bin/python scripts/benchmark_delivery.py
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot.utils.delivery import OrderedDispatcher


async def main():
    queue = OrderedDispatcher(capacity=500)
    calls = 0
    async def send():
        nonlocal calls
        await asyncio.sleep(0)
        calls += 1
    started = time.perf_counter()
    for message_id in range(5000):
        await queue.submit(22, ('source', message_id), send)
        await queue.submit(22, ('source', message_id), send)
    await queue.queues[22].join()
    elapsed = time.perf_counter() - started
    result = {**queue.snapshot(), 'input_events':10000, 'mock_deliveries':calls,
              'elapsed_seconds':round(elapsed, 4),
              'note':'Synthetic queue overhead only; this is not Telegram throughput or end-to-end latency.'}
    await queue.stop(22)
    print(json.dumps(result, indent=2))
    assert calls == 5000 and result['duplicates'] == 5000


if __name__ == '__main__':
    asyncio.run(main())
