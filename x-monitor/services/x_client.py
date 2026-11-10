"""Client for the official X API v2 (read-only, app-only Bearer token).

Endpoints used:
  GET /2/users/by/username/{username}   - profile lookup when an account is added
  GET /2/users/{id}/tweets              - user timeline: posts, replies, reposts and quotes

The timeline is polled with ``since_id`` so each request only returns posts we have not seen.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from models import EventType

log = logging.getLogger(__name__)

TWITTER_EPOCH_MS = 1288834974657


def snowflake_from_datetime(dt: datetime) -> int:
    """Smallest possible X post ID created at ``dt``; used as a since_id baseline."""
    return (int(dt.timestamp() * 1000) - TWITTER_EPOCH_MS) << 22


def datetime_from_snowflake(snowflake: int) -> datetime:
    return datetime.fromtimestamp(((snowflake >> 22) + TWITTER_EPOCH_MS) / 1000, tz=UTC)


# ---------------------------------------------------------------- errors


class XApiError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class XAuthError(XApiError):
    """401: bearer token invalid or revoked."""


class XPaymentRequired(XApiError):
    """402: pay-per-use credits exhausted or billing not set up."""


class XForbidden(XApiError):
    """403 on the app level: the app/project has no access to the endpoint."""


class XNotFound(XApiError):
    """The user does not exist (or was deleted)."""


class XAccountUnavailable(XApiError):
    """The user exists but posts cannot be read (suspended, protected)."""


class XBadRequest(XApiError):
    pass


class XTransientError(XApiError):
    """Network failure or 5xx after all retries."""


class XRateLimited(XApiError):
    def __init__(self, reset_at: float, message: str = "rate limited") -> None:
        super().__init__(message, 429)
        self.reset_at = reset_at


# ---------------------------------------------------------------- data


@dataclass(slots=True)
class XUser:
    id: int
    username: str
    name: str
    avatar_url: str | None = None
    protected: bool = False

    @property
    def profile_url(self) -> str:
        return f"https://x.com/{self.username}"


@dataclass(slots=True)
class XMedia:
    type: str
    url: str | None

    def as_dict(self) -> dict[str, Any]:
        return {"type": self.type, "url": self.url}


@dataclass(slots=True)
class XReferencedPost:
    id: int
    author_id: int | None = None
    author_username: str | None = None
    author_name: str | None = None
    text: str | None = None

    @property
    def url(self) -> str:
        if self.author_username:
            return f"https://x.com/{self.author_username}/status/{self.id}"
        return f"https://x.com/i/web/status/{self.id}"


@dataclass(slots=True)
class XPost:
    id: int
    author_id: int | None
    text: str
    created_at: datetime
    type: EventType
    in_reply_to_user_id: int | None = None
    in_reply_to_username: str | None = None
    referenced: XReferencedPost | None = None
    quoted: XReferencedPost | None = None  # quote inside a reply
    media: list[XMedia] = field(default_factory=list)

    def url_for(self, username: str) -> str:
        return f"https://x.com/{username}/status/{self.id}"


# ---------------------------------------------------------------- request dialects


@dataclass(frozen=True, slots=True)
class Dialect:
    name: str
    fields_param: str
    post_fields: str
    expansions_full: str
    expansions_min: str


# Historic, still documented in the data dictionary.
LEGACY = Dialect(
    name="legacy",
    fields_param="tweet.fields",
    post_fields="created_at,author_id,conversation_id,in_reply_to_user_id,referenced_tweets,attachments,entities,note_tweet",
    expansions_full="referenced_tweets.id,referenced_tweets.id.author_id,in_reply_to_user_id,attachments.media_keys",
    expansions_min="attachments.media_keys",
)
# Names from the current OpenAPI spec (https://api.x.com/2/openapi.json, v2.174).
V2 = Dialect(
    name="v2",
    fields_param="post.fields",
    post_fields="created_at,conversation_id,attachments,entities,note_post",
    expansions_full="referenced_posts,in_reply_to_user_id,attachments.media_keys",
    expansions_min="attachments.media_keys",
)

USER_FIELDS = "name,username,profile_image_url,protected"
MEDIA_FIELDS = "type,url,preview_image_url"
_DIALECT_HINTS = ("tweet.fields", "post.fields", "referenced_tweets", "referenced_posts", "expansions", "note_")


# ---------------------------------------------------------------- rate limits


class RateLimitTracker:
    """Tracks x-rate-limit-* headers per endpoint so we stop before X answers 429."""

    def __init__(self) -> None:
        self._state: dict[str, tuple[int, float]] = {}

    def check(self, endpoint: str) -> None:
        state = self._state.get(endpoint)
        if state is None:
            return
        remaining, reset_at = state
        if remaining <= 0 and reset_at > time.time():
            raise XRateLimited(reset_at, f"rate limit for {endpoint} exhausted")

    def update(self, endpoint: str, headers: httpx.Headers) -> None:
        try:
            remaining = int(headers["x-rate-limit-remaining"])
            reset_at = float(headers["x-rate-limit-reset"])
        except (KeyError, ValueError):
            return
        self._state[endpoint] = (remaining, reset_at)

    def snapshot(self) -> dict[str, dict[str, float]]:
        return {k: {"remaining": r, "reset_at": t} for k, (r, t) in self._state.items()}


# ---------------------------------------------------------------- client


class XClient:
    def __init__(
        self,
        bearer_token: str,
        base_url: str = "https://api.x.com/2",
        *,
        dialect: str = "auto",
        timeout: float = 20.0,
        max_retries: int = 3,
        fetch_references: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_base_delay: float = 1.0,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {bearer_token}", "User-Agent": "x-monitor/1.0"},
            timeout=timeout,
            transport=transport,
        )
        self._auto_dialect = dialect == "auto"
        self.dialect: Dialect = V2 if dialect == "v2" else LEGACY
        self._dialect_switched = False
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._fetch_references = fetch_references
        self.rate_limits = RateLimitTracker()

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------ low level

    async def _get(self, endpoint: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        self.rate_limits.check(endpoint)
        attempt = 0
        while True:
            try:
                resp = await self._http.get(path, params=params)
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise XTransientError(f"network error: {type(exc).__name__}") from exc
                await self._sleep_backoff(attempt)
                attempt += 1
                continue

            self.rate_limits.update(endpoint, resp.headers)
            status = resp.status_code
            if status == 200:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise XTransientError("invalid JSON from X API") from exc
            if status == 429:
                reset = resp.headers.get("x-rate-limit-reset")
                reset_at = float(reset) if reset and reset.isdigit() else time.time() + 60
                raise XRateLimited(reset_at, f"429 on {endpoint}")
            if status >= 500:
                if attempt >= self._max_retries:
                    raise XTransientError(f"X API {status} on {endpoint}", status)
                await self._sleep_backoff(attempt)
                attempt += 1
                continue

            detail = _error_detail(resp)
            if status == 401:
                raise XAuthError(f"401 Unauthorized: {detail}", status)
            if status == 402:
                raise XPaymentRequired(f"402 Payment Required: {detail}", status)
            if status == 403:
                raise XForbidden(f"403 Forbidden: {detail}", status)
            if status == 404:
                raise XNotFound(f"404 Not Found: {detail}", status)
            raise XBadRequest(f"{status}: {detail}", status)

    async def _sleep_backoff(self, attempt: int) -> None:
        delay = self._retry_base_delay * (2**attempt) * (1 + random.random() * 0.25)
        await asyncio.sleep(delay)

    async def _get_with_dialect(self, endpoint: str, path: str, build: Any) -> dict[str, Any]:
        """Calls ``build(dialect)`` for params; on a field-name 400 switches dialect once (auto mode)."""
        try:
            return await self._get(endpoint, path, build(self.dialect))
        except XBadRequest as exc:
            if not (self._auto_dialect and not self._dialect_switched and _mentions_dialect(str(exc))):
                raise
            other = V2 if self.dialect is LEGACY else LEGACY
            log.warning("X API rejected %s parameters, switching to %s field names", self.dialect.name, other.name)
            self.dialect = other
            self._dialect_switched = True
            return await self._get(endpoint, path, build(self.dialect))

    # ------------------------------------------------------------ public API

    async def get_user_by_username(self, username: str) -> XUser:
        payload = await self._get(
            "users/by/username",
            f"/users/by/username/{username}",
            {"user.fields": USER_FIELDS},
        )
        data = payload.get("data")
        if not data:
            _raise_payload_errors(payload, default=XNotFound(f"@{username} not found"))
        return _parse_user(data)

    async def get_user_posts(
        self,
        user_id: int,
        since_id: int,
        *,
        max_pages: int = 3,
    ) -> list[XPost]:
        """All posts newer than ``since_id`` (oldest first). Includes replies, reposts and quotes."""

        posts: dict[int, XPost] = {}
        pagination_token: str | None = None
        use_start_time = False
        for _page in range(max_pages):

            def build(d: Dialect, token: str | None = pagination_token, by_time: bool = use_start_time) -> dict:
                params: dict[str, Any] = {
                    "max_results": 100,
                    d.fields_param: d.post_fields,
                    "expansions": d.expansions_full if self._fetch_references else d.expansions_min,
                    "user.fields": "name,username",
                    "media.fields": MEDIA_FIELDS,
                }
                if by_time:
                    params["start_time"] = _iso(datetime_from_snowflake(since_id))
                else:
                    params["since_id"] = str(since_id)
                if token:
                    params["pagination_token"] = token
                return params

            try:
                payload = await self._get_with_dialect("users/:id/tweets", f"/users/{user_id}/tweets", build)
            except XBadRequest as exc:
                # A synthetic baseline since_id may be rejected; fall back to an equivalent start_time.
                if "since_id" in str(exc) and not use_start_time:
                    log.info("since_id rejected for user %s, retrying with start_time", user_id)
                    use_start_time = True
                    pagination_token = None
                    continue
                raise
            except XNotFound as exc:
                raise XNotFound(f"user {user_id} not found", exc.status) from exc

            data = payload.get("data") or []
            if not data and payload.get("errors"):
                _raise_payload_errors(payload, default=None)
            for post in parse_posts(payload):
                if post.id > since_id:
                    posts[post.id] = post
            pagination_token = (payload.get("meta") or {}).get("next_token")
            if not pagination_token:
                break
        else:
            if pagination_token:
                log.warning("user %s: more new posts than %d pages, older ones skipped", user_id, max_pages)
        return sorted(posts.values(), key=lambda p: p.id)


# ---------------------------------------------------------------- parsing


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _error_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300]
    parts = [str(body.get(k)) for k in ("title", "detail") if body.get(k)]
    for err in body.get("errors") or []:
        parts.append(str(err.get("message") or err.get("detail") or err))
    return "; ".join(parts)[:500] or resp.text[:300]


def _mentions_dialect(message: str) -> bool:
    return any(h in message for h in _DIALECT_HINTS)


def _raise_payload_errors(payload: dict[str, Any], default: Exception | None) -> None:
    for err in payload.get("errors") or []:
        etype = str(err.get("type", ""))
        detail = str(err.get("detail") or err.get("title") or err)
        lowered = detail.lower()
        if "resource-not-found" in etype or "could not find" in lowered:
            raise XNotFound(detail)
        if "suspended" in lowered:
            raise XAccountUnavailable(f"suspended: {detail}")
        if "not-authorized" in etype or "authorization-error" in etype or "protected" in lowered:
            raise XAccountUnavailable(f"protected or not authorized: {detail}")
    if default is not None:
        raise default


def _parse_user(data: dict[str, Any]) -> XUser:
    avatar = data.get("profile_image_url")
    if avatar:
        avatar = avatar.replace("_normal.", "_400x400.")
    return XUser(
        id=int(data["id"]),
        username=data.get("username", ""),
        name=data.get("name", ""),
        avatar_url=avatar,
        protected=bool(data.get("protected", False)),
    )


def _parse_dt(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


_TCO_RE = re.compile(r"https://t\.co/\w+")


def full_text(post: dict[str, Any]) -> str:
    """Long-post text if present, with t.co links expanded and trailing media links removed."""
    note = post.get("note_tweet") or post.get("note_post") or {}
    text = note.get("text") or post.get("text") or ""
    entities = (note.get("entities") if note.get("text") else None) or post.get("entities") or {}
    for url in entities.get("urls") or []:
        short = url.get("url")
        if not short:
            continue
        if (
            url.get("media_key")
            or "/photo/" in (url.get("expanded_url") or "")
            or "/video/" in (url.get("expanded_url") or "")
        ):
            text = text.replace(short, "")
        elif url.get("expanded_url"):
            text = text.replace(short, url["expanded_url"])
    return text.strip()


def _refs(post: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for ref in post.get("referenced_tweets") or post.get("referenced_posts") or []:
        if ref.get("type") and ref.get("id"):
            out[ref["type"]] = int(ref["id"])
    return out


def parse_posts(payload: dict[str, Any]) -> list[XPost]:
    includes = payload.get("includes") or {}
    users = {int(u["id"]): u for u in includes.get("users") or [] if u.get("id")}
    included_posts = {int(p["id"]): p for p in (includes.get("tweets") or includes.get("posts") or []) if p.get("id")}
    media = {m["media_key"]: m for m in includes.get("media") or [] if m.get("media_key")}

    def referenced(post_id: int) -> XReferencedPost:
        ref = XReferencedPost(id=post_id)
        src = included_posts.get(post_id)
        if src:
            ref.text = full_text(src)
            if src.get("author_id"):
                ref.author_id = int(src["author_id"])
                user = users.get(ref.author_id)
                if user:
                    ref.author_username = user.get("username")
                    ref.author_name = user.get("name")
        return ref

    def media_of(post: dict[str, Any]) -> list[XMedia]:
        out = []
        for key in (post.get("attachments") or {}).get("media_keys") or []:
            m = media.get(key)
            if m:
                out.append(XMedia(type=m.get("type", "photo"), url=m.get("url") or m.get("preview_image_url")))
        return out

    result: list[XPost] = []
    for raw in payload.get("data") or []:
        refs = _refs(raw)
        reply_to = int(raw["in_reply_to_user_id"]) if raw.get("in_reply_to_user_id") else None
        post = XPost(
            id=int(raw["id"]),
            author_id=int(raw["author_id"]) if raw.get("author_id") else None,
            text=full_text(raw),
            created_at=_parse_dt(raw.get("created_at")),
            type=EventType.POST,
            in_reply_to_user_id=reply_to,
            media=media_of(raw),
        )
        if "retweeted" in refs:
            post.type = EventType.REPOST
            post.referenced = referenced(refs["retweeted"])
            if not post.referenced.author_username:
                m = re.match(r"RT @(\w{1,15}):", raw.get("text", ""))
                if m:
                    post.referenced.author_username = m.group(1)
            if post.referenced.text is None:
                post.referenced.text = re.sub(r"^RT @\w{1,15}:\s*", "", post.text)
            if not post.media and post.referenced.id in included_posts:
                post.media = media_of(included_posts[post.referenced.id])
        elif "replied_to" in refs or reply_to:
            post.type = EventType.REPLY
            if "replied_to" in refs:
                post.referenced = referenced(refs["replied_to"])
            if "quoted" in refs:
                post.quoted = referenced(refs["quoted"])
        elif "quoted" in refs:
            post.type = EventType.QUOTE
            post.referenced = referenced(refs["quoted"])

        if post.type is EventType.REPLY:
            if reply_to and reply_to in users:
                post.in_reply_to_username = users[reply_to].get("username")
            elif post.referenced and post.referenced.author_username:
                post.in_reply_to_username = post.referenced.author_username
            if post.referenced and not post.referenced.author_username and post.in_reply_to_username:
                if post.referenced.author_id in (None, reply_to):
                    post.referenced.author_username = post.in_reply_to_username
        result.append(post)
    return result
