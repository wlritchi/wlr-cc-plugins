# vim: filetype=python
"""Minimal async GitHub REST client for PR monitoring.

Reads GITHUB_TOKEN from the environment. The API base is GITHUB_API_URL (default
https://api.github.com) so tests can point it at a local fake and it also works
against GitHub Enterprise.
"""

import os

import httpx

_PER_PAGE = 100
_MAX_PAGES = 10


def api_base() -> str:
    return os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")


class GitHubClient:
    def __init__(self, token: str | None = None) -> None:
        self._token = token if token is not None else os.environ.get("GITHUB_TOKEN")

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _get_one(self, client: httpx.AsyncClient, path: str) -> dict:
        resp = await client.get(f"{api_base()}{path}", headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    async def _get_all(self, client: httpx.AsyncClient, path: str) -> list[dict]:
        items: list[dict] = []
        url: str | None = f"{api_base()}{path}"
        params: dict | None = {"per_page": _PER_PAGE}
        for _ in range(_MAX_PAGES):
            if url is None:
                break
            resp = await client.get(url, headers=self._headers(), params=params)
            resp.raise_for_status()
            page = resp.json()
            if not isinstance(page, list):
                return page
            items.extend(page)
            nxt = resp.links.get("next")
            url = nxt["url"] if nxt else None
            params = None  # the next link already carries pagination params
        return items

    async def fetch_pr_state(self, owner: str, repo: str, number: int) -> dict:
        """Fetch everything we diff on: PR core, reviews, comments, checks, statuses."""
        base = f"/repos/{owner}/{repo}"
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
            pr = await self._get_one(client, f"{base}/pulls/{number}")
            head_sha = (pr.get("head") or {}).get("sha")
            reviews = await self._get_all(client, f"{base}/pulls/{number}/reviews")
            review_comments = await self._get_all(
                client, f"{base}/pulls/{number}/comments"
            )
            issue_comments = await self._get_all(
                client, f"{base}/issues/{number}/comments"
            )
            check_runs: list[dict] = []
            status: dict = {}
            if head_sha:
                runs = await self._get_one(
                    client, f"{base}/commits/{head_sha}/check-runs"
                )
                check_runs = runs.get("check_runs", []) or []
                status = await self._get_one(
                    client, f"{base}/commits/{head_sha}/status"
                )
            return {
                "pr": pr,
                "reviews": reviews,
                "review_comments": review_comments,
                "issue_comments": issue_comments,
                "check_runs": check_runs,
                "status": status,
            }
