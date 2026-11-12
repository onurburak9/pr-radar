"""Localhost HTTP server: JSON API, the page, the SwiftBar menu, and /open redirects."""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import quote

from .poller import Poller
from .status import NEEDS_YOU, IN_FLIGHT, PARKED

log = logging.getLogger("pr_radar")

OPEN_RE = re.compile(r"^/open/([\w.-]+)/([\w.-]+)/(\d+)$")
MARK_RE = re.compile(r"^/api/(hide|unhide)/([\w-]+)$")
ICONS = {
    "dropped_from_queue": "⛔", "ci_failed": "🔴", "assigned_ready": "📋", "review_requested": "👀",
    "new_comments": "💬", "in_merge_queue": "🚂", "waiting": "⏳", "merged": "✅",
}
MENU_ROWS_PER_GROUP = 15


def open_path(row: dict[str, Any]) -> str:
    owner, name = row["repo"].split("/")
    return f"/open/{owner}/{name}/{row['number']}"


def swiftbar_menu(snap: dict[str, Any], base: str) -> str:
    """SwiftBar plugin output: title line, '---', then menu items."""
    health = snap["health"]
    needs = [r for r in snap["rows"] if r["group"] == NEEDS_YOU]
    flight = [r for r in snap["rows"] if r["group"] == IN_FLIGHT]
    lines: list[str] = []
    if health["state"] in ("error", "stale"):
        lines.append(f"PR ⚠ {len(needs)} | color=#d92d20")
    else:
        lines.append(f"PR ● {len(needs)}" if needs else "PR ○")
    lines.append("---")
    if health["state"] in ("error", "stale"):
        lines.append(f"⚠ {clean(health['message'] or health['state'])} | color=#d92d20")
        lines.append("---")
    elif health["state"] != "ok":
        lines.append(f"⟳ {clean(health['message'] or health['state'])} | color=#86868b")
        lines.append("---")

    def item(r: dict[str, Any], prefix: str = "") -> str:
        repo = r["repo"].split("/")[1]
        detail = r["detail"]
        if len(detail) > 32:
            detail = detail[:31] + "…"
        detail = f" ({detail})" if detail else ""
        text = clean(f"{ICONS.get(r['status'], '•')} {repo}#{r['number']}  {r['label']}{detail} · {r['title']}")
        return f"{prefix}{text[:95]} | href={base}{open_path(r)}"

    # Needs you: top level, it's what the menu is for.
    lines.append(f"Needs you · {len(needs)} | size=11 color=#86868b")
    lines += [item(r) for r in needs[:MENU_ROWS_PER_GROUP]]
    if len(needs) > MENU_ROWS_PER_GROUP:
        lines.append(f"…{len(needs) - MENU_ROWS_PER_GROUP} more | href={base}/")
    if not needs:
        lines.append("Nothing needs you 🎉 | color=#86868b")
    lines.append("---")
    # In flight: a submenu, so the dropdown stays short.
    # An href keeps the parent enabled (macOS greys out action-less items) and opens the page section on click.
    lines.append(f"In flight · {len(flight)} | href={base}/#in_flight")
    lines += [item(r, "--") for r in flight[:MENU_ROWS_PER_GROUP * 2]]
    if len(flight) > MENU_ROWS_PER_GROUP * 2:
        lines.append(f"--…{len(flight) - MENU_ROWS_PER_GROUP * 2} more | href={base}/")
    lines.append(f"Parked ({snap['counts'].get(PARKED, 0)})… | href={base}/#parked")
    lines.append(f"Open full view ↗ | href={base}/")
    lines.append(
        f"Refresh now | bash=/usr/bin/curl param1=-sfX param2=POST param3=-H param4=X-PR-Radar:1 "
        f"param5={base}/api/refresh terminal=false refresh=true"
    )
    last_ok = health.get("last_ok")
    if last_ok:
        lines.append(f"Updated {ago(last_ok['at'])} · {last_ok['cost']} pts · {last_ok['rate_remaining']} left | size=11 color=#86868b")
    return "\n".join(lines) + "\n"


def clean(s: str) -> str:
    return s.replace("|", "¦").replace("\n", " ")


def ago(iso_ts: str) -> str:
    dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{secs // 60} min ago"
    return f"{secs // 3600} h ago"


def make_handler(poller: Poller):
    index_html = resources.files("pr_radar").joinpath("static/index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        server_version = "pr-radar"

        def log_message(self, fmt: str, *args: Any) -> None:  # route to logging, quietly
            log.debug("http " + fmt, *args)

        def _send(self, code: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: Any, code: int = 200) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _base(self) -> str:
            return f"http://localhost:{self.server.server_address[1]}"

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/":
                return self._send(200, index_html, "text/html; charset=utf-8")
            if path == "/api/state":
                return self._json(poller.snapshot())
            if path == "/api/health":
                return self._json(poller.health())
            if path == "/swiftbar":
                return self._send(200, swiftbar_menu(poller.snapshot(), self._base()).encode(), "text/plain; charset=utf-8")
            if m := OPEN_RE.match(path):
                owner, name, number = m.group(1), m.group(2), int(m.group(3))
                repo = f"{owner}/{name}"
                pr_id = poller.store.find_id(repo, number)
                if pr_id:
                    poller.store.mark_seen(pr_id, poller.clock())
                # Always redirect to a github.com URL we build ourselves — never an arbitrary target.
                target = f"https://github.com/{quote(repo)}/pull/{number}"
                return self._send(302, b"", "text/plain", {"Location": target})
            return self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            # A custom header forces a CORS preflight, so other web pages can't hit these.
            if self.headers.get("X-PR-Radar") != "1":
                return self._send(403, b"missing X-PR-Radar header", "text/plain")
            path = self.path.split("?", 1)[0]
            if path == "/api/refresh":
                poller.trigger()
                return self._json({"ok": True})
            if m := MARK_RE.match(path):
                action, pr_id = m.groups()
                if action == "hide":
                    poller.store.hide(pr_id, poller.clock())
                else:
                    poller.store.unhide(pr_id)
                return self._json({"ok": True})
            return self._send(404, b"not found", "text/plain")

    return Handler


def serve(poller: Poller, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(poller))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()
    return httpd
