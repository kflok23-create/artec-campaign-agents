from datetime import UTC, datetime, timedelta

import anyio
import pytest
from sqlalchemy import select, update

from db.models import Task
from worker import queue

pytestmark = pytest.mark.anyio


async def _add(sessionmaker, key="k1", **kw) -> int | None:
    async with sessionmaker() as s, s.begin():
        return await queue.enqueue(s, brand="artec", kind="test", idempotency_key=key, **kw)


async def _task(sessionmaker, task_id) -> Task:
    async with sessionmaker() as s:
        return await s.get(Task, task_id)


async def _expire_lease(sessionmaker, task_id):
    async with sessionmaker() as s, s.begin():
        await s.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(locked_until=datetime.now(UTC) - timedelta(seconds=1))
        )


async def _make_ready(sessionmaker, task_id):
    async with sessionmaker() as s, s.begin():
        await s.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(run_after=datetime.now(UTC) - timedelta(seconds=1))
        )


async def test_enqueue_is_idempotent(sessionmaker):
    first = await _add(sessionmaker, payload={"a": 1})
    again = await _add(sessionmaker, payload={"a": 2})
    assert first is not None and again is None
    async with sessionmaker() as s:
        rows = (await s.scalars(select(Task))).all()
    assert len(rows) == 1 and rows[0].payload == {"a": 1}


async def test_claim_and_finish(sessionmaker):
    task_id = await _add(sessionmaker, payload={"x": 1})
    claimed = await queue.claim(sessionmaker, worker_id="w1")
    assert claimed.id == task_id and claimed.attempts == 1 and claimed.payload == {"x": 1}
    assert await queue.claim(sessionmaker, worker_id="w2") is None  # held by w1
    assert await queue.finish(sessionmaker, claimed, {"ok": True})
    row = await _task(sessionmaker, task_id)
    assert (row.status, row.result, row.locked_until) == ("done", {"ok": True}, None)


async def test_future_tasks_wait(sessionmaker):
    await _add(sessionmaker, run_after=datetime.now(UTC) + timedelta(hours=1))
    assert await queue.claim(sessionmaker, worker_id="w1") is None


async def test_claim_filters_by_kind(sessionmaker):
    await _add(sessionmaker)
    assert await queue.claim(sessionmaker, worker_id="w1", kinds=["other"]) is None
    assert await queue.claim(sessionmaker, worker_id="w1", kinds=["test"]) is not None


async def test_parallel_claims_never_share_a_task(sessionmaker):
    for i in range(10):
        await _add(sessionmaker, key=f"k{i}")
    claimed: list[int] = []

    async def worker(name: str):
        while (task := await queue.claim(sessionmaker, worker_id=name)) is not None:
            claimed.append(task.id)

    async with anyio.create_task_group() as tg:
        for n in range(4):
            tg.start_soon(worker, f"w{n}")
    assert sorted(claimed) == list(range(1, 11))  # each task exactly once


async def test_expired_lease_is_reclaimed_and_old_owner_cannot_finish(sessionmaker):
    task_id = await _add(sessionmaker)
    dead = await queue.claim(sessionmaker, worker_id="dead")
    await _expire_lease(sessionmaker, task_id)
    alive = await queue.claim(sessionmaker, worker_id="alive")
    assert alive.id == task_id and alive.attempts == 2
    assert not await queue.finish(sessionmaker, dead, {"late": True})  # lease was taken over
    assert await queue.finish(sessionmaker, alive, {"ok": True})
    assert (await _task(sessionmaker, task_id)).result == {"ok": True}


async def test_errors_back_off_then_fail_on_the_third(sessionmaker):
    task_id = await _add(sessionmaker)
    statuses = []
    for _ in range(3):
        claimed = await queue.claim(sessionmaker, worker_id="w1")
        statuses.append(await queue.fail(sessionmaker, claimed, "boom"))
        row = await _task(sessionmaker, task_id)
        if row.status == "queued":
            assert row.run_after > datetime.now(UTC)  # backing off
            await _make_ready(sessionmaker, task_id)
    assert statuses == ["queued", "queued", "failed"]
    row = await _task(sessionmaker, task_id)
    assert (row.status, row.attempts, row.last_error) == ("failed", 3, "boom")


def test_backoff_doubles():
    assert [queue.backoff_seconds(n) for n in (1, 2, 3)] == [60, 120, 240]


async def test_non_retryable_error_fails_at_once(sessionmaker):
    task_id = await _add(sessionmaker)
    claimed = await queue.claim(sessionmaker, worker_id="w1")
    assert await queue.fail(sessionmaker, claimed, "bad input", retry=False) == "failed"
    assert (await _task(sessionmaker, task_id)).attempts == 1


async def test_worker_dying_on_last_attempt_fails_the_task(sessionmaker):
    task_id = await _add(sessionmaker, max_attempts=1)
    await queue.claim(sessionmaker, worker_id="dead")
    await _expire_lease(sessionmaker, task_id)
    assert await queue.claim(sessionmaker, worker_id="w2") is None
    row = await _task(sessionmaker, task_id)
    assert row.status == "failed" and "lease expired" in row.last_error


async def test_defer_does_not_count_the_attempt(sessionmaker):
    task_id = await _add(sessionmaker)
    claimed = await queue.claim(sessionmaker, worker_id="w1")
    later = datetime.now(UTC) + timedelta(hours=3)
    assert await queue.defer(sessionmaker, claimed, later, "daily Claude cap reached")
    row = await _task(sessionmaker, task_id)
    assert (row.status, row.attempts, row.locked_by) == ("queued", 0, None)
    assert abs((row.run_after - later).total_seconds()) < 1


async def test_extend_lease(sessionmaker):
    task_id = await _add(sessionmaker)
    claimed = await queue.claim(sessionmaker, worker_id="w1", lease_seconds=5)
    before = (await _task(sessionmaker, task_id)).locked_until
    assert await queue.extend_lease(sessionmaker, claimed, lease_seconds=600)
    assert (await _task(sessionmaker, task_id)).locked_until > before + timedelta(seconds=500)
