import asyncio
import inspect
import io
import json

import httpx
import pytest

from sculptary import AsyncSculptary, NotFound, PermissionDenied, Sculptary

REPO = {
    "id": "r",
    "full_name": "alice/shell",
    "name": "shell",
    "owner": {"slug": "alice"},
    "visibility": "private",
    "default_branch": "main",
    "clone_http": "https://x/shell.git",
    "clone_ssh": "git@x:shell.git",
}
ASSET = {
    "id": "asset",
    "path": "model.glb",
    "commit": "abc",
    "format": "glb",
    "status": "ready",
    "metadata": {"triangle_count": 12},
}


def run(test):
    return asyncio.run(test())


def test_async_resources_match_the_sync_call_surface():
    async def scenario():
        with Sculptary(token="") as sync:
            async with AsyncSculptary(token="") as client:
                for name in (
                    "users",
                    "repos",
                    "contents",
                    "assets",
                    "releases",
                    "pulls",
                    "issues",
                    "collections",
                ):
                    resource, counterpart = getattr(client, name), getattr(sync, name)
                    for method_name, method in inspect.getmembers(type(counterpart), inspect.isfunction):
                        if method_name.startswith("_"):
                            continue
                        implementation = getattr(type(resource), method_name)
                        assert inspect.iscoroutinefunction(implementation), (
                            f"{name}.{method_name} must await network I/O"
                        )
                        expected, actual = inspect.signature(method), inspect.signature(implementation)
                        assert list(expected.parameters) == list(actual.parameters)
                        assert [(p.kind, p.default) for p in expected.parameters.values()] == [
                            (p.kind, p.default) for p in actual.parameters.values()
                        ]

    run(scenario)


def test_async_requests_overlap_and_clients_close():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        count = 0

        async def handler(request):
            nonlocal count
            assert request.headers["authorization"] == "Bearer test-async-credential"
            count += 1
            if count == 2:
                entered.set()
            await release.wait()
            return httpx.Response(200, json={"username": "alice"})

        async with AsyncSculptary(
            host="https://x.test", token="test-async-credential", transport=httpx.MockTransport(handler)
        ) as client:
            first = asyncio.create_task(client.users.me())
            second = asyncio.create_task(client.users.get("alice"))
            await asyncio.wait_for(entered.wait(), timeout=2)
            release.set()
            assert await first == await second == {"username": "alice"}
        assert client._http.is_closed

    run(scenario)


def test_async_parses_models_and_preserves_wire_payloads():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/assets"):
            return httpx.Response(200, json={"assets": [ASSET]})
        if request.url.path.endswith("/collections/alice/kits"):
            return httpx.Response(200, json={"name": "Kits", "future_field": True})
        if request.url.path.endswith("/comments"):
            return httpx.Response(201, json={"id": "comment", **json.loads(request.content)})
        if request.url.path.endswith("/search/repositories"):
            return httpx.Response(200, json={"items": [REPO]})
        return httpx.Response(200, json=REPO)

    async def scenario():
        async with AsyncSculptary(host="https://x.test", transport=httpx.MockTransport(handler)) as client:
            assert (await client.repos.get("alice/shell")).owner == "alice"
            assert (await client.assets.list("alice/shell"))[0].triangles == 12
            assert (await client.collections.get("alice/kits"))["future_field"] is True
            assert (await client.pulls.reply("alice/shell", 7, "root", "Fixed"))["parent_id"] == "root"
            assert (await client.search("shell"))[0].full_name == "alice/shell"

    run(scenario)
    assert json.loads(calls[-2].content) == {"body": "Fixed", "parent_id": "root"}


@pytest.mark.parametrize("status,exception", [(403, PermissionDenied), (404, NotFound)])
def test_async_errors_keep_server_details(status, exception):
    async def scenario():
        async with AsyncSculptary(
            host="https://x.test",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    status, json={"error": {"code": "fixture", "message": "Unavailable", "field": "name"}}
                )
            ),
        ) as client:
            with pytest.raises(exception) as caught:
                await client.repos.get("alice/shell")
            assert caught.value.message == "Unavailable" and caught.value.field == "name"

    run(scenario)


def test_async_upload_is_multipart_and_uses_the_selected_head(tmp_path):
    path = tmp_path / "model.glb"
    path.write_bytes(b"glTF-fixture")
    calls = []

    async def handler(request):
        calls.append(request)
        if request.url.path.endswith("/branches"):
            return httpx.Response(200, json=[{"name": "fit/v2", "commit": "selected-head"}])
        body = await request.aread()
        assert b'filename="model.glb"' in body and b"selected-head" in body and b"glTF-fixture" in body
        return httpx.Response(200, json={"commit": "new-head"})

    async def scenario():
        async with AsyncSculptary(host="https://x.test", transport=httpx.MockTransport(handler)) as client:
            result = await client.contents.upload(
                "alice/shell", {"models/model.glb": path}, message="Adjust", branch="fit/v2"
            )
            assert result["commit"] == "new-head"

    run(scenario)
    assert [request.method for request in calls] == ["GET", "POST"]
    path.unlink()  # The multipart file handle must be closed, including on Windows.


def test_async_download_redirects_strip_credentials_and_keep_destinations(tmp_path):
    seen = []

    async def handler(request):
        seen.append((request.url.host, request.headers.get("authorization")))
        if request.url.host == "x.test":
            return httpx.Response(302, headers={"location": "https://storage.test/signed"})
        return httpx.Response(200, content=b"model-bytes")

    async def scenario():
        async with AsyncSculptary(
            host="https://x.test", token="test-async-credential", transport=httpx.MockTransport(handler)
        ) as client:
            dest = tmp_path / "download.glb"
            assert await client.contents.download("alice/shell", "model.glb", dest, ref="abc") == 11
            assert dest.read_bytes() == b"model-bytes"
            buffer = io.BytesIO()
            assert await client.contents.download("alice/shell", "model.glb", buffer) == 11
            assert buffer.getvalue() == b"model-bytes" and not buffer.closed

    run(scenario)
    assert seen == [("x.test", "Bearer test-async-credential"), ("storage.test", None)] * 2


def test_async_failed_download_preserves_existing_file_and_closes_the_stream(tmp_path):
    class Interrupted(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"x" * (1024 * 1024)
            raise httpx.ReadError("Interrupted fixture")

        async def aclose(self):
            self.closed = True

    stream = Interrupted()
    dest = tmp_path / "model.glb"
    dest.write_bytes(b"previous-model")

    async def scenario():
        async with AsyncSculptary(
            host="https://x.test",
            token="",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
        ) as client:
            with pytest.raises(httpx.ReadError):
                await client.contents.download("alice/shell", "model.glb", dest)

    run(scenario)
    assert stream.closed and dest.read_bytes() == b"previous-model"
    assert list(tmp_path.iterdir()) == [dest]


def test_async_cancelled_download_cleans_up_without_replacing_the_file(tmp_path):
    dest = tmp_path / "model.glb"
    dest.write_bytes(b"previous-model")

    async def scenario():
        entered = asyncio.Event()

        class Waiting(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                yield b"x" * (1024 * 1024)
                entered.set()
                await asyncio.Event().wait()

            async def aclose(self):
                self.closed = True

        stream = Waiting()
        async with AsyncSculptary(
            host="https://x.test",
            token="",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
        ) as client:
            task = asyncio.create_task(client.contents.download("alice/shell", "model.glb", dest))
            await asyncio.wait_for(entered.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert stream.closed

    run(scenario)
    assert dest.read_bytes() == b"previous-model" and list(tmp_path.iterdir()) == [dest]


def test_async_request_cannot_send_credentials_to_another_origin():
    calls = []

    async def scenario():
        async with AsyncSculptary(
            host="https://x.test",
            token="test-async-credential",
            transport=httpx.MockTransport(
                lambda request: calls.append(request) or httpx.Response(200, json={})
            ),
        ) as client:
            with pytest.raises(ValueError, match="configured"):
                await client.request("GET", "https://other.test/api")

    run(scenario)
    assert not calls


def test_async_download_handles_short_writes_to_a_caller_owned_stream():
    class ShortWriter(io.BytesIO):
        def write(self, data):
            return super().write(data[:3])

    async def scenario():
        async with AsyncSculptary(
            host="https://x.test",
            token="",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"model-bytes")),
        ) as client:
            stream = ShortWriter()
            assert await client.contents.download("alice/shell", "model.glb", stream) == 11
            assert stream.getvalue() == b"model-bytes" and not stream.closed

    run(scenario)


def test_async_failed_upload_closes_local_files(tmp_path):
    path = tmp_path / "model.glb"
    path.write_bytes(b"glTF-fixture")

    async def scenario():
        async with AsyncSculptary(
            host="https://x.test",
            token="",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(403, json={"error": {"message": "Denied"}})
            ),
        ) as client:
            with pytest.raises(PermissionDenied):
                await client.contents.upload(
                    "alice/shell", {"model.glb": path}, message="Adjust", branch="main", expected_head="abc"
                )

    run(scenario)
    path.unlink()


def test_async_invalid_json_is_a_client_error_without_echoing_the_response():
    from sculptary import SculptaryError

    async def scenario():
        async with AsyncSculptary(
            host="https://x.test",
            token="",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=b"<html>upstream response</html>")
            ),
        ) as client:
            with pytest.raises(SculptaryError) as caught:
                await client.users.me()
            assert caught.value.status == 502 and "upstream response" not in str(caught.value)

    run(scenario)
