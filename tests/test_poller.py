import threading
import time
from datetime import timedelta

from pr_radar import poller as poller_mod
from pr_radar.config import Config
from pr_radar.github import AuthError, NetworkError
from pr_radar.poller import Poller
from pr_radar.store import Store

from .test_status import ME, NOW, pr

CFG = Config(github_login=ME, repos=("acme/widgets",), poll_interval_seconds=60)


class FakeClient:
    def __init__(self):
        self.involved_map = {}
        self.prs = {}
        self.fail = None

    def involved(self, repos, include_team, usage):
        if self.fail:
            raise self.fail
        usage.requests += 1
        return {k: set(v) for k, v in self.involved_map.items()}

    def details(self, ids, usage, list_checks_since=None):
        usage.requests += 1
        return {i: self.prs[i] for i in ids if i in self.prs}


def make(tmp_path, clock=lambda: NOW, mono=time.monotonic):
    client = FakeClient()
    return Poller(CFG, Store(tmp_path / "db.sqlite"), client, clock=clock, mono=mono), client


def test_poll_lists_involved_prs(tmp_path):
    p, c = make(tmp_path)
    c.involved_map = {"A": {"author"}}
    c.prs = {"A": pr(id="A", number=10)}
    p.poll_once()
    snap = p.snapshot()
    assert [r["number"] for r in snap["rows"]] == [10]
    assert snap["health"]["state"] == "ok"


def test_pr_leaving_search_as_merged_shows_merged(tmp_path):
    p, c = make(tmp_path)
    c.involved_map = {"A": {"author"}}
    c.prs = {"A": pr(id="A")}
    p.poll_once()
    c.involved_map = {}
    c.prs = {"A": pr(id="A", state="MERGED", merged=True, mergedAt=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"))}
    p.poll_once()
    rows = p.snapshot()["rows"]
    assert [(r["status"], r["group"], r["relations"]) for r in rows] == [("merged", "done", ["author"])]


def test_pr_still_open_but_no_longer_involved_is_dropped(tmp_path):
    p, c = make(tmp_path)
    c.involved_map = {"A": {"commenter"}}
    c.prs = {"A": pr(id="A")}
    p.poll_once()
    c.involved_map = {}
    p.poll_once()
    assert p.snapshot()["rows"] == []


def test_auth_error_surfaces_and_keeps_old_rows(tmp_path):
    p, c = make(tmp_path)
    c.involved_map = {"A": {"author"}}
    c.prs = {"A": pr(id="A")}
    p.poll_once()
    c.fail = AuthError("bad token")
    p.poll_once()
    snap = p.snapshot()
    assert snap["health"]["state"] == "error"
    assert "auth" in snap["health"]["message"]
    assert len(snap["rows"]) == 1  # last known data still shown, flagged by health


def test_wake_from_sleep_is_refreshing_not_red(tmp_path):
    now, mono = [NOW], [0.0]
    p, _ = make(tmp_path, clock=lambda: now[0], mono=lambda: mono[0])
    p.poll_once()
    now[0] = NOW + timedelta(hours=8)  # wall clock jumped; monotonic didn't (Mac slept)
    assert p.health()["state"] == "refreshing"


def test_stuck_poll_loop_goes_stale(tmp_path):
    now, mono = [NOW], [0.0]
    p, _ = make(tmp_path, clock=lambda: now[0], mono=lambda: mono[0])
    p.poll_once()
    now[0] = NOW + timedelta(minutes=10)
    mono[0] = 600.0  # awake the whole time, yet no poll
    assert p.health()["state"] == "stale"


def test_first_network_error_is_soft_second_is_red(tmp_path):
    p, c = make(tmp_path)
    p.poll_once()
    c.fail = NetworkError("GitHub unreachable")
    assert p.poll_once() == "network"
    assert p.health()["state"] == "refreshing"
    p.poll_once()
    assert p.health()["state"] == "error"


def run_loop(p, monkeypatch):
    monkeypatch.setattr(poller_mod, "LOOP_SLICE_SECONDS", 0.01)
    t = threading.Thread(target=p.run_forever, daemon=True)
    t.start()
    return t


def wait_for(pred, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_loop_polls_when_wall_clock_passes_interval(tmp_path, monkeypatch):
    now = [NOW]
    p, c = make(tmp_path, clock=lambda: now[0])
    calls = []
    orig = c.involved
    c.involved = lambda *a: (calls.append(1), orig(*a))[1]
    run_loop(p, monkeypatch)
    assert wait_for(lambda: len(calls) == 1)
    time.sleep(0.1)
    assert len(calls) == 1  # not due yet
    now[0] = NOW + timedelta(seconds=CFG.poll_interval_seconds + 1)  # e.g. woke from sleep
    assert wait_for(lambda: len(calls) == 2)
    p.stop()


def test_loop_retries_soon_after_first_network_error(tmp_path, monkeypatch):
    now = [NOW]
    p, c = make(tmp_path, clock=lambda: now[0])
    c.fail = NetworkError("down")
    run_loop(p, monkeypatch)
    assert wait_for(lambda: p.consecutive_failures == 1)
    c.fail = None
    now[0] = NOW + timedelta(seconds=poller_mod.RETRY_SECONDS + 1)  # well before the 60s interval
    assert wait_for(lambda: p.consecutive_failures == 0)
    p.stop()


def test_trigger_polls_immediately(tmp_path, monkeypatch):
    p, c = make(tmp_path)
    run_loop(p, monkeypatch)
    assert wait_for(lambda: p.store.get_meta("last_ok") is not None)
    first = p.store.get_meta("last_attempt_done")
    p.trigger()
    assert wait_for(lambda: p.store.get_meta("last_attempt_done") != first)
    p.stop()


def test_health_starting_before_first_poll(tmp_path):
    p, _ = make(tmp_path)
    assert p.health()["state"] == "starting"


def test_hide_and_seen_marks(tmp_path):
    p, c = make(tmp_path)
    c.involved_map = {"A": {"author"}}
    c.prs = {"A": pr(id="A", updatedAt=(NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))}
    p.poll_once()
    p.store.hide("A", NOW)
    assert p.snapshot()["rows"][0]["group"] == "parked"
    p.store.unhide("A")
    assert p.snapshot()["rows"][0]["group"] != "parked"
    assert p.store.find_id("acme/widgets", 1) == "A"


def test_resource_stats_present(tmp_path):
    p, _ = make(tmp_path)
    proc = p.health()["process"]
    assert proc["rss_mb"] and proc["rss_mb"] > 0
