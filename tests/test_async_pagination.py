import asyncio

import httpx
import pytest

from sculptary import AsyncSculptary, AuthenticationRequired, SculptaryError


def repo(n):
    return {
        "id": str(n),
        "full_name": f"alice/model-{n}",
        "name": f"model-{n}",
        "owner": {"slug": "alice"},
        "visibility": "private",
        "default_branch": "main",
        "clone_http": "https://x/model.git",
        "clone_ssh": "git@x:model.git",
    }


@pytest.mark.parametrize("resource", ["pulls", "issues"])
def test_async_page_iteration_is_lazy_and_preserves_filters(resource):
    calls = []

    def handler(request):
        calls.append(request)
        page = int(request.url.params["page"])
        assert request.url.params["state"] == "closed"
        assert request.url.params["per_page"] == "2"
        return httpx.Response(
            200,
            json={
                "items": [{"number": n} for n in ([3, 2] if page == 1 else [1])],
                "page": page,
                "has_more": page == 1,
            },
        )

    async def scenario():
        async with AsyncSculptary(
            host="https://x.test", token="", transport=httpx.MockTransport(handler)
        ) as client:
            iterator = getattr(client, resource).iter("alice/shell", state="closed", per_page=2)
            assert not calls
            first = await iterator.__anext__()
            assert first == {"number": 3} and len(calls) == 1
            assert [first, *[item async for item in iterator]] == [
                {"number": 3},
                {"number": 2},
                {"number": 1},
            ]

    asyncio.run(scenario())
    assert len(calls) == 2


def test_list_response_links_do_not_duplicate_the_api_prefix():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.path == "/api/v1/user/repos"
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            json=[repo(n) for n in ([1, 2] if page == 1 else [3])],
            headers={
                "X-Has-More": str(page == 1).lower(),
                "X-Page": str(page),
                **({"Link": '</api/v1/user/repos?per_page=2&page=2>; rel="next"'} if page == 1 else {}),
            },
        )

    async def scenario():
        async with AsyncSculptary(
            host="https://x.test", token="", transport=httpx.MockTransport(handler)
        ) as client:
            assert [item.full_name async for item in client.repos.iter_mine(per_page=2)] == [
                f"alice/model-{n}" for n in (1, 2, 3)
            ]

    asyncio.run(scenario())
    assert len(calls) == 2


def test_search_uses_total_and_commits_use_has_more():
    def handler(request):
        page = int(request.url.params["page"])
        if request.url.path.endswith("/commits"):
            assert request.url.params["ref"] == "immutable-sha" and request.url.params["path"] == "model.glb"
            return httpx.Response(
                200, json={"commits": [{"oid": str(page)}], "page": page, "has_more": page == 1}
            )
        assert request.url.params["q"] == "shell" and request.url.params["license"] == "CC0-1.0"
        return httpx.Response(
            200, json={"total": 3, "page": page, "items": [repo(n) for n in ([1, 2] if page == 1 else [3])]}
        )

    async def scenario():
        async with AsyncSculptary(
            host="https://x.test", token="", transport=httpx.MockTransport(handler)
        ) as client:
            assert (
                len([item async for item in client.iter_search("shell", per_page=2, license="CC0-1.0")]) == 3
            )
            assert [
                item["oid"]
                async for item in client.repos.iter_commits(
                    "alice/shell", ref="immutable-sha", path="model.glb", per_page=1
                )
            ] == ["1", "2"]

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["external", "cycle", "empty", "legacy", "invalid_flag"])
def test_invalid_pagination_is_reported_without_leaking_credentials_or_looping(mode):
    calls = []

    def handler(request):
        calls.append(request)
        headers = {"X-Has-More": "true"}
        if mode == "external":
            headers["Link"] = '<https://other.test/private>; rel="next"'
        elif mode == "cycle":
            headers["Link"] = '<https://x.test/api/v1/user/repos?per_page=2&page=1>; rel="next"'
        elif mode == "legacy":
            headers = {}
        elif mode == "invalid_flag":
            headers["X-Has-More"] = "maybe"
        return httpx.Response(200, json=[] if mode == "empty" else [repo(1)], headers=headers)

    async def scenario():
        async with AsyncSculptary(
            host="https://x.test", token="test-iterator-credential", transport=httpx.MockTransport(handler)
        ) as client:
            with pytest.raises(SculptaryError) as caught:
                _ = [item async for item in client.repos.iter_mine(per_page=2)]
            assert caught.value.code == "invalid_pagination"

    asyncio.run(scenario())
    assert len(calls) == 1 and calls[0].url.host == "x.test"


def test_permission_failure_on_a_later_page_is_not_treated_as_the_end():
    def handler(request):
        if request.url.params["page"] == "1":
            return httpx.Response(200, json=[{"id": "release"}], headers={"X-Has-More": "true"})
        return httpx.Response(401, json={"error": {"message": "Expired"}})

    async def scenario():
        async with AsyncSculptary(
            host="https://x.test", token="", transport=httpx.MockTransport(handler)
        ) as client:
            iterator = client.releases.iter("alice/shell", per_page=1)
            assert await iterator.__anext__() == {"id": "release"}
            with pytest.raises(AuthenticationRequired):
                await iterator.__anext__()

    asyncio.run(scenario())
