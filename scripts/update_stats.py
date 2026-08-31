#!/usr/bin/env python3
"""Refresh the GitHub and LeetCode numbers embedded in index.html.

The script intentionally has no LinkedIn selector or API call. GitHub Actions
runs it daily, then commits index.html only when a displayed value changes.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
INDEX_FILE = ROOT / "index.html"
GITHUB_USER = "ayuxharma"
LEETCODE_USER = "ayuxharma"
USER_AGENT = "ayuxharma-portfolio-stats/1.0"


def request(url: str, *, payload: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None, attempts: int = 3) -> bytes:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)

    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            req = Request(url, data=body, headers=request_headers, method="POST" if body else "GET")
            with urlopen(req, timeout=30) as response:
                return response.read()
        except (HTTPError, URLError, TimeoutError) as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Request failed for {url}: {last_error}")


def request_json(url: str, **kwargs: Any) -> dict[str, Any]:
    return json.loads(request(url, **kwargs).decode("utf-8"))


def longest_contribution_streak(days: list[dict[str, Any]]) -> int:
    longest = current = 0
    for day in sorted(days, key=lambda item: item["date"]):
        if int(day["contributionCount"]) > 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def github_stats() -> dict[str, int]:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is required for contribution data")

    profile = request_json(
        f"https://api.github.com/users/{GITHUB_USER}",
        headers={"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"},
    )

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=365)
    query = """
      query PortfolioStats($login: String!, $from: DateTime!, $to: DateTime!) {
        user(login: $login) {
          contributionsCollection(from: $from, to: $to) {
            contributionCalendar {
              totalContributions
              weeks { contributionDays { contributionCount date } }
            }
          }
        }
      }
    """
    result = request_json(
        "https://api.github.com/graphql",
        payload={
            "query": query,
            "variables": {
                "login": GITHUB_USER,
                "from": start.isoformat().replace("+00:00", "Z"),
                "to": now.isoformat().replace("+00:00", "Z"),
            },
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    if result.get("errors"):
        raise RuntimeError(f"GitHub GraphQL error: {result['errors']}")

    calendar = result["data"]["user"]["contributionsCollection"]["contributionCalendar"]
    days = [day for week in calendar["weeks"] for day in week["contributionDays"]]
    return {
        "github-contributions": int(calendar["totalContributions"]),
        "github-repositories": int(profile["public_repos"]),
        "github-streak": longest_contribution_streak(days),
    }


def github_profile_views() -> int:
    """Read the public profile-counter badge; callers preserve the old value on failure."""
    svg = request(
        f"https://komarev.com/ghpvc/?username={GITHUB_USER}&style=flat&label=Profile+views",
        headers={"Accept": "image/svg+xml"},
    ).decode("utf-8", errors="replace")
    values = re.findall(r"<text[^>]*>([\d,]+)</text>", svg)
    if not values:
        raise RuntimeError("Profile-view counter returned no numeric value")
    return int(values[-1].replace(",", ""))


def leetcode_stats() -> tuple[dict[str, str | int], dict[str, int]]:
    query = """
      query PortfolioStats($username: String!) {
        matchedUser(username: $username) {
          profile { ranking }
          submitStatsGlobal {
            acSubmissionNum { difficulty count submissions }
          }
          badges { id }
          userCalendar { streak }
        }
      }
    """
    result = request_json(
        "https://leetcode.com/graphql",
        payload={"query": query, "variables": {"username": LEETCODE_USER}},
        headers={"Referer": f"https://leetcode.com/u/{LEETCODE_USER}/"},
    )
    if result.get("errors"):
        raise RuntimeError(f"LeetCode GraphQL error: {result['errors']}")
    user = result.get("data", {}).get("matchedUser")
    if not user:
        raise RuntimeError("LeetCode user was not found")

    submissions = {
        row["difficulty"].lower(): row
        for row in user["submitStatsGlobal"]["acSubmissionNum"]
    }
    all_stats = submissions["all"]
    acceptance = (
        (int(all_stats["count"]) / int(all_stats["submissions"])) * 100
        if int(all_stats["submissions"]) else 0
    )
    counts = {difficulty: int(submissions[difficulty]["count"]) for difficulty in ("easy", "medium", "hard")}
    values: dict[str, str | int] = {
        "leetcode-rank": f"Rank {int(user['profile']['ranking']):,} →",
        "leetcode-solved": int(all_stats["count"]),
        "leetcode-acceptance": f"{acceptance:.2f}%",
        "leetcode-streak": int(user["userCalendar"]["streak"]),
        "leetcode-badges": len(user.get("badges") or []),
        **{f"leetcode-{difficulty}": count for difficulty, count in counts.items()},
    }
    return values, counts


def replace_stat(html: str, key: str, value: str | int) -> str:
    pattern = re.compile(
        rf'(<[^>]+\bdata-stat="{re.escape(key)}"[^>]*>)(.*?)(</[^>]+>)',
        re.DOTALL,
    )
    updated, count = pattern.subn(rf"\g<1>{value}\g<3>", html, count=1)
    if count != 1:
        raise RuntimeError(f"Expected exactly one data-stat marker for {key}, found {count}")
    return updated


def replace_bar_width(html: str, key: str, width: float) -> str:
    pattern = re.compile(rf'<[^>]+\bdata-stat-bar="{re.escape(key)}"[^>]*>')
    match = pattern.search(html)
    if not match:
        raise RuntimeError(f"Missing data-stat-bar marker for {key}")
    opening_tag = re.sub(r"width:\s*[\d.]+%", f"width:{width:.1f}%", match.group(0))
    return html[:match.start()] + opening_tag + html[match.end():]


def main() -> int:
    html = INDEX_FILE.read_text(encoding="utf-8")
    values: dict[str, str | int] = {}
    failures: list[str] = []

    try:
        values.update(github_stats())
    except Exception as error:  # Keep the last published values if an API is temporarily unavailable.
        failures.append(f"GitHub: {error}")

    try:
        values["github-profile-views"] = github_profile_views()
    except Exception as error:
        failures.append(f"GitHub profile views: {error}")

    leetcode_counts: dict[str, int] = {}
    try:
        leetcode_values, leetcode_counts = leetcode_stats()
        values.update(leetcode_values)
    except Exception as error:
        failures.append(f"LeetCode: {error}")

    if not values:
        raise RuntimeError("No stats provider succeeded; index.html was left unchanged")

    for key, raw_value in values.items():
        display_value = f"{raw_value:,}" if isinstance(raw_value, int) else raw_value
        html = replace_stat(html, key, display_value)

    if leetcode_counts:
        maximum = max(leetcode_counts.values()) or 1
        for difficulty, count in leetcode_counts.items():
            html = replace_bar_width(html, f"leetcode-{difficulty}", count / maximum * 100)

    INDEX_FILE.write_text(html, encoding="utf-8")
    print(f"Updated {len(values)} GitHub/LeetCode values in {INDEX_FILE.name}")
    for failure in failures:
        print(f"Warning: {failure}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
