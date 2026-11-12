"""Decide one status per PR. Pure functions: no I/O, all inputs explicit."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .config import Config

FAILING = {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED", "ERROR"}
RUNNING = {"IN_PROGRESS", "QUEUED", "PENDING", "WAITING", "REQUESTED", "EXPECTED"}
PASSING = {"SUCCESS", "NEUTRAL", "SKIPPED"}

NEEDS_YOU, IN_FLIGHT, DONE, PARKED = "needs_you", "in_flight", "done", "parked"

LABELS = {
    "merged": "Merged",
    "dropped_from_queue": "Dropped from merge queue",
    "ci_failed": "CI failed",
    "in_merge_queue": "In merge queue",
    "assigned_ready": "Assigned, ready for review",
    "review_requested": "Review requested",
    "new_comments": "New comments",
    "waiting": "Waiting on others",
}
GROUP_OF = {
    "merged": DONE,
    "dropped_from_queue": NEEDS_YOU,
    "ci_failed": NEEDS_YOU,
    "in_merge_queue": IN_FLIGHT,
    "assigned_ready": NEEDS_YOU,
    "review_requested": NEEDS_YOU,
    "new_comments": NEEDS_YOU,
    "waiting": IN_FLIGHT,
}


@dataclass
class UserState:
    """What the local user did to this PR (from the SQLite store)."""

    first_seen: datetime
    seen_at: datetime | None = None
    hidden_at: datetime | None = None


@dataclass
class Row:
    id: str
    repo: str
    number: int
    title: str
    url: str
    author: str
    is_draft: bool
    relations: list[str]
    status: str
    label: str
    group: str
    detail: str
    parked_reason: str | None
    last_activity: str
    created_at: str
    failing_checks: list[dict[str, Any]] = field(default_factory=list)
    queue_position: int | None = None
    new_comment_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def norm_login(login: str | None) -> str:
    login = (login or "").strip()
    if login.startswith("app/"):
        login = login[4:]
    if login.endswith("[bot]"):
        login = login[: -len("[bot]")]
    return login.lower()


def _head(pr: dict[str, Any]) -> dict[str, Any] | None:
    nodes = (pr.get("commits") or {}).get("nodes") or []
    return nodes[0]["commit"] if nodes else None


def check_summary(pr: dict[str, Any], cfg: Config) -> tuple[list[dict[str, Any]], bool, int]:
    """(failing checks, any non-ignored check still running, failing count from counts-only)."""
    ignore = set(cfg.ignore_checks)
    by_name: dict[str, list[dict[str, Any]]] = {}
    for c in pr.get("_checks") or []:
        if c["name"] in ignore:
            continue
        by_name.setdefault(c["name"], []).append(c)
    failing, running = [], False
    for name, runs in by_name.items():
        states = {r["state"] for r in runs}
        if states & PASSING:
            continue  # a re-run (or sibling job of the same name) passed
        if states & FAILING:
            failing.append(next(r for r in runs if r["state"] in FAILING))
        elif states & RUNNING:
            running = True
    counts = pr.get("_check_counts") or {}
    listed_runs = any(c["kind"] == "check_run" for c in pr.get("_checks") or [])
    unlisted_failing = 0
    if not listed_runs:
        # Names weren't fetched (stale PR or nothing interesting): trust the counts.
        unlisted_failing = sum(counts.get(s, 0) for s in FAILING)
        if any(counts.get(s, 0) for s in RUNNING):
            running = True
    return failing, running, unlisted_failing


def _queue_drop(pr: dict[str, Any], me: str) -> dict[str, Any] | None:
    if pr.get("mergeQueueEntry") or pr.get("merged"):
        return None
    events = [
        e for e in (pr.get("timelineItems") or {}).get("nodes") or []
        if e and e.get("__typename") in ("AddedToMergeQueueEvent", "RemovedFromMergeQueueEvent")
    ]
    if not events:
        return None
    last = max(events, key=lambda e: e["createdAt"])
    if last["__typename"] != "RemovedFromMergeQueueEvent":
        return None
    if norm_login((last.get("actor") or {}).get("login")) == me:
        return None  # you dequeued it on purpose
    head = _head(pr)
    if head and (parse_ts(head.get("committedDate")) or datetime.min) > parse_ts(last["createdAt"]):
        return None  # new commit since the drop; normal rules apply
    return last


def _new_comments(pr: dict[str, Any], me: str, cfg: Config, since: datetime) -> int:
    ignored = {norm_login(b) for b in cfg.bot_logins_ignored_for_comments}
    n = 0
    items = [(c.get("author"), c.get("createdAt")) for c in (pr.get("comments") or {}).get("nodes") or []]
    items += [(r.get("author"), r.get("submittedAt")) for r in (pr.get("reviews") or {}).get("nodes") or []]
    for author, ts in items:
        if not author or author.get("__typename") == "Bot":
            continue
        login = norm_login(author.get("login"))
        if login == me or login in ignored:
            continue
        t = parse_ts(ts)
        if t and t > since:
            n += 1
    return n


def classify(
    pr: dict[str, Any], relations: set[str], cfg: Config, user: UserState, now: datetime
) -> Row | None:
    """Return the row to show, or None if the PR should not be listed at all."""
    me = norm_login(cfg.github_login)
    updated = parse_ts(pr["updatedAt"])
    head = _head(pr)
    failing, running, unlisted_failing = check_summary(pr, cfg)
    detail = ""
    queue_position = None
    new_count = 0

    if pr.get("merged"):
        merged_at = parse_ts(pr.get("mergedAt"))
        if not merged_at or now - merged_at > timedelta(hours=cfg.merged_visible_hours):
            return None
        status = "merged"
    elif pr.get("state") == "CLOSED":
        return None
    elif drop := _queue_drop(pr, me):
        status = "dropped_from_queue"
        detail = (drop.get("reason") or "removed").replace("_", " ")
    elif failing or unlisted_failing:
        status = "ci_failed"
        detail = ", ".join(c["name"] for c in failing[:3]) or f"{unlisted_failing} failing checks"
        if len(failing) > 3:
            detail += f" +{len(failing) - 3} more"
    elif entry := pr.get("mergeQueueEntry"):
        status = "in_merge_queue"
        queue_position = entry.get("position")
        detail = f"position {queue_position}" if queue_position is not None else (entry.get("state") or "").lower()
    elif _assigned_ready(pr, me, cfg, running, head):
        status = "assigned_ready"
    elif _review_requested(pr, me, relations):
        status = "review_requested"
    elif (new_count := _new_comments(pr, me, cfg, user.seen_at or user.first_seen)) > 0:
        status = "new_comments"
        detail = f"{new_count} new"
    else:
        status = "waiting"
        detail = "draft" if pr.get("isDraft") else ("CI running" if running else "")

    if status != "new_comments":
        new_count = _new_comments(pr, me, cfg, user.seen_at or user.first_seen)

    group = GROUP_OF[status]
    parked_reason = None
    if status != "merged":
        if user.hidden_at and not (updated and updated > user.hidden_at):
            parked_reason = "hidden"
        elif updated and now - updated > timedelta(days=cfg.stale_days):
            parked_reason = f"no activity for {(now - updated).days}d"
        if parked_reason:
            group = PARKED

    return Row(
        id=pr["id"],
        repo=pr["repository"]["nameWithOwner"],
        number=pr["number"],
        title=pr["title"],
        url=pr["url"],
        author=norm_login((pr.get("author") or {}).get("login")),
        is_draft=bool(pr.get("isDraft")),
        relations=sorted(relations),
        status=status,
        label=LABELS[status],
        group=group,
        detail=detail,
        parked_reason=parked_reason,
        last_activity=pr["updatedAt"],
        created_at=pr["createdAt"],
        failing_checks=[{"name": c["name"], "url": c.get("url")} for c in failing],
        queue_position=queue_position,
        new_comment_count=new_count,
    )


def _assigned_ready(pr: dict[str, Any], me: str, cfg: Config, running: bool, head: dict[str, Any] | None) -> bool:
    """Someone else's PR assigned to you, finished (not draft, checks done), that you haven't reviewed at its head."""
    author = norm_login((pr.get("author") or {}).get("login"))
    if author == me:
        return False
    if cfg.assigned_ready_authors and author not in {norm_login(a) for a in cfg.assigned_ready_authors}:
        return False
    if me not in {norm_login(a["login"]) for a in (pr.get("assignees") or {}).get("nodes") or []}:
        return False
    if pr.get("isDraft") or running:
        return False
    head_oid = head and head.get("oid")
    for r in (pr.get("reviews") or {}).get("nodes") or []:
        if norm_login((r.get("author") or {}).get("login")) == me and (r.get("commit") or {}).get("oid") == head_oid:
            return False  # you already reviewed the current head
    return True


def _review_requested(pr: dict[str, Any], me: str, relations: set[str]) -> bool:
    for n in (pr.get("reviewRequests") or {}).get("nodes") or []:
        rr = n.get("requestedReviewer") or {}
        if rr.get("__typename") == "User" and norm_login(rr.get("login")) == me:
            return True
    return "team_review_requested" in relations
