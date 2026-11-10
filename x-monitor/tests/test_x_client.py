from __future__ import annotations

import time
from datetime import UTC, datetime

import httpx
import pytest
import respx

from models import EventType
from services.x_client import (
    XAuthError,
    XClient,
    XNotFound,
    XPaymentRequired,
    XRateLimited,
    XTransientError,
    datetime_from_snowflake,
    parse_posts,
    snowflake_from_datetime,
)

BASE = "https://api.x.com/2"
UID = 44196397


def client(**kw) -> XClient:
    return XClient("test-bearer", BASE, retry_base_delay=0, **kw)


TIMELINE = {
    "data": [
        {
            "id": "1900000000000000005",
            "author_id": str(UID),
            "created_at": "2026-11-10T09:30:00.000Z",
            "text": "Look at this https://t.co/abc https://t.co/pic",
            "entities": {
                "urls": [
                    {"url": "https://t.co/abc", "expanded_url": "https://example.com/article"},
                    {
                        "url": "https://t.co/pic",
                        "expanded_url": "https://x.com/elonmusk/status/1900000000000000005/photo/1",
                        "media_key": "3_1",
                    },
                ]
            },
            "attachments": {"media_keys": ["3_1"]},
        },
        {
            "id": "1900000000000000004",
            "author_id": str(UID),
            "created_at": "2026-11-10T09:20:00.000Z",
            "text": "@nasa great launch",
            "in_reply_to_user_id": "11348282",
            "referenced_tweets": [{"type": "replied_to", "id": "1899999999999999990"}],
        },
        {
            "id": "1900000000000000003",
            "author_id": str(UID),
            "created_at": "2026-11-10T09:10:00.000Z",
            "text": "RT @spacex: Starship flight 12 is go for launch…",
            "referenced_tweets": [{"type": "retweeted", "id": "1899999999999999980"}],
        },
        {
            "id": "1900000000000000002",
            "author_id": str(UID),
            "created_at": "2026-11-10T09:00:00.000Z",
            "text": "Exactly",
            "referenced_tweets": [{"type": "quoted", "id": "1899999999999999970"}],
        },
        {
            "id": "1900000000000000001",
            "author_id": str(UID),
            "created_at": "2026-11-10T08:50:00.000Z",
            "text": "short",
            "note_tweet": {"text": "This is the full long post text " * 20},
        },
    ],
    "includes": {
        "users": [
            {"id": "11348282", "username": "NASA", "name": "NASA"},
            {"id": "34743251", "username": "SpaceX", "name": "SpaceX"},
            {"id": "1", "username": "someone", "name": "Some One"},
        ],
        "tweets": [
            {"id": "1899999999999999990", "author_id": "11348282", "text": "We have liftoff!"},
            {
                "id": "1899999999999999980",
                "author_id": "34743251",
                "text": "Starship flight 12 is go for launch, full text here",
            },
            {"id": "1899999999999999970", "author_id": "1", "text": "Quoted original text"},
        ],
        "media": [{"media_key": "3_1", "type": "photo", "url": "https://pbs.twimg.com/media/a.jpg"}],
    },
    "meta": {"result_count": 5, "newest_id": "1900000000000000005", "oldest_id": "1900000000000000001"},
}


def test_snowflake_roundtrip() -> None:
    dt = datetime(2026, 11, 10, 12, 30, tzinfo=UTC)
    sf = snowflake_from_datetime(dt)
    assert datetime_from_snowflake(sf) == dt
    assert len(str(sf)) <= 19
    # A real post ID from 2023 must be smaller than a 2026 baseline
    assert 1700000000000000000 < sf


def test_parse_all_activity_types() -> None:
    posts = {p.id: p for p in parse_posts(TIMELINE)}
    media_post = posts[1900000000000000005]
    assert media_post.type is EventType.POST
    assert media_post.text == "Look at this https://example.com/article"
    assert media_post.media[0].url == "https://pbs.twimg.com/media/a.jpg"

    reply = posts[1900000000000000004]
    assert reply.type is EventType.REPLY
    assert reply.in_reply_to_username == "NASA"
    assert reply.referenced.text == "We have liftoff!"
    assert reply.referenced.url == "https://x.com/NASA/status/1899999999999999990"

    repost = posts[1900000000000000003]
    assert repost.type is EventType.REPOST
    assert repost.referenced.author_username == "SpaceX"
    assert repost.referenced.text.endswith("full text here")

    quote = posts[1900000000000000002]
    assert quote.type is EventType.QUOTE
    assert quote.text == "Exactly"
    assert quote.referenced.author_name == "Some One"
    assert quote.referenced.text == "Quoted original text"

    long_post = posts[1900000000000000001]
    assert long_post.text.startswith("This is the full long post text")
    assert len(long_post.text) > 300


def test_parse_v2_field_names_and_quote_reply() -> None:
    payload = {
        "data": [
            {
                "id": "10",
                "text": "@bob agreed",
                "created_at": "2026-11-10T09:00:00Z",
                "in_reply_to_user_id": "2",
                "referenced_posts": [{"type": "replied_to", "id": "8"}, {"type": "quoted", "id": "7"}],
                "note_post": {"text": "@bob agreed, long version"},
            },
            {"id": "11", "text": "RT @carol: hi there", "referenced_posts": [{"type": "retweeted", "id": "9"}]},
        ],
        "includes": {"users": [{"id": "2", "username": "bob", "name": "Bob"}], "posts": [{"id": "8", "text": "orig"}]},
    }
    posts = {p.id: p for p in parse_posts(payload)}
    assert posts[10].type is EventType.REPLY
    assert posts[10].text == "@bob agreed, long version"
    assert posts[10].quoted.id == 7
    assert posts[10].in_reply_to_username == "bob"
    assert posts[10].referenced.text == "orig"
    # Retweet without expansions: author from "RT @user:" prefix, text from the RT body
    assert posts[11].type is EventType.REPOST
    assert posts[11].referenced.author_username == "carol"
    assert posts[11].referenced.text == "hi there"


@respx.mock
async def test_get_user_posts_since_id_and_pagination() -> None:
    route = respx.get(f"{BASE}/users/{UID}/tweets").mock(
        side_effect=[
            httpx.Response(200, json={**TIMELINE, "meta": {"next_token": "page2"}}),
            httpx.Response(
                200, json={"data": [{"id": "1899999999999999000", "text": "old, before since_id"}], "meta": {}}
            ),
        ]
    )
    x = client()
    posts = await x.get_user_posts(UID, since_id=1899999999999999999, max_pages=3)
    assert [p.id for p in posts] == sorted(int(p["id"]) for p in TIMELINE["data"])  # old one filtered, sorted asc
    first, second = route.calls[0].request, route.calls[1].request
    assert first.url.params["since_id"] == "1899999999999999999"
    assert first.url.params["tweet.fields"]
    assert "referenced_tweets.id" in first.url.params["expansions"]
    assert second.url.params["pagination_token"] == "page2"
    assert first.headers["Authorization"] == "Bearer test-bearer"
    await x.close()


@respx.mock
async def test_empty_timeline_returns_nothing() -> None:
    respx.get(f"{BASE}/users/{UID}/tweets").mock(return_value=httpx.Response(200, json={"meta": {"result_count": 0}}))
    x = client()
    assert await x.get_user_posts(UID, since_id=1) == []
    await x.close()


@respx.mock
async def test_rate_limit_headers_and_429() -> None:
    reset = int(time.time()) + 600
    respx.get(f"{BASE}/users/{UID}/tweets").mock(
        return_value=httpx.Response(
            200, json={"meta": {}}, headers={"x-rate-limit-remaining": "0", "x-rate-limit-reset": str(reset)}
        )
    )
    x = client()
    await x.get_user_posts(UID, since_id=1)
    # Budget exhausted: the next call must not even hit the network.
    with pytest.raises(XRateLimited) as exc:
        await x.get_user_posts(UID, since_id=1)
    assert exc.value.reset_at == reset
    assert respx.calls.call_count == 1
    await x.close()

    respx.get(f"{BASE}/users/42/tweets").mock(
        return_value=httpx.Response(429, headers={"x-rate-limit-reset": str(reset)})
    )
    x = client()
    with pytest.raises(XRateLimited):
        await x.get_user_posts(42, since_id=1)
    await x.close()


@respx.mock
async def test_server_errors_are_retried() -> None:
    route = respx.get(f"{BASE}/users/{UID}/tweets").mock(
        side_effect=[httpx.Response(503), httpx.ConnectError("boom"), httpx.Response(200, json=TIMELINE)]
    )
    x = client(max_retries=3)
    assert len(await x.get_user_posts(UID, since_id=1)) == 5
    assert route.call_count == 3
    await x.close()

    respx.get(f"{BASE}/users/7/tweets").mock(return_value=httpx.Response(500))
    x = client(max_retries=2)
    with pytest.raises(XTransientError):
        await x.get_user_posts(7, since_id=1)
    await x.close()


@respx.mock
@pytest.mark.parametrize(("status", "exc"), [(401, XAuthError), (402, XPaymentRequired), (404, XNotFound)])
async def test_error_mapping(status: int, exc: type[Exception]) -> None:
    respx.get(f"{BASE}/users/{UID}/tweets").mock(return_value=httpx.Response(status, json={"title": "x"}))
    x = client()
    with pytest.raises(exc):
        await x.get_user_posts(UID, since_id=1)
    await x.close()


@respx.mock
async def test_dialect_auto_fallback_to_post_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "tweet.fields" in request.url.params:
            return httpx.Response(
                400, json={"errors": [{"message": "The query parameter [tweet.fields] is not one of"}]}
            )
        assert "post.fields" in request.url.params
        return httpx.Response(200, json={"data": [{"id": "5", "text": "hi"}]})

    respx.get(f"{BASE}/users/{UID}/tweets").mock(side_effect=handler)
    x = client()
    posts = await x.get_user_posts(UID, since_id=1)
    assert [p.id for p in posts] == [5]
    assert x.dialect.name == "v2"
    await x.close()


@respx.mock
async def test_since_id_rejected_falls_back_to_start_time() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "since_id" in request.url.params:
            return httpx.Response(400, json={"errors": [{"message": "Invalid since_id"}]})
        assert request.url.params["start_time"].endswith("Z")
        return httpx.Response(
            200, json={"data": [{"id": str(snowflake_from_datetime(datetime.now(UTC))), "text": "x"}]}
        )

    respx.get(f"{BASE}/users/{UID}/tweets").mock(side_effect=handler)
    x = client()
    since = snowflake_from_datetime(datetime(2026, 1, 1, tzinfo=UTC))
    assert len(await x.get_user_posts(UID, since_id=since)) == 1
    await x.close()


@respx.mock
async def test_user_lookup() -> None:
    respx.get(f"{BASE}/users/by/username/elonmusk").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "id": str(UID),
                    "username": "elonmusk",
                    "name": "Elon Musk",
                    "profile_image_url": "https://pbs.twimg.com/profile_images/1/a_normal.jpg",
                }
            },
        )
    )
    respx.get(f"{BASE}/users/by/username/nobody_here").mock(
        return_value=httpx.Response(
            200,
            json={
                "errors": [
                    {
                        "detail": "Could not find user with username: [nobody_here].",
                        "type": "https://api.twitter.com/2/problems/resource-not-found",
                    }
                ]
            },
        )
    )
    x = client()
    user = await x.get_user_by_username("elonmusk")
    assert (user.id, user.name, user.protected) == (UID, "Elon Musk", False)
    assert user.avatar_url.endswith("a_400x400.jpg")
    with pytest.raises(XNotFound):
        await x.get_user_by_username("nobody_here")
    await x.close()
