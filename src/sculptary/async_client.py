"""Asynchronous public API client with the synchronous client's resource surface."""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator, Iterable
from contextlib import ExitStack
from functools import partial
from pathlib import Path
from typing import Any, BinaryIO

import anyio
import httpx
from typing_extensions import Self

from .client import (
    DEFAULT_HOST,
    Asset,
    AuthenticationRequired,
    NotFound,
    PermissionDenied,
    Repository,
    SculptaryError,
    _clean,
)
from .models import AssetComparison, Collection, Comment, GeometryComparison, SpatialAnchor, Visibility


def _check_status(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    try:
        payload = response.json()
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        error = error if isinstance(error, dict) else {}
    except ValueError:
        error = {}
    cls = {401: AuthenticationRequired, 403: PermissionDenied, 404: NotFound}.get(
        response.status_code, SculptaryError
    )
    raise cls(
        response.status_code,
        error.get("code", "error"),
        error.get("message") or response.reason_phrase,
        error.get("field"),
    )


def _write_all(dest: BinaryIO, chunk: bytes) -> None:
    remaining = memoryview(chunk)
    while remaining:
        written = dest.write(remaining)
        if not isinstance(written, int) or not 0 < written <= len(remaining):
            raise OSError("The destination stream did not accept the downloaded bytes.")
        remaining = remaining[written:]


async def _write(chunks: AsyncIterator[bytes], dest: str | os.PathLike | BinaryIO) -> int:
    count = 0
    if hasattr(dest, "write"):
        async for chunk in chunks:
            await anyio.to_thread.run_sync(_write_all, dest, chunk)
            count += len(chunk)
        return count
    target = Path(dest)
    handle = await anyio.to_thread.run_sync(
        partial(
            tempfile.NamedTemporaryFile,
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".part",
            delete=False,
        )
    )
    temporary = Path(handle.name)
    try:
        async with anyio.wrap_file(handle) as output:
            async for chunk in chunks:
                await output.write(chunk)
                count += len(chunk)
        await anyio.to_thread.run_sync(temporary.replace, target)
        return count
    finally:
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(handle.close)
            await anyio.to_thread.run_sync(partial(temporary.unlink, missing_ok=True))


class AsyncSculptary:
    """Use with ``async with`` and await resource methods, for example ``users.me()``."""

    def __init__(
        self,
        token: str | None = None,
        host: str | None = None,
        *,
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        host = (host or os.environ.get("SCULPTARY_HOST") or DEFAULT_HOST).rstrip("/")
        token = token if token is not None else os.environ.get("SCULPTARY_TOKEN")
        headers = {"User-Agent": "sculptary-python/0.3.0", "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.host = host
        self._http = httpx.AsyncClient(
            base_url=f"{host}/api/v1", headers=headers, timeout=timeout, transport=transport
        )
        self.users = AsyncUsers(self)
        self.repos = AsyncRepos(self)
        self.contents = AsyncContents(self)
        self.assets = AsyncAssets(self)
        self.releases = AsyncReleases(self)
        self.pulls = AsyncPulls(self)
        self.issues = AsyncIssues(self)
        self.collections = AsyncCollections(self)

    async def close(self) -> None:
        with anyio.CancelScope(shield=True):
            await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def request(self, method: str, path: str, **kw: Any) -> Any:
        url, base = self._http.base_url.join(path), self._http.base_url
        if (url.scheme, url.host, url.port) != (base.scheme, base.host, base.port):
            raise ValueError("Requests must use the configured Sculptary origin.")
        response = await self._http.request(method, path, **kw)
        _check_status(response)
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise SculptaryError(502, "invalid_response", "The server did not return valid JSON.") from None

    async def search(self, q: str = "", **filters: Any) -> list[Repository]:
        data = await self.request("GET", "/search/repositories", params={"q": q, **filters})
        return [Repository.from_api(repo) for repo in data["items"]]


class _AsyncResource:
    def __init__(self, client: AsyncSculptary):
        self._c = client


class AsyncUsers(_AsyncResource):
    async def me(self) -> dict[str, Any]:
        return await self._c.request("GET", "/user")

    async def get(self, username: str) -> dict[str, Any]:
        return await self._c.request("GET", f"/users/{username}")


class AsyncRepos(_AsyncResource):
    async def get(self, full_name: str) -> Repository:
        return Repository.from_api(await self._c.request("GET", f"/repos/{full_name}"))

    async def list_for(self, owner: str) -> list[Repository]:
        return [Repository.from_api(r) for r in await self._c.request("GET", f"/users/{owner}/repos")]

    async def mine(self) -> list[Repository]:
        return [Repository.from_api(r) for r in await self._c.request("GET", "/user/repos")]

    async def create(
        self,
        name: str,
        *,
        owner: str | None = None,
        private: bool = False,
        description: str = "",
        license: str = "",
        topics: Iterable[str] = (),
        initialize_readme: bool = False,
    ) -> Repository:
        body = {
            "name": name,
            "owner": owner,
            "visibility": "private" if private else "public",
            "description": description,
            "license": license,
            "topics": list(topics),
            "initialize_readme": initialize_readme,
        }
        return Repository.from_api(await self._c.request("POST", "/repos", json=body))

    async def update(self, full_name: str, **fields: Any) -> Repository:
        return Repository.from_api(await self._c.request("PATCH", f"/repos/{full_name}", json=fields))

    async def delete(self, full_name: str) -> None:
        await self._c.request("DELETE", f"/repos/{full_name}")

    async def fork(self, full_name: str, *, owner: str | None = None, name: str | None = None) -> Repository:
        return Repository.from_api(
            await self._c.request("POST", f"/repos/{full_name}/forks", json={"owner": owner, "name": name})
        )

    async def branches(self, full_name: str) -> list[dict[str, Any]]:
        return await self._c.request("GET", f"/repos/{full_name}/branches")

    async def commits(
        self, full_name: str, ref: str | None = None, path: str | None = None, page: int = 1
    ) -> list[dict[str, Any]]:
        return (
            await self._c.request(
                "GET", f"/repos/{full_name}/commits", params=_clean(ref=ref, path=path, page=page)
            )
        )["commits"]

    async def compare(self, full_name: str, base: str, head: str) -> dict[str, Any]:
        return await self._c.request(
            "GET", f"/repos/{full_name}/compare", params={"base": base, "head": head}
        )

    async def star(self, full_name: str) -> None:
        await self._c.request("PUT", f"/user/starred/{full_name}")

    async def unstar(self, full_name: str) -> None:
        await self._c.request("DELETE", f"/user/starred/{full_name}")


class AsyncCollections(_AsyncResource):
    """Curated repositories owned by a user or organization (owner/slug)."""

    async def list_for(self, owner: str) -> list[Collection]:
        """List all visible collections for a user or organization; no pagination."""
        return await self._c.request("GET", f"/users/{owner}/collections")

    async def get(self, full_name: str) -> Collection:
        """Get owner/slug with visible items, notes and positions."""
        return await self._c.request("GET", f"/collections/{full_name}")

    async def create(
        self,
        name: str,
        *,
        owner: str | None = None,
        slug: str | None = None,
        description: str = "",
        visibility: Visibility = "public",
    ) -> Collection:
        """Create a collection. Omit owner to use the signed-in user's namespace."""
        body = {
            "name": name,
            "owner": owner,
            "slug": slug,
            "description": description,
            "visibility": visibility,
        }
        return await self._c.request("POST", "/collections", json=body)

    async def update(
        self,
        full_name: str,
        *,
        name: str | None = None,
        slug: str | None = None,
        description: str | None = None,
        visibility: Visibility | None = None,
    ) -> Collection:
        """Update supplied fields; an empty description clears it."""
        fields = {"name": name, "slug": slug, "description": description, "visibility": visibility}
        body = {k: v for k, v in fields.items() if v is not None}
        return await self._c.request("PATCH", f"/collections/{full_name}", json=body)

    async def delete(self, full_name: str) -> None:
        """Delete a collection, leaving its repositories intact."""
        await self._c.request("DELETE", f"/collections/{full_name}")

    async def add_item(self, full_name: str, repository: str, *, note: str | None = None) -> Collection:
        """Add owner/repo, or update its note. None preserves an existing note; '' clears it."""
        return await self._c.request(
            "PUT", f"/collections/{full_name}/items/{repository}", json={"note": note}
        )

    async def remove_item(self, full_name: str, repository: str) -> None:
        """Remove a repository from the collection."""
        await self._c.request("DELETE", f"/collections/{full_name}/items/{repository}")

    async def reorder(self, full_name: str, full_names: Iterable[str]) -> Collection:
        """Supply every visible repository's full name exactly once, in the desired order."""
        return await self._c.request(
            "POST", f"/collections/{full_name}/order", json={"full_names": list(full_names)}
        )

    async def containing(self, repository: str) -> list[Collection]:
        """List visible collections containing owner/repo; no pagination."""
        return await self._c.request("GET", f"/repos/{repository}/collections")


class AsyncContents(_AsyncResource):
    async def download(
        self, full_name: str, path: str, dest: str | os.PathLike | BinaryIO, ref: str | None = None
    ) -> int:
        """Stream a file, resolving LFS redirects. Path destinations are replaced only on success."""
        async with self._c._http.stream(
            "GET",
            f"/repos/{full_name}/raw",
            params=_clean(path=path, ref=ref, download="true"),
            follow_redirects=True,
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                _check_status(response)
            return await _write(response.aiter_bytes(chunk_size=1024 * 1024), dest)

    async def upload(
        self,
        full_name: str,
        files: dict[str, str | os.PathLike | bytes],
        *,
        message: str,
        branch: str | None = None,
        expected_head: str | None = None,
        directory: str = "",
        new_branch: str | None = None,
    ) -> dict[str, Any]:
        """Commit files in one request without loading local files entirely into memory."""
        if branch is None:
            branch = (await self._c.repos.get(full_name)).default_branch
        if expected_head is None:
            heads = {item["name"]: item["commit"] for item in await self._c.repos.branches(full_name)}
            expected_head = heads.get(branch, "")
        stack = ExitStack()
        try:
            parts = []
            for repo_path, source in files.items():
                data = (
                    source
                    if isinstance(source, bytes)
                    else stack.enter_context(await anyio.to_thread.run_sync(Path(source).open, "rb"))
                )
                parts.append(("files", (repo_path.rsplit("/", 1)[-1], data, "application/octet-stream")))
            form = _clean(
                branch=branch,
                message=message,
                expected_head=expected_head,
                directory=directory,
                new_branch=new_branch,
            )
            form["paths"] = list(files)
            return await self._c.request(
                "POST", f"/repos/{full_name}/contents", data=form, files=parts, timeout=600
            )
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(stack.close)

    async def tree(self, full_name: str, path: str = "", ref: str | None = None) -> list[dict[str, Any]]:
        return (await self._c.request("GET", f"/repos/{full_name}/tree", params=_clean(path=path, ref=ref)))[
            "entries"
        ]

    async def read_text(self, full_name: str, path: str, ref: str | None = None) -> str:
        blob = await self._c.request("GET", f"/repos/{full_name}/blob", params=_clean(path=path, ref=ref))
        if blob["content"] is None:
            raise ValueError(f"{path} is binary; use download()")
        return blob["content"]

    async def commit(
        self,
        full_name: str,
        operations: list[dict[str, Any]],
        *,
        message: str,
        branch: str,
        expected_head: str | None,
    ) -> dict[str, Any]:
        """Text edits, deletes and moves: operations like {"op": "put", "path": ..., "content": ...}."""
        body = {
            "branch": branch,
            "message": message,
            "expected_head": expected_head,
            "operations": operations,
        }
        return await self._c.request("POST", f"/repos/{full_name}/commits", json=body)


class AsyncAssets(_AsyncResource):
    async def download_preview(self, full_name: str, asset: Asset, dest: str | os.PathLike | BinaryIO) -> int:
        async with self._c._http.stream(
            "GET", f"/repos/{full_name}/assets/{asset.id}/preview", follow_redirects=True
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                _check_status(response)
            return await _write(response.aiter_bytes(chunk_size=1024 * 1024), dest)

    async def list(self, full_name: str, ref: str | None = None) -> list[Asset]:
        data = await self._c.request("GET", f"/repos/{full_name}/assets", params=_clean(ref=ref))
        return [Asset.from_api(a) for a in data["assets"]]

    async def get(self, full_name: str, path: str, ref: str | None = None) -> Asset:
        return Asset.from_api(
            await self._c.request("GET", f"/repos/{full_name}/asset", params=_clean(path=path, ref=ref))
        )

    async def compare(
        self, full_name: str, path: str, base: str, head: str, *, base_path: str | None = None
    ) -> AssetComparison:
        """Compare model metadata between refs; base_path supports renamed files.

        Returns the wire dictionary, including nullable sides and numeric deltas.
        """
        return await self._c.request(
            "GET",
            f"/repos/{full_name}/asset-compare",
            params=_clean(path=path, base=base, head=head, base_path=base_path),
        )

    async def geometry_compare(
        self, full_name: str, path: str, base: str, head: str, *, base_path: str | None = None
    ) -> GeometryComparison:
        """Get or start a surface deviation comparison, including heatmap scale and URL.

        Call again for waiting/pending/running results. Hausdorff is the largest
        change; method, confidence and resolution describe its accuracy. The
        heatmap is a GLB at heatmap_url, not an inline array.
        """
        return await self._c.request(
            "GET",
            f"/repos/{full_name}/geometry-compare",
            params=_clean(path=path, base=base, head=head, base_path=base_path),
        )

    async def formats(self) -> list[dict[str, Any]]:
        return await self._c.request("GET", "/formats")


class AsyncReleases(_AsyncResource):
    async def list(self, full_name: str) -> list[dict[str, Any]]:
        return await self._c.request("GET", f"/repos/{full_name}/releases")

    async def create(
        self,
        full_name: str,
        tag: str,
        *,
        name: str = "",
        body: str = "",
        target: str | None = None,
        prerelease: bool = False,
        draft: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "tag_name": tag,
            "name": name,
            "body": body,
            "target": target,
            "prerelease": prerelease,
            "draft": draft,
        }
        return await self._c.request("POST", f"/repos/{full_name}/releases", json=payload)


class AsyncPulls(_AsyncResource):
    async def comments(self, full_name: str, number: int) -> list[Comment]:
        """List all PR comments, including spatial anchors, via PR detail (no pagination)."""
        return (await self._c.request("GET", f"/repos/{full_name}/pulls/{number}"))["comments"]

    async def comment(
        self,
        full_name: str,
        number: int,
        body: str,
        *,
        path: str | None = None,
        spatial_anchor: SpatialAnchor | None = None,
        line: int | None = None,
        commit_sha: str | None = None,
        parent_id: str | None = None,
    ) -> Comment:
        """Create a comment. Model anchors use point/normal/camera; path identifies the asset."""
        payload = {
            "body": body,
            "path": path,
            "line": line,
            "commit_sha": commit_sha,
            "spatial_anchor": spatial_anchor,
        }
        if parent_id is not None:
            payload = {"body": body, "parent_id": parent_id}
        return await self._c.request("POST", f"/repos/{full_name}/pulls/{number}/comments", json=payload)

    async def reply(self, full_name: str, number: int, comment_id: str, body: str) -> Comment:
        """Reply to a root comment. The API validates the parent and thread depth."""
        return await self.comment(full_name, number, body, parent_id=comment_id)

    async def resolve(self, full_name: str, comment_id: str) -> Comment:
        """Resolve a thread as its author, the PR author or a repository writer."""
        return await self._c.request("POST", f"/repos/{full_name}/comments/{comment_id}/resolve", json={})

    async def unresolve(self, full_name: str, comment_id: str) -> Comment:
        """Reopen a resolved thread."""
        return await self._c.request("POST", f"/repos/{full_name}/comments/{comment_id}/unresolve", json={})

    async def list(self, full_name: str, state: str = "open") -> list[dict[str, Any]]:
        return (await self._c.request("GET", f"/repos/{full_name}/pulls", params={"state": state}))["items"]

    async def create(
        self,
        full_name: str,
        *,
        title: str,
        source_branch: str,
        target_branch: str | None = None,
        body: str = "",
        source_repository: str | None = None,
    ) -> dict[str, Any]:
        payload = _clean(
            title=title,
            body=body,
            source_branch=source_branch,
            target_branch=target_branch,
            source_repository=source_repository,
        )
        return await self._c.request("POST", f"/repos/{full_name}/pulls", json=payload)

    async def merge(self, full_name: str, number: int, strategy: str = "merge") -> dict[str, Any]:
        return await self._c.request(
            "POST", f"/repos/{full_name}/pulls/{number}/merge", json={"strategy": strategy}
        )


class AsyncIssues(_AsyncResource):
    async def list(self, full_name: str, state: str = "open") -> list[dict[str, Any]]:
        return (await self._c.request("GET", f"/repos/{full_name}/issues", params={"state": state}))["items"]

    async def create(self, full_name: str, title: str, body: str = "") -> dict[str, Any]:
        return await self._c.request(
            "POST", f"/repos/{full_name}/issues", json={"title": title, "body": body}
        )
