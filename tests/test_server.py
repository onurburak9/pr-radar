import json
import urllib.error
import urllib.request

import pytest

from pr_radar.server import serve

from .test_poller import make
from .test_status import pr


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


@pytest.fixture
def app(tmp_path):
    poller, client = make(tmp_path)
    client.involved_map = {"PR_A": {"author"}, "PR_B": {"assignee"}}
    client.prs = {
        "PR_A": pr(id="PR_A", number=10, title="has | pipe", _checks=[{"name": "unit", "kind": "check_run", "state": "FAILURE", "url": None}]),
        "PR_B": pr(id="PR_B", number=11),
    }
    poller.poll_once()
    httpd = serve(poller, 0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield poller, base
    httpd.shutdown()


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read().decode(), r.headers


def post(url, header=True):
    req = urllib.request.Request(url, method="POST", headers={"X-PR-Radar": "1"} if header else {})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status


def test_index_and_state(app):
    _, base = app
    status, body, _ = get(base + "/")
    assert status == 200 and "PR radar" in body
    state = json.loads(get(base + "/api/state")[1])
    assert state["counts"]["needs_you"] == 1
    assert {r["number"] for r in state["rows"]} == {10, 11}


def test_swiftbar_menu(app):
    _, base = app
    _, body, _ = get(base + "/swiftbar")
    lines = body.splitlines()
    assert lines[0] == "PR ● 1"
    assert lines[1] == "---"
    ci_line = next(l for l in lines if "#10" in l)
    assert "has ¦ pipe" in ci_line  # a '|' in a title would break SwiftBar's parser
    assert ci_line.endswith(f"href=http://localhost:{base.rsplit(':', 1)[1]}/open/acme/widgets/10")


def test_swiftbar_in_flight_is_a_submenu(app):
    _, base = app
    lines = get(base + "/swiftbar")[1].splitlines()
    i = next(n for n, l in enumerate(lines) if l.startswith("In flight · 1 | href="))
    assert lines[i + 1].startswith("--⏳ widgets#11")


def test_swiftbar_shows_warning_on_error(app):
    poller, base = app
    poller.store.set_meta("last_error", {"kind": "auth", "message": "bad token", "at": "x", "seconds": 0, "requests": 0})
    body = get(base + "/swiftbar")[1]
    assert body.startswith("PR ⚠")
    assert "auth error: bad token" in body


def test_open_marks_seen_and_redirects_to_github(app):
    poller, base = app
    opener = urllib.request.build_opener(NoRedirect)
    with pytest.raises(urllib.error.HTTPError) as e:
        opener.open(base + "/open/acme/widgets/10", timeout=5)
    assert e.value.code == 302
    assert e.value.headers["Location"] == "https://github.com/acme/widgets/pull/10"
    assert any(u.seen_at for p, _, u in poller.store.all_prs() if p["id"] == "PR_A")


def test_post_requires_header(app):
    _, base = app
    with pytest.raises(urllib.error.HTTPError) as e:
        post(base + "/api/refresh", header=False)
    assert e.value.code == 403


def test_hide_unhide(app):
    poller, base = app
    assert post(base + "/api/hide/PR_B") == 200
    rows = {r["number"]: r for r in poller.snapshot()["rows"]}
    assert rows[11]["group"] == "parked"
    post(base + "/api/unhide/PR_B")
    assert {r["number"]: r for r in poller.snapshot()["rows"]}[11]["group"] != "parked"
