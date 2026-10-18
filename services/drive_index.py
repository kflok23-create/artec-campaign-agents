"""Indexes a brand's Drive asset bank into the `assets` table.

Plain code does the indexing: walk the bank folder, upsert one row per image or video, derive
tags from the folder path and file name, set the consent flag from the folder convention in the
brand config, and mark files that disappeared as `removed`. Then the vision tagger looks at each
new or changed image, within the run's tagging budget and behind the daily Claude cap.

Re-running is always safe. Unchanged files are left alone and a changed file is tagged again.
Nothing is marked removed unless the whole bank was listed without an error.
"""

from __future__ import annotations

import io
import logging
import re
import secrets
from collections import Counter, deque
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

import anyio
from claude_agent_sdk import query as sdk_query
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agents import asset_tagger
from agents.asset_tagger.schema import AssetVision
from agents.runner import (
    AgentCallFailed,
    AgentOutputInvalid,
    ImageInput,
    QueryFn,
    RunContext,
    run_agent,
)
from db.models import AgentRun, Asset
from services.budget import BudgetExceeded
from services.config import BrandConfig, Settings
from services.drive import SHORTCUT_MIME, DriveClient, DriveError, DriveFile, DriveNotFound
from services.logs import ctx

log = logging.getLogger(__name__)

IMAGE_MIMES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif",
        "image/heic",
        "image/heif",
        "image/tiff",
        "image/bmp",
    }
)
# Formats Pillow opens directly. Others (HEIC, video) are tagged from Drive's thumbnail.
PILLOW_MIMES = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/gif", "image/tiff", "image/bmp"}
)
MAX_DOWNLOAD_BYTES = 40 * 1024 * 1024
MAX_THUMBNAIL_BYTES = 5 * 1024 * 1024
TAG_IMAGE_EDGE = 1024  # long edge of the copy the tagger sees: about 1,400 input tokens
MAX_DEPTH = 25
MAX_FILES = 50_000
MAX_TAGS = 45

_STOPWORDS = frozenset(
    "img image images photo photos pic pics picture dsc dscn dcim copy final edit edited new old "
    "the and of for with to in on at by a an jpg jpeg png webp heic heif gif mov mp4 file files "
    "untitled misc other".split()
)


class IndexConfigError(RuntimeError):
    """The bank cannot be indexed as configured (wrong folder ID, not shared). Retry won't help."""


def kind_of(mime_type: str) -> str | None:
    if mime_type in IMAGE_MIMES:
        return "image"
    if mime_type.startswith("video/"):
        return "video"
    return None


# --- Tags from names --------------------------------------------------------------------------

_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z])(?=[0-9])|(?<=[0-9])(?=[A-Za-z])")
_SPLIT = re.compile(r"[^0-9A-Za-zÀ-\U0010FFFF]+")


def words(name: str) -> list[str]:
    """Search words in a file or folder name: split on separators and camelCase, lowercased."""
    out = []
    for chunk in _SPLIT.split(name):
        if not chunk:
            continue
        parts = _CAMEL.sub(" ", chunk).split()
        if len(parts) > 1 and chunk.isalnum():
            out.append(chunk.lower())  # keep "artecrobo" as well as "artec" and "robo"
        out.extend(p.lower() for p in parts)
    return [
        w for w in out if w not in _STOPWORDS and len(w) >= 2 and not (w.isdigit() and len(w) < 3)
    ]


def name_tags(folders: list[str], file_name: str) -> list[str]:
    stem = file_name.rsplit(".", 1)[0] if "." in file_name else file_name
    return _dedupe(w for part in [*folders, stem] for w in words(part))[:30]


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for item in items:
        seen.setdefault(item, None)
    return list(seen)


def _norm_folder(name: str) -> str:
    return re.sub(r"[\s_]+", "-", name.strip().lower())


def is_consent_cleared(folders: list[str], consent_folder_names: list[str]) -> bool:
    """True if any folder on the path matches the brand's consent folder convention."""
    wanted = {_norm_folder(n) for n in consent_folder_names}
    return any(_norm_folder(f) in wanted for f in folders)


def merged_tags(name_tag_list: list[str], vision: dict[str, Any] | None) -> list[str]:
    vision_tags = list((vision or {}).get("tags") or [])
    return _dedupe([*vision_tags, *name_tag_list])[:MAX_TAGS]


# --- Walking the bank -------------------------------------------------------------------------


@dataclass(frozen=True)
class Found:
    file: DriveFile
    folders: list[str]
    kind: str


@dataclass
class WalkResult:
    found: list[Found] = field(default_factory=list)
    folders: int = 0
    shortcuts: int = 0
    skipped_types: Counter = field(default_factory=Counter)


async def walk(drive: DriveClient, root_id: str) -> WalkResult:
    """List every image and video under the bank folder. Raises on any listing error."""
    try:
        root = await drive.get(root_id)
    except DriveNotFound as e:
        raise IndexConfigError(
            f"asset bank folder {root_id} not found: check assets.drive_folder_id and that the "
            f"folder is shared with {drive.account_email}"
        ) from e
    if not root.is_folder:
        raise IndexConfigError(f"assets.drive_folder_id {root_id} is not a folder")
    result = WalkResult()
    pending: deque[tuple[str, list[str]]] = deque([(root.id, [])])
    visited = {root.id}
    while pending:
        folder_id, path = pending.popleft()
        if len(path) > MAX_DEPTH:
            raise IndexConfigError(f"folders nested deeper than {MAX_DEPTH} levels")
        async for item in drive.list_children(folder_id, drive_id=root.drive_id):
            if item.is_folder:
                if item.id not in visited:
                    visited.add(item.id)
                    pending.append((item.id, [*path, item.name]))
                    result.folders += 1
            elif item.mime_type == SHORTCUT_MIME:
                result.shortcuts += 1  # not followed: a shortcut can point outside the bank
            elif kind := kind_of(item.mime_type):
                result.found.append(Found(item, path, kind))
                if len(result.found) > MAX_FILES:
                    raise IndexConfigError(f"more than {MAX_FILES} files; is this the bank?")
            else:
                result.skipped_types[item.mime_type] += 1
    return result


# --- Syncing rows -----------------------------------------------------------------------------


@dataclass
class SyncReport:
    new: int = 0
    changed: int = 0
    restored: int = 0
    unchanged: int = 0
    removed: int = 0


def _content_changed(asset: Asset, f: DriveFile) -> bool:
    if asset.md5 or f.md5:
        return asset.md5 != f.md5
    return asset.drive_modified_at != f.modified_time


async def rebuild_search(session: AsyncSession, ids: list[int]) -> None:
    """Recompute the full-text vector: vision tags weigh most, then description and names."""
    if not ids:
        return
    await session.execute(
        text(
            """
            UPDATE assets SET search =
                setweight(to_tsvector('english', coalesce((
                    SELECT string_agg(t, ' ') FROM jsonb_array_elements_text(vision->'tags') t
                ), '')), 'A')
                || setweight(to_tsvector('english', coalesce(description, '')), 'B')
                || setweight(to_tsvector('english', array_to_string(name_tags, ' ')), 'B')
            WHERE id = ANY(:ids)
            """
        ),
        {"ids": ids},
    )


async def sync_assets(
    sessionmaker: async_sessionmaker, brand: BrandConfig, brand_id: int, found: list[Found]
) -> SyncReport:
    report = SyncReport()
    async with sessionmaker() as session, session.begin():
        rows = await session.scalars(select(Asset).where(Asset.brand_id == brand_id))
        existing = {a.drive_file_id: a for a in rows}
        touched: list[Asset] = []
        seen: set[str] = set()
        for item in found:
            f = item.file
            if f.id in seen:
                continue  # the same file in two folders: keep the first path
            seen.add(f.id)
            asset = existing.get(f.id)
            if asset is None:
                asset = Asset(
                    brand_id=brand_id,
                    drive_file_id=f.id,
                    public_token=secrets.token_urlsafe(18),
                    tagging_status="pending",
                    people="unknown",
                )
                session.add(asset)
                report.new += 1
            elif _content_changed(asset, f):
                asset.tagging_status = "pending"
                asset.vision = None
                asset.description = None
                asset.people = "unknown"
                asset.tagging_error = None
                report.changed += 1
            elif asset.status == "removed":
                report.restored += 1
            else:
                report.unchanged += 1
            ntags = name_tags(item.folders, f.name)
            asset.name = f.name
            asset.folder_path = "/".join(item.folders)
            asset.mime_type = f.mime_type
            asset.kind = item.kind
            asset.width, asset.height, asset.duration_ms = f.width, f.height, f.duration_ms
            asset.size_bytes = f.size
            asset.md5 = f.md5
            asset.drive_modified_at = f.modified_time
            asset.consent_cleared = is_consent_cleared(
                item.folders, brand.assets.consent_folder_names
            )
            asset.name_tags = ntags
            asset.tags = merged_tags(ntags, asset.vision)
            asset.status = "active"
            asset.last_seen_at = func.now()
            touched.append(asset)
        for drive_file_id, asset in existing.items():
            if drive_file_id not in seen and asset.status == "active":
                asset.status = "removed"
                report.removed += 1
        await session.flush()
        await rebuild_search(session, [a.id for a in touched])
    return report


# --- Vision tagging ---------------------------------------------------------------------------


@dataclass
class TaggingReport:
    tagged: int = 0
    failed: int = 0
    skipped: int = 0
    pending_left: int = 0
    stopped: str | None = None  # "run budget" or "daily cap" when tagging stopped early


def prepare_jpeg(data: bytes, edge: int = TAG_IMAGE_EDGE) -> bytes:
    """A small upright RGB JPEG of an image, which is all the tagger needs to see."""
    with Image.open(io.BytesIO(data)) as img:
        img = ImageOps.exif_transpose(img)
        img.thumbnail((edge, edge))
        out = io.BytesIO()
        img.convert("RGB").save(out, format="JPEG", quality=85)
        return out.getvalue()


def _sized_thumbnail(link: str, edge: int = TAG_IMAGE_EDGE) -> str:
    # Drive thumbnail links end in "=s220"; ask for a larger rendition.
    return re.sub(r"=s\d+$", f"=s{edge}", link)


async def preview(drive: DriveClient, asset: Asset) -> bytes | None:
    """JPEG bytes for the tagger, from the file itself or Drive's thumbnail. None if neither."""
    if asset.mime_type in PILLOW_MIMES and (asset.size_bytes or 0) <= MAX_DOWNLOAD_BYTES:
        data = await drive.download(asset.drive_file_id, max_bytes=MAX_DOWNLOAD_BYTES)
        try:
            return await anyio.to_thread.run_sync(prepare_jpeg, data)
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            log.info("image did not open; trying its thumbnail", extra=ctx(asset_id=asset.id))
    fresh = await drive.get(asset.drive_file_id)  # thumbnail links expire within hours
    if not fresh.thumbnail_link:
        return None
    data = await drive.thumbnail(
        _sized_thumbnail(fresh.thumbnail_link), max_bytes=MAX_THUMBNAIL_BYTES
    )
    try:
        return await anyio.to_thread.run_sync(prepare_jpeg, data)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        return None


async def _save_vision(sessionmaker: async_sessionmaker, asset: Asset, vision: AssetVision) -> None:
    data = vision.model_dump()
    async with sessionmaker() as session, session.begin():
        await session.execute(
            update(Asset)
            .where(Asset.id == asset.id, Asset.md5.is_not_distinct_from(asset.md5))
            .values(
                vision=data,
                description=vision.description,
                people=vision.people,
                tags=merged_tags(asset.name_tags, data),
                tagging_status="done",
                tagging_error=None,
                tagged_md5=asset.md5,
                tagged_at=func.now(),
            )
        )
        await rebuild_search(session, [asset.id])


async def _mark(sessionmaker: async_sessionmaker, asset_id: int, status: str, why: str) -> None:
    async with sessionmaker() as session, session.begin():
        await session.execute(
            update(Asset)
            .where(Asset.id == asset_id)
            .values(tagging_status=status, tagging_error=why[:500])
        )


async def tag_pending(
    *,
    sessionmaker: async_sessionmaker,
    settings: Settings,
    brand: BrandConfig,
    brand_id: int,
    drive: DriveClient,
    run_id: str,
    task_id: int | None = None,
    budget_usd: float,
    retag_failed: bool = False,
    limit: int | None = None,
    query_fn: QueryFn = sdk_query,
) -> TaggingReport:
    """Tag pending images, a few at a time, until done or the run's tagging budget is used."""
    report = TaggingReport()
    statuses = ["pending", "failed"] if retag_failed else ["pending"]
    async with sessionmaker() as session:
        query = (
            select(Asset)
            .where(
                Asset.brand_id == brand_id,
                Asset.status == "active",
                Asset.tagging_status.in_(statuses),
            )
            .order_by(Asset.id)
        )
        if limit:
            query = query.limit(limit)
        todo = deque((await session.scalars(query)).all())

    budget = Decimal(str(budget_usd))
    ceiling = Decimal(str(asset_tagger.CEILING_USD))
    committed = Decimal(0)  # spent so far plus the ceiling of every call in flight
    spec = asset_tagger.spec(brand)
    rc = RunContext(settings, brand, sessionmaker, run_id=run_id, task_id=task_id)

    async def tag_one(asset: Asset) -> None:
        nonlocal committed
        try:
            image = await preview(drive, asset)
        except DriveError as e:
            await _mark(sessionmaker, asset.id, "failed", f"preview: {e}")
            report.failed += 1
            return
        if image is None:
            await _mark(sessionmaker, asset.id, "skipped", f"no preview for {asset.mime_type}")
            report.skipped += 1
            return
        committed += ceiling
        cost = ceiling
        try:
            result = await run_agent(
                spec,
                asset_tagger.user_prompt(asset.folder_path, asset.name),
                rc,
                images=[ImageInput("image/jpeg", image)],
                query_fn=query_fn,
            )
            cost = result.cost_usd
            await _save_vision(sessionmaker, asset, result.output)
            report.tagged += 1
        except BudgetExceeded:
            cost = Decimal(0)  # refused before any call: nothing was spent
            raise
        except (AgentOutputInvalid, AgentCallFailed) as e:
            # The ledger has the exact cost; here the ceiling is a safe upper bound.
            await _mark(sessionmaker, asset.id, "failed", str(e))
            report.failed += 1
        finally:
            committed += cost - ceiling

    async def worker() -> None:
        while todo and report.stopped is None:
            if committed + ceiling > budget:
                report.stopped = "run budget"
                return
            asset = todo.popleft()
            try:
                await tag_one(asset)
            except BudgetExceeded:
                report.stopped = "daily cap"
                todo.appendleft(asset)
                return

    async with anyio.create_task_group() as tg:
        for _ in range(settings.asset_tagging_concurrency):
            tg.start_soon(worker)
    report.pending_left = len(todo)
    if report.stopped == "daily cap":
        log.error(
            "daily Claude cap reached; asset tagging paused until the next budget day",
            extra=ctx(run_id=run_id, pending_left=report.pending_left, alert=True),
        )
    return report


# --- One full run -----------------------------------------------------------------------------


@dataclass
class IndexReport:
    folders: int
    files_seen: int
    images: int
    videos: int
    shortcuts: int
    skipped_types: dict[str, int]
    sync: SyncReport
    tagging: TaggingReport | None
    tagging_cost_usd: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


async def run_cost(sessionmaker: async_sessionmaker, run_id: str) -> Decimal:
    async with sessionmaker() as session:
        total = await session.scalar(
            select(func.coalesce(func.sum(AgentRun.cost_usd), 0)).where(AgentRun.run_id == run_id)
        )
    return Decimal(total)


async def index_brand(
    *,
    sessionmaker: async_sessionmaker,
    settings: Settings,
    brand: BrandConfig,
    brand_id: int,
    drive: DriveClient,
    run_id: str,
    task_id: int | None = None,
    tag: bool = True,
    retag_failed: bool = False,
    tag_limit: int | None = None,
    query_fn: QueryFn = sdk_query,
) -> IndexReport:
    walked = await walk(drive, brand.assets.drive_folder_id)
    synced = await sync_assets(sessionmaker, brand, brand_id, walked.found)
    log.info(
        "asset bank synced",
        extra=ctx(run_id=run_id, files=len(walked.found), **asdict(synced)),
    )
    tagging = None
    if tag and brand.assets.tagging_budget_usd_per_run > 0:
        tagging = await tag_pending(
            sessionmaker=sessionmaker,
            settings=settings,
            brand=brand,
            brand_id=brand_id,
            drive=drive,
            run_id=run_id,
            task_id=task_id,
            budget_usd=brand.assets.tagging_budget_usd_per_run,
            retag_failed=retag_failed,
            limit=tag_limit,
            query_fn=query_fn,
        )
    kinds = Counter(item.kind for item in walked.found)
    report = IndexReport(
        folders=walked.folders,
        files_seen=len(walked.found),
        images=kinds["image"],
        videos=kinds["video"],
        shortcuts=walked.shortcuts,
        skipped_types=dict(walked.skipped_types),
        sync=synced,
        tagging=tagging,
        tagging_cost_usd=str(await run_cost(sessionmaker, run_id)),
    )
    log.info("asset index run finished", extra=ctx(run_id=run_id, **report.as_dict()))
    return report
