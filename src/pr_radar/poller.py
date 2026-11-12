"""One poll = searches + detail fetch + store update. Plus the snapshot the UI reads."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from .config import Config
from .github import AuthError, NetworkError, Usage
from .status import DONE, IN_FLIGHT, NEEDS_YOU, PARKED, classify
from .store import Store, iso

log = logging.getLogger("pr_radar")
GROUP_ORDER = {NEEDS_YOU: 0, IN_FLIGHT: 1, DONE: 2, PARKED: 3}
LOOP_SLICE_SECONDS = 10   # loop re-checks the wall clock this often, so a wake from sleep polls promptly
RETRY_SECONDS = 15        # after a first network failure (e.g. Wi-Fi still joining after wake)
LOOP_ALIVE_SECONDS = 120  # loop heartbeat older than this (monotonic) means the poll thread is stuck


class Client(Protocol):
    def involved(self, repos: tuple[str, ...], include_team: bool, usage: Usage) -> dict[str, set[str]]: ...
    def details(self, ids: list[str], usage: Usage, list_checks_since: str | None = None) -> dict[str, dict[str, Any]]: ...


def process_stats() -> dict[str, Any]:
    """Current RSS (via ps — getrusage only gives the peak on macOS) and CPU time."""
    rss_mb = None
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True, timeout=5)
        rss_mb = round(int(out.stdout.strip()) / 1024, 1)
    except (ValueError, OSError, subprocess.SubprocessError):
        pass
    return {"rss_mb": rss_mb, "cpu_seconds": round(time.process_time(), 2), "threads": threading.active_count()}


class Poller:
    def __init__(
        self, cfg: Config, store: Store, client: Client,
        clock: Callable[[], datetime] | None = None, mono: Callable[[], float] = time.monotonic,
    ):
        self.cfg = cfg
        self.store = store
        self.client = client
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.mono = mono
        # Monotonic time doesn't advance while the Mac sleeps, so a fresh heartbeat
        # with an old last_ok means "just woke up", not "poller is broken".
        self._heartbeat = mono()
        self.consecutive_failures = 0
        self.polling = False
        self._poll_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.started_at = self.clock()

    # ---- polling --------------------------------------------------------

    def poll_once(self) -> str:
        """Poll GitHub once. Returns "ok", "auth", "network" or "poll"."""
        with self._poll_lock:
            self.polling = True
            try:
                result = self._poll()
            finally:
                self.polling = False
                self.store.set_meta("last_attempt_done", iso(self.clock()) + f"#{time.monotonic_ns()}")
                self._heartbeat = self.mono()
            self.consecutive_failures = 0 if result == "ok" else self.consecutive_failures + 1
            return result

    def _poll(self) -> str:
        now = self.clock()
        usage = Usage()
        t0 = time.monotonic()
        self.store.set_meta("last_attempt", iso(now))
        try:
            involved = self.client.involved(self.cfg.repos, self.cfg.include_team_review_requests, usage)
            # PRs we tracked as open but that left the search: fetch once more to learn merged/closed.
            previously_open = set(self.store.open_ids())
            ids = sorted(set(involved) | previously_open)
            since = iso(now - timedelta(days=self.cfg.stale_days))
            details = self.client.details(ids, usage, list_checks_since=since)
            for pr_id in ids:
                pr = details.get(pr_id)
                if pr is None:
                    self.store.drop_pr(pr_id)  # deleted, or no longer visible
                elif pr_id in involved or pr.get("state") in ("MERGED", "CLOSED"):
                    self.store.save_pr(pr, involved.get(pr_id, set()) or self._old_relations(pr_id), now)
                else:
                    self.store.drop_pr(pr_id)  # still open but you're no longer involved
            pruned = self.store.prune(now)
        except AuthError as e:
            self._record_error("auth", str(e), now, usage, t0)
            return "auth"
        except NetworkError as e:
            self._record_error("network", str(e), now, usage, t0)
            return "network"
        except Exception as e:  # noqa: BLE001 — any failure must surface in the UI, not kill the loop
            log.exception("poll failed")
            self._record_error("poll", f"{type(e).__name__}: {e}", now, usage, t0)
            return "poll"
        secs = round(time.monotonic() - t0, 1)
        stats = {
            "at": iso(now), "seconds": secs, "prs": len(involved), "requests": usage.requests,
            "cost": usage.cost, "rate_remaining": usage.remaining, "rate_reset_at": usage.reset_at,
            "pruned": pruned,
        }
        self.store.set_meta("last_ok", stats)
        self.store.set_meta("last_error", None)
        log.info("poll ok prs=%d requests=%d cost=%d remaining=%s secs=%.1f %s",
                 len(involved), usage.requests, usage.cost, usage.remaining, secs, process_stats())
        return "ok"

    def _old_relations(self, pr_id: str) -> set[str]:
        for pr, rel, _ in self.store.all_prs():
            if pr["id"] == pr_id:
                return rel
        return set()

    def _record_error(self, kind: str, msg: str, now: datetime, usage: Usage, t0: float) -> None:
        self.store.set_meta("last_error", {"kind": kind, "message": msg[:500], "at": iso(now),
                                           "seconds": round(time.monotonic() - t0, 1), "requests": usage.requests})
        log.warning("poll error kind=%s %s", kind, msg[:300])

    def run_forever(self) -> None:
        next_due = self.clock()
        while not self._stop.is_set():
            self._heartbeat = self.mono()
            if self._wake.is_set() or self.clock() >= next_due:
                self._wake.clear()
                result = self.poll_once()
                retry = result == "network" and self.consecutive_failures == 1
                next_due = self.clock() + timedelta(seconds=RETRY_SECONDS if retry else self.cfg.poll_interval_seconds)
            # Wait in short slices against the wall clock: Event.wait is monotonic and
            # would otherwise sleep a full interval past a wake from sleep.
            self._wake.wait(LOOP_SLICE_SECONDS)

    def trigger(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # ---- snapshot for the UI -------------------------------------------

    def health(self) -> dict[str, Any]:
        now = self.clock()
        last_ok = self.store.get_meta("last_ok")
        last_error = self.store.get_meta("last_error")
        stale_after = timedelta(seconds=3 * self.cfg.poll_interval_seconds)
        ok_at = datetime.fromisoformat(last_ok["at"].replace("Z", "+00:00")) if last_ok else None
        loop_alive = self.mono() - self._heartbeat < LOOP_ALIVE_SECONDS
        if last_error and (last_error["kind"] == "auth" or self.consecutive_failures >= 2 or not loop_alive):
            state, message = "error", f"{last_error['kind']} error: {last_error['message']}"
        elif last_error:
            state, message = "refreshing", f"retrying after {last_error['kind']} error"
        elif not ok_at:
            state, message = "starting", "first poll in progress"
        elif now - ok_at > stale_after:
            if loop_alive:
                state, message = "refreshing", "catching up (e.g. after sleep)"
            else:
                state, message = "stale", f"no successful poll since {last_ok['at']}"
        else:
            state, message = "ok", ""
        return {"state": state, "message": message, "last_ok": last_ok, "last_error": last_error,
                "polling": self.polling, "last_attempt_done": self.store.get_meta("last_attempt_done"),
                "poll_interval_seconds": self.cfg.poll_interval_seconds, "started_at": iso(self.started_at),
                "process": process_stats()}

    def snapshot(self) -> dict[str, Any]:
        now = self.clock()
        rows = []
        for pr, relations, user in self.store.all_prs():
            row = classify(pr, relations, self.cfg, user, now)
            if row:
                rows.append(row.to_dict())
        rows.sort(key=lambda r: r["last_activity"], reverse=True)  # newest first within a group
        rows.sort(key=lambda r: GROUP_ORDER[r["group"]])  # stable
        counts = {g: sum(1 for r in rows if r["group"] == g) for g in GROUP_ORDER}
        return {"generated_at": iso(now), "health": self.health(), "counts": counts, "rows": rows,
                "repos": list(self.cfg.repos)}
