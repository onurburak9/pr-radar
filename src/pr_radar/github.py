"""Thin GitHub GraphQL client: who's-involved searches and per-PR detail fetches."""

from __future__ import annotations

import json
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

GRAPHQL_URL = "https://api.github.com/graphql"
DETAIL_BATCH = 20
CHECKS_PAGE = 100

# relation name -> search qualifier template
RELATIONS: dict[str, str] = {
    "author": "author:{login}",
    "assignee": "assignee:{login}",
    "reviewer": "reviewed-by:{login}",
    "commenter": "commenter:{login}",
    "review_requested": "user-review-requested:{login}",
}
TEAM_REVIEW_RELATION = ("team_review_requested", "review-requested:{login}")


class GitHubError(RuntimeError):
    pass


class AuthError(GitHubError):
    pass


class NetworkError(GitHubError):
    """Transient: no network, DNS, timeout, or GitHub 5xx."""


def gh_token(login: str) -> str:
    gh = shutil.which("gh") or "/opt/homebrew/bin/gh"
    try:
        out = subprocess.run(
            [gh, "auth", "token", "--user", login],
            capture_output=True, text=True, timeout=15, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
        raise AuthError(f"could not get gh token for {login}: {e}") from e
    token = out.stdout.strip()
    if not token:
        raise AuthError(f"gh returned an empty token for {login}")
    return token


@dataclass
class Usage:
    """GraphQL cost accounting for one poll."""

    requests: int = 0
    cost: int = 0
    remaining: int | None = None
    reset_at: str | None = None

    def add(self, rate: dict[str, Any] | None) -> None:
        self.requests += 1
        if rate:
            self.cost += rate.get("cost", 0)
            self.remaining = rate.get("remaining")
            self.reset_at = rate.get("resetAt")


@dataclass
class GitHubClient:
    login: str
    _token: str | None = field(default=None, repr=False)
    # (pr_id, head oid, check-count signature) -> listed check runs. Completed
    # runs on a commit don't change unless the counts do, so re-polls are free.
    _check_cache: dict[tuple[str, str, tuple], list[dict[str, Any]]] = field(default_factory=dict, repr=False)

    def _get_token(self) -> str:
        if self._token is None:
            self._token = gh_token(self.login)
        return self._token

    def graphql(self, query: str, variables: dict[str, Any], usage: Usage) -> dict[str, Any]:
        query = query.replace("RATE_LIMIT", "rateLimit { cost remaining resetAt }")
        for attempt in (1, 2):
            req = urllib.request.Request(
                GRAPHQL_URL,
                data=json.dumps({"query": query, "variables": variables}).encode(),
                headers={
                    "Authorization": f"bearer {self._get_token()}",
                    "Content-Type": "application/json",
                    "User-Agent": "pr-radar",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = json.loads(resp.read())
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 1:
                    self._token = None  # token may have rotated; re-read from gh once
                    continue
                if e.code == 401:
                    raise AuthError("GitHub rejected the token (401)") from e
                if e.code >= 500:
                    raise NetworkError(f"GitHub HTTP {e.code}") from e
                raise GitHubError(f"GitHub HTTP {e.code}: {e.read()[:300]!r}") from e
            except urllib.error.URLError as e:
                raise NetworkError(f"GitHub unreachable: {e.reason}") from e
            except (TimeoutError, ConnectionError) as e:
                raise NetworkError(f"GitHub unreachable: {e}") from e
            if body.get("errors"):
                raise GitHubError(f"GraphQL errors: {body['errors'][:2]}")
            data = body["data"]
            usage.add(data.get("rateLimit"))
            return data
        raise AssertionError("unreachable")

    # ---- searches -------------------------------------------------------

    def search_ids(self, repos: tuple[str, ...], qualifier: str, usage: Usage) -> list[str]:
        scope = " ".join(f"repo:{r}" for r in repos)
        q = f"is:pr is:open archived:false {scope} {qualifier}"
        ids: list[str] = []
        cursor = None
        while True:
            data = self.graphql(SEARCH_QUERY, {"q": q, "after": cursor}, usage)
            s = data["search"]
            ids += [n["id"] for n in s["nodes"] if n and n.get("id")]
            if not s["pageInfo"]["hasNextPage"]:
                return ids
            cursor = s["pageInfo"]["endCursor"]

    def involved(self, repos: tuple[str, ...], include_team: bool, usage: Usage) -> dict[str, set[str]]:
        """PR node id -> set of relation names."""
        relations = dict(RELATIONS)
        if include_team:
            relations[TEAM_REVIEW_RELATION[0]] = TEAM_REVIEW_RELATION[1]
        out: dict[str, set[str]] = {}
        for name, tmpl in relations.items():
            for pr_id in self.search_ids(repos, tmpl.format(login=self.login), usage):
                out.setdefault(pr_id, set()).add(name)
        return out

    # ---- details --------------------------------------------------------

    def details(
        self, ids: list[str], usage: Usage, list_checks_since: str | None = None
    ) -> dict[str, dict[str, Any]]:
        """Fetch PR details. Check-run names are only listed for PRs updated at or
        after ``list_checks_since`` (ISO timestamp); older PRs get counts only."""
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(ids), DETAIL_BATCH):
            data = self.graphql(DETAIL_QUERY, {"ids": ids[i : i + DETAIL_BATCH]}, usage)
            for node in data["nodes"]:
                if node and node.get("id"):
                    out[node["id"]] = node
        live_keys = set()
        for node in out.values():
            want_names = list_checks_since is None or node.get("updatedAt", "") >= list_checks_since
            key = self._complete_checks(node, usage, want_names)
            if key:
                live_keys.add(key)
        self._check_cache = {k: v for k, v in self._check_cache.items() if k in live_keys}
        return out

    def _complete_checks(self, node: dict[str, Any], usage: Usage, want_names: bool) -> tuple | None:
        """Normalise the head commit's checks into node["_checks"].

        Listing ~140 check runs per PR is the expensive part of a poll, so check
        runs are only listed when the cheap per-state counts say one is red or
        still running. Legacy status contexts are few and always included.
        """
        node["_checks"] = []
        node["_check_counts"] = {}
        commits = node.get("commits", {}).get("nodes") or []
        if not commits:
            return None
        commit = commits[0]["commit"]
        for sc in (commit.get("status") or {}).get("contexts") or []:
            node["_checks"].append(
                {"name": sc["context"], "kind": "status", "state": sc["state"], "url": sc.get("targetUrl")}
            )
        rollup = commit.get("statusCheckRollup")
        if not rollup:
            return None
        counts = {c["state"]: c["count"] for c in rollup["contexts"].get("checkRunCountsByState") or [] if c["count"]}
        node["_check_counts"] = counts
        if not want_names or not any(counts.get(s, 0) for s in INTERESTING_CHECK_RUN_STATES):
            return None
        key = (node["id"], commit["oid"], tuple(sorted(counts.items())))
        cached = self._check_cache.get(key)
        if cached is not None:
            node["_checks"].extend(cached)
            return key
        runs: list[dict[str, Any]] = []
        cursor = None
        while True:
            data = self.graphql(CHECKS_PAGE_QUERY, {"id": node["id"], "after": cursor}, usage)
            page = data["node"]["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]
            for n in page["nodes"]:
                if n.get("__typename") == "CheckRun":
                    state = n["conclusion"] if n["status"] == "COMPLETED" else n["status"]
                    runs.append({"name": n["name"], "kind": "check_run", "state": state, "url": n.get("detailsUrl")})
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
        self._check_cache[key] = runs
        node["_checks"].extend(runs)
        return key


# CheckRunState values that mean "red" or "not finished" — worth listing names for.
INTERESTING_CHECK_RUN_STATES = (
    "FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED",
    "IN_PROGRESS", "QUEUED", "PENDING", "WAITING", "REQUESTED",
)


SEARCH_QUERY = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest { id } }
  }
  RATE_LIMIT
}
"""

CHECK_FIELDS = """
pageInfo { hasNextPage endCursor }
nodes {
  __typename
  ... on CheckRun { name status conclusion detailsUrl }
  ... on StatusContext { context state targetUrl }
}
"""

DETAIL_QUERY = f"""
query($ids: [ID!]!) {{
  nodes(ids: $ids) {{
    ... on PullRequest {{
      id number title url isDraft state merged mergedAt closedAt createdAt updatedAt
      repository {{ nameWithOwner }}
      author {{ __typename login }}
      assignees(first: 10) {{ nodes {{ login }} }}
      reviewRequests(first: 20) {{
        nodes {{ requestedReviewer {{ __typename ... on User {{ login }} ... on Team {{ slug }} }} }}
      }}
      reviewDecision
      mergeQueueEntry {{ position state enqueuedAt }}
      commits(last: 1) {{
        nodes {{ commit {{ oid committedDate
          status {{ contexts {{ context state targetUrl }} }}
          statusCheckRollup {{ contexts(first: 1) {{
            checkRunCountsByState {{ state count }}
          }} }}
        }} }}
      }}
      timelineItems(last: 10, itemTypes: [ADDED_TO_MERGE_QUEUE_EVENT, REMOVED_FROM_MERGE_QUEUE_EVENT]) {{
        nodes {{
          __typename
          ... on AddedToMergeQueueEvent {{ createdAt actor {{ login }} }}
          ... on RemovedFromMergeQueueEvent {{ createdAt reason actor {{ login }} }}
        }}
      }}
      reviews(last: 30) {{
        nodes {{ author {{ __typename login }} state submittedAt commit {{ oid }} }}
      }}
      comments(last: 30) {{ nodes {{ author {{ __typename login }} createdAt }} }}
    }}
  }}
  RATE_LIMIT
}}
"""

CHECKS_PAGE_QUERY = f"""
query($id: ID!, $after: String) {{
  node(id: $id) {{
    ... on PullRequest {{
      commits(last: 1) {{ nodes {{ commit {{
        statusCheckRollup {{ contexts(first: {CHECKS_PAGE}, after: $after) {{ {CHECK_FIELDS} }} }}
      }} }} }}
    }}
  }}
  RATE_LIMIT
}}
"""
