from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from pr_radar.config import Config
from pr_radar.status import IN_FLIGHT, NEEDS_YOU, PARKED, UserState, classify, norm_login

NOW = datetime(2026, 11, 12, 20, 0, tzinfo=timezone.utc)
ME = "octocat"
CFG = Config(
    github_login=ME,
    repos=("acme/widgets",),
    bot_logins_ignored_for_comments=("review-bot",),
    ignore_checks=("policy/approval",),
)


def ts(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def pr(**kw):
    base = {
        "id": "PR_1", "number": 1, "title": "t", "url": "https://github.com/o/r/pull/1",
        "isDraft": False, "state": "OPEN", "merged": False, "mergedAt": None,
        "createdAt": ts(48), "updatedAt": ts(1),
        "repository": {"nameWithOwner": "acme/widgets"},
        "author": {"__typename": "User", "login": "someone"},
        "assignees": {"nodes": []},
        "reviewRequests": {"nodes": []},
        "mergeQueueEntry": None,
        "commits": {"nodes": [{"commit": {"oid": "HEAD", "committedDate": ts(10)}}]},
        "timelineItems": {"nodes": []},
        "reviews": {"nodes": []},
        "comments": {"nodes": []},
        "_checks": [],
        "_check_counts": {},
    }
    base.update(kw)
    return base


def user(**kw):
    return UserState(first_seen=kw.pop("first_seen", NOW - timedelta(days=5)), **kw)


def run(p, relations=("commenter",), u=None):
    return classify(p, set(relations), CFG, u or user(), NOW)


def added(h, actor="someone"):
    return {"__typename": "AddedToMergeQueueEvent", "createdAt": ts(h), "actor": {"login": actor}}


def removed(h, reason="failed_checks", actor="github-merge-queue"):
    return {"__typename": "RemovedFromMergeQueueEvent", "createdAt": ts(h), "reason": reason, "actor": {"login": actor}}


def check(name, state, kind="check_run"):
    return {"name": name, "kind": kind, "state": state, "url": None}


# --- merge queue -----------------------------------------------------------

def test_dropped_from_queue_shows_reason():
    r = run(pr(timelineItems={"nodes": [added(3), removed(2, "checks_timed_out")]}))
    assert (r.status, r.group, r.detail) == ("dropped_from_queue", NEEDS_YOU, "checks timed out")


def test_removed_then_requeued_is_in_queue_not_dropped():
    r = run(pr(timelineItems={"nodes": [added(5), removed(4, "merge_conflict"), added(1)]},
               mergeQueueEntry={"position": 3, "state": "QUEUED"}))
    assert (r.status, r.group, r.detail) == ("in_merge_queue", IN_FLIGHT, "position 3")


def test_removal_by_me_is_ignored():
    r = run(pr(timelineItems={"nodes": [added(3), removed(2, "manually_removed", actor=ME)]}))
    assert r.status == "waiting"


def test_new_commit_after_drop_clears_it():
    r = run(pr(timelineItems={"nodes": [added(30), removed(20)]},
               commits={"nodes": [{"commit": {"oid": "NEW", "committedDate": ts(5)}}]}))
    assert r.status == "waiting"


def test_dropped_beats_ci_failed():
    r = run(pr(timelineItems={"nodes": [removed(2)]}, _checks=[check("unit", "FAILURE")]))
    assert r.status == "dropped_from_queue"


# --- CI --------------------------------------------------------------------

def test_ci_failed_names_checks():
    r = run(pr(_checks=[check("unit", "FAILURE"), check("lint", "SUCCESS")]))
    assert (r.status, r.detail) == ("ci_failed", "unit")
    assert r.failing_checks == [{"name": "unit", "url": None}]


def test_cancelled_is_not_failure():
    assert run(pr(_checks=[check("unit", "CANCELLED")])).status == "waiting"


def test_ignored_check_never_fails_or_blocks():
    p = pr(_checks=[check("policy/approval", "PENDING", "status")])
    assert run(p).detail == ""  # not "CI running"


def test_rerun_that_passed_is_not_failure():
    assert run(pr(_checks=[check("unit", "FAILURE"), check("unit", "SUCCESS")])).status == "waiting"


def test_counts_only_failure_for_unlisted_checks():
    r = run(pr(_check_counts={"FAILURE": 2, "SUCCESS": 90}))
    assert (r.status, r.detail) == ("ci_failed", "2 failing checks")


def test_counts_only_running():
    assert run(pr(_check_counts={"IN_PROGRESS": 1})).detail == "CI running"


# --- Assigned-ready / reviews / comments -----------------------------------

def assigned_pr(author="helper-bot", **kw):
    return pr(author={"__typename": "Bot", "login": author},
              assignees={"nodes": [{"login": ME}]}, **kw)


def test_assigned_ready_from_any_author_by_default():
    assert run(assigned_pr(), ("assignee",)).status == "assigned_ready"
    assert run(assigned_pr(author="a-human"), ("assignee",)).status == "assigned_ready"


def test_assigned_ready_can_be_limited_to_authors():
    cfg = replace(CFG, assigned_ready_authors=("helper-bot",))
    assert classify(assigned_pr(), {"assignee"}, cfg, user(), NOW).status == "assigned_ready"
    assert classify(assigned_pr(author="a-human"), {"assignee"}, cfg, user(), NOW).status == "waiting"


def test_own_pr_assigned_to_self_is_not_ready_for_your_review():
    assert run(assigned_pr(author=ME), ("author", "assignee")).status == "waiting"


def test_not_ready_while_checks_run():
    assert run(assigned_pr(_checks=[check("unit", "IN_PROGRESS")]), ("assignee",)).status == "waiting"


def test_not_ready_while_draft():
    assert run(assigned_pr(isDraft=True), ("assignee",)).status == "waiting"


def test_not_ready_once_head_reviewed():
    rv = {"author": {"__typename": "User", "login": ME}, "state": "APPROVED",
          "submittedAt": ts(1), "commit": {"oid": "HEAD"}}
    assert run(assigned_pr(reviews={"nodes": [rv]}), ("assignee",)).status == "waiting"


def test_ready_again_after_push_post_approval():
    rv = {"author": {"__typename": "User", "login": ME}, "state": "APPROVED",
          "submittedAt": ts(12), "commit": {"oid": "OLD"}}
    assert run(assigned_pr(reviews={"nodes": [rv]}), ("assignee",)).status == "assigned_ready"


def test_review_requested_personal_only():
    p = pr(reviewRequests={"nodes": [{"requestedReviewer": {"__typename": "User", "login": ME}}]})
    assert run(p, ("review_requested",)).status == "review_requested"
    team = pr(reviewRequests={"nodes": [{"requestedReviewer": {"__typename": "Team", "slug": "aa-product"}}]})
    assert run(team).status == "waiting"


def c(login, h, typ="User"):
    return {"author": {"__typename": typ, "login": login}, "createdAt": ts(h)}


def test_new_comments_from_humans_only():
    p = pr(comments={"nodes": [c("anton", 2), c(ME, 1), c("helper-bot", 1, "Bot"), c("review-bot", 1)]})
    r = run(p, u=user(seen_at=NOW - timedelta(hours=3)))
    assert (r.status, r.new_comment_count) == ("new_comments", 1)


def test_seen_clears_new_comments():
    p = pr(comments={"nodes": [c("anton", 2)]})
    assert run(p, u=user(seen_at=NOW - timedelta(hours=1))).status == "waiting"


def test_comments_before_first_seen_dont_count():
    p = pr(comments={"nodes": [c("anton", 30)]})
    assert run(p, u=user(first_seen=NOW - timedelta(hours=2))).status == "waiting"


# --- parked / merged / closed ----------------------------------------------

def test_stale_pr_is_parked_but_keeps_status():
    r = run(pr(updatedAt=ts(24 * 40), timelineItems={"nodes": [removed(24 * 41)]},
               commits={"nodes": [{"commit": {"oid": "H", "committedDate": ts(24 * 45)}}]}))
    assert (r.group, r.status) == (PARKED, "dropped_from_queue")
    assert r.parked_reason.startswith("no activity for 40d")


def test_hidden_pr_parked_until_new_activity():
    hidden = NOW - timedelta(hours=5)
    assert run(pr(updatedAt=ts(6)), u=user(hidden_at=hidden)).group == PARKED
    assert run(pr(updatedAt=ts(1)), u=user(hidden_at=hidden)).group != PARKED


@pytest.mark.parametrize("hours,listed", [(2, True), (30, False)])
def test_merged_visible_for_24h(hours, listed):
    r = run(pr(merged=True, state="MERGED", mergedAt=ts(hours)))
    assert (r is not None) == listed


def test_closed_unmerged_not_listed():
    assert run(pr(state="CLOSED")) is None


def test_norm_login_variants():
    assert norm_login("helper-bot[bot]") == norm_login("app/helper-bot") == "helper-bot"
