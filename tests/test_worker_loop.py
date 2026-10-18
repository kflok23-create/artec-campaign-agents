from datetime import UTC, datetime, timedelta

import anyio
import pytest
from sqlalchemy import update

from db.models import Task
from services import budget
from services.brands import ensure_brand
from worker import queue, scheduler
from worker.main import run_task
from worker.tasks import NonRetryable, WorkerContext

pytestmark = pytest.mark.anyio


@pytest.fixture
async def wctx(settings, brand, sessionmaker) -> WorkerContext:
    async with sessionmaker() as s, s.begin():
        brand_id = await ensure_brand(s, brand)
    fast = settings.model_copy(update={"worker_shutdown_grace_seconds": 0})
    return WorkerContext(fast, brand, brand_id, sessionmaker)


async def _claim(wctx: WorkerContext, kind="job") -> queue.ClaimedTask:
    async with wctx.sessionmaker() as s, s.begin():
        await queue.enqueue(s, brand="artec", kind=kind, idempotency_key=f"{kind}:1")
    return await queue.claim(wctx.sessionmaker, worker_id="w1")


async def _row(wctx: WorkerContext, task_id: int) -> Task:
    async with wctx.sessionmaker() as s:
        return await s.get(Task, task_id)


async def test_handler_result_is_stored(wctx):
    async def ok(task, w):
        return {"indexed": 3}

    task = await _claim(wctx)
    assert await run_task(task, wctx, anyio.Event(), handlers={"job": ok}) == "done"
    row = await _row(wctx, task.id)
    assert (row.status, row.result) == ("done", {"indexed": 3})


async def test_handler_error_is_retried_later(wctx):
    async def boom(task, w):
        raise RuntimeError("drive timeout")

    task = await _claim(wctx)
    assert await run_task(task, wctx, anyio.Event(), handlers={"job": boom}) == "queued"
    row = await _row(wctx, task.id)
    assert row.last_error == "RuntimeError: drive timeout"
    assert row.run_after > datetime.now(UTC)


async def test_non_retryable_error_fails_now(wctx):
    async def bad(task, w):
        raise NonRetryable("folder not shared with the service account")

    task = await _claim(wctx)
    assert await run_task(task, wctx, anyio.Event(), handlers={"job": bad}) == "failed"


async def test_budget_cap_pauses_until_next_budget_day(wctx):
    async def costly(task, w):
        raise budget.BudgetExceeded("daily cap reached")

    task = await _claim(wctx)
    assert await run_task(task, wctx, anyio.Event(), handlers={"job": costly}) == "queued"
    row = await _row(wctx, task.id)
    assert row.attempts == 0  # a budget pause is not an error
    assert row.run_after == budget.next_local_day_start(wctx.brand)


async def test_shutdown_puts_a_running_task_back(wctx):
    stop = anyio.Event()
    started = anyio.Event()

    async def slow(task, w):
        started.set()
        await anyio.sleep(60)

    task = await _claim(wctx)
    async with anyio.create_task_group() as tg:

        async def trigger():
            await started.wait()
            stop.set()

        tg.start_soon(trigger)
        with anyio.fail_after(5):
            status = await run_task(task, wctx, stop, handlers={"job": slow})
    assert status == "queued"
    row = await _row(wctx, task.id)
    assert (row.status, row.attempts, row.locked_by) == ("queued", 0, None)


async def test_lost_lease_abandons_the_task(wctx):
    async def slow(task, w):
        await anyio.sleep(60)

    task = await _claim(wctx)
    async with wctx.sessionmaker() as s, s.begin():  # another worker took it over
        await s.execute(update(Task).where(Task.id == task.id).values(locked_by="w2"))
    with anyio.fail_after(5):
        await run_task(task, wctx, anyio.Event(), handlers={"job": slow}, lease_seconds=0.3)
    assert (await _row(wctx, task.id)).locked_by == "w2"  # the new owner's claim is untouched


# --- Scheduler --------------------------------------------------------------------------------


def test_buckets_are_stable_within_an_interval():
    job = scheduler.Periodic("index_assets", timedelta(minutes=60))
    t = datetime(2026, 10, 18, 7, 5, tzinfo=UTC)
    assert scheduler.idempotency_key(job, "artec", t) == scheduler.idempotency_key(
        job, "artec", t + timedelta(minutes=50)
    )
    assert scheduler.idempotency_key(job, "artec", t) != scheduler.idempotency_key(
        job, "artec", t + timedelta(minutes=60)
    )
    assert scheduler.bucket_start(job.every, t) == datetime(2026, 10, 18, 7, 0, tzinfo=UTC)


async def test_enqueue_due_runs_once_per_bucket(wctx):
    jobs = [scheduler.Periodic("index_assets", timedelta(minutes=60))]
    now = datetime.now(UTC)
    first = await scheduler.enqueue_due(wctx.sessionmaker, wctx.brand, jobs, now)
    again = await scheduler.enqueue_due(wctx.sessionmaker, wctx.brand, jobs, now)
    assert len(first) == 1 and again == []


def test_indexing_waits_for_folder_and_credentials(settings, brand):
    assert scheduler.periodic_jobs(settings, brand) == []
    assert scheduler.missing_prerequisites(settings, brand) == [
        "assets.drive_folder_id in brands/artec.yaml",
        "GOOGLE_SERVICE_ACCOUNT_JSON",
    ]
    ready_brand = brand.model_copy(
        update={"assets": brand.assets.model_copy(update={"drive_folder_id": "folder123"})}
    )
    ready = settings.model_copy(update={"google_service_account_json": "e30="})
    (job,) = scheduler.periodic_jobs(ready, ready_brand)
    assert (job.kind, job.every) == ("index_assets", timedelta(minutes=60))


async def test_work_loop_runs_queued_tasks_and_stops(wctx, monkeypatch):
    from worker import main, tasks

    done = anyio.Event()

    async def job(task, w):
        done.set()
        return {"ran": task.payload["n"]}

    monkeypatch.setitem(tasks.HANDLERS, "loop_test", job)
    async with wctx.sessionmaker() as s, s.begin():
        task_id = await queue.enqueue(
            s, brand="artec", kind="loop_test", idempotency_key="loop:1", payload={"n": 7}
        )
    stop = anyio.Event()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            tg.start_soon(main._work, wctx, stop, "w1")
            await done.wait()
            stop.set()
    row = await _row(wctx, task_id)
    for _ in range(50):  # the loop records the result just after the handler returns
        if row.status == "done":
            break
        await anyio.sleep(0.05)
        row = await _row(wctx, task_id)
    assert (row.status, row.result) == ("done", {"ran": 7})
