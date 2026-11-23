import asyncio
from datetime import datetime, timedelta


class AgentLeaseTestClock:
    """Advance storage time while letting real async lease renewals finish."""

    def __init__(self, repository, now: datetime):
        self.now = now
        self.sleepers = []
        self.renewals = asyncio.Queue()
        self.renew_count = 0
        self.max_sleepers = 0
        repository._now = lambda: self.now
        original_renew = repository.renew_task_lease

        async def renew(*args, **kwargs):
            try:
                result = await original_renew(*args, **kwargs)
                self.renew_count += 1
                return result
            finally:
                self.renewals.put_nowait(None)

        repository.renew_task_lease = renew

    async def sleep(self, delay):
        future = asyncio.get_running_loop().create_future()
        entry = (self.now + timedelta(seconds=delay), future)
        self.sleepers.append(entry)
        self.max_sleepers = max(self.max_sleepers, len(self.sleepers))
        try:
            await future
        finally:
            self.sleepers.remove(entry)

    async def advance(self, seconds):
        for _ in range(seconds):
            self.now += timedelta(seconds=1)
            due = [
                future
                for deadline, future in self.sleepers
                if deadline <= self.now and not future.done()
            ]
            for future in due:
                future.set_result(None)
            for _future in due:
                await asyncio.wait_for(self.renewals.get(), timeout=2)
            await asyncio.sleep(0)
