# vim: filetype=python
"""Async GitHub client for PR monitoring, using a single GraphQL query per poll.

One query fetches PR core, reviews, review threads (inline comments + resolution),
conversation comments, the head commit's check rollup, and recent timeline events
(label add/remove, reviewer requested/removed, draft toggled, force-push) whose
globally-unique node ids give recurring transitions a stable identity — far fewer
API calls than the REST equivalent, which keeps us well under rate limits. Rate-limit headers are parsed so the daemon can throttle,
and failures are classified (auth / not-found / rate-limited / transient) so the
daemon can tailor its recovery.

Reads GITHUB_TOKEN. The GraphQL endpoint is GITHUB_GRAPHQL_URL (default
{GITHUB_API_URL}/graphql, GITHUB_API_URL default https://api.github.com), so it
works against GitHub Enterprise and a local fake in tests.
"""

import os
import time

import httpx

_USER_AGENT = "wlr-notifications-daemon"


class GitHubError(Exception):
    """Base for classified GitHub failures."""


class GitHubAuthError(GitHubError):
    """Bad/insufficient credentials (401/403 non-rate-limit, GraphQL FORBIDDEN)."""


class GitHubNotFound(GitHubError):
    """The PR/repo does not exist or the token can't see it (404, GraphQL NOT_FOUND)."""


class GitHubRateLimited(GitHubError):
    """Rate limit hit; `reset_at` is the epoch seconds to wait until."""

    def __init__(self, reset_at: float, message: str = "rate limited") -> None:
        super().__init__(message)
        self.reset_at = reset_at


class GitHubTransient(GitHubError):
    """Server/network error worth retrying with backoff (5xx, timeouts)."""


_PR_QUERY = """
query($owner:String!, $repo:String!, $number:Int!) {
  repository(owner:$owner, name:$repo) {
    pullRequest(number:$number) {
      title url state merged isDraft headRefOid
      mergedBy { login }
      mergeable
      labels(first:50) { nodes { name } }
      reviewRequests(first:50) { nodes { requestedReviewer {
        __typename ... on User { login } ... on Team { name } } } }
      reviews(first:100) { nodes { id state author { login } body url } }
      reviewThreads(first:100) { nodes {
        id isResolved isOutdated path line startLine
        comments(first:50) { nodes {
          id author { login } body url diffHunk path line startLine originalLine originalStartLine } }
      } }
      comments(first:100) { nodes { id author { login } body url } }
      commits(last:1) { nodes { commit { oid statusCheckRollup { state contexts(first:100) { nodes {
        __typename
        ... on CheckRun { id name status conclusion detailsUrl title summary }
        ... on StatusContext { id context state targetUrl description }
      } } } } } }
      timelineItems(
        last:50
        itemTypes:[LABELED_EVENT, UNLABELED_EVENT, REVIEW_REQUESTED_EVENT, REVIEW_REQUEST_REMOVED_EVENT, READY_FOR_REVIEW_EVENT, CONVERT_TO_DRAFT_EVENT, HEAD_REF_FORCE_PUSHED_EVENT]
      ) { nodes {
        __typename
        ... on LabeledEvent { id label { name } }
        ... on UnlabeledEvent { id label { name } }
        ... on ReviewRequestedEvent { id requestedReviewer {
          __typename ... on User { login } ... on Bot { login } ... on Mannequin { login } ... on Team { name } ... on EnterpriseTeam { name } } }
        ... on ReviewRequestRemovedEvent { id requestedReviewer {
          __typename ... on User { login } ... on Bot { login } ... on Mannequin { login } ... on Team { name } ... on EnterpriseTeam { name } } }
        ... on ReadyForReviewEvent { id actor { login } }
        ... on ConvertToDraftEvent { id actor { login } }
        ... on HeadRefForcePushedEvent { id beforeCommit { oid } afterCommit { oid } }
      } }
    }
  }
}
"""


def api_base() -> str:
    return os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")


def graphql_url() -> str:
    return os.environ.get("GITHUB_GRAPHQL_URL", f"{api_base()}/graphql")


class GitHubClient:
    def __init__(self, token: str | None = None) -> None:
        self._token = token if token is not None else os.environ.get("GITHUB_TOKEN")
        self.rate_limit_remaining: int | None = None
        self.rate_limit_reset: float | None = None

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _update_rate_limit(self, headers: httpx.Headers) -> None:
        remaining = headers.get("X-RateLimit-Remaining")
        reset = headers.get("X-RateLimit-Reset")
        if remaining and remaining.lstrip("-").isdigit():
            self.rate_limit_remaining = int(remaining)
        if reset and reset.isdigit():
            self.rate_limit_reset = float(reset)

    def should_throttle(self, threshold: int = 50) -> float | None:
        """If the remaining budget is low, the epoch to wait until; else None."""
        if (
            self.rate_limit_remaining is not None
            and self.rate_limit_remaining <= threshold
            and self.rate_limit_reset
        ):
            return self.rate_limit_reset
        return None

    @staticmethod
    def _reset_at(headers: httpx.Headers) -> float:
        reset = headers.get("X-RateLimit-Reset")
        if reset and reset.isdigit():
            return float(reset)
        retry_after = headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            return time.time() + float(retry_after)
        return time.time() + 60.0

    def _classify_http(self, resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        code = resp.status_code
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if code in (403, 429) and (remaining == "0" or resp.headers.get("Retry-After")):
            raise GitHubRateLimited(
                self._reset_at(resp.headers), f"rate limited (HTTP {code})"
            )
        if code == 401:
            raise GitHubAuthError("unauthorized (401): token invalid or missing")
        if code == 403:
            raise GitHubAuthError("forbidden (403): token lacks access")
        if code == 404:
            raise GitHubNotFound("not found (404)")
        if 500 <= code < 600:
            raise GitHubTransient(f"server error (HTTP {code})")
        raise GitHubTransient(f"unexpected HTTP {code}")

    @staticmethod
    def _classify_graphql(body: dict) -> None:
        errors = body.get("errors")
        if not errors:
            return
        types = {e.get("type") for e in errors}
        message = "; ".join(e.get("message", "") for e in errors if e.get("message"))[
            :300
        ]
        if "NOT_FOUND" in types:
            raise GitHubNotFound(message or "not found")
        if "FORBIDDEN" in types:
            raise GitHubAuthError(message or "forbidden")
        if "RATE_LIMITED" in types:
            raise GitHubRateLimited(time.time() + 60.0, message or "rate limited")
        raise GitHubTransient(message or "graphql error")

    async def fetch_pr(self, owner: str, repo: str, number: int) -> dict:
        """Return the GraphQL pullRequest node, or raise a classified GitHubError."""
        payload = {
            "query": _PR_QUERY,
            "variables": {"owner": owner, "repo": repo, "number": number},
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
                resp = await client.post(
                    graphql_url(), headers=self._headers(), json=payload
                )
        except httpx.HTTPError as exc:
            raise GitHubTransient(f"network error: {exc}") from exc
        self._update_rate_limit(resp.headers)
        self._classify_http(resp)
        try:
            body = resp.json()
        except ValueError as exc:
            raise GitHubTransient(f"non-JSON response: {exc}") from exc
        self._classify_graphql(body)
        pr = ((body.get("data") or {}).get("repository") or {}).get("pullRequest")
        if pr is None:
            raise GitHubNotFound(f"{owner}/{repo}#{number} not found")
        return pr
