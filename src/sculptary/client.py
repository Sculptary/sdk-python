"""Client for the public Sculptary API (`/api/v1`)."""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

import httpx
from typing_extensions import Self

from .models import AssetComparison, Collection, Comment, GeometryComparison, SpatialAnchor, Visibility

DEFAULT_HOST = "https://sculptary.com"


class SculptaryError(Exception):
    """An error response from the API."""

    def __init__(self, status: int, code: str, message: str, field: str | None = None):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.field = field


class NotFound(SculptaryError):
    pass


class PermissionDenied(SculptaryError):
    pass


class AuthenticationRequired(SculptaryError):
    pass


@dataclass
class Repository:
    """A repository as returned by the API. Unknown fields are kept in `raw`."""

    id: str
    full_name: str
    name: str
    owner: str
    description: str
    visibility: str
    default_branch: str
    clone_http: str
    clone_ssh: str
    topics: list[str]
    formats: list[str]
    star_count: int
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> Repository:
        return cls(
            id=d["id"], full_name=d["full_name"], name=d["name"], owner=d["owner"]["slug"], description=d.get("description", ""),
            visibility=d["visibility"], default_branch=d["default_branch"], clone_http=d["clone_http"],
            clone_ssh=d["clone_ssh"], topics=d.get("topics", []), formats=d.get("formats", []),
            star_count=d.get("star_count", 0), raw=d,
        )


@dataclass
class Asset:
    """A model file at an immutable commit, with geometry metadata once processed."""

    id: str
    path: str
    commit: str
    format: str
    status: str
    metadata: dict[str, Any]
    warnings: list[str]
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> Asset:
        return cls(
            id=d["id"], path=d["path"], commit=d["commit"], format=d["format"], status=d["status"],
            metadata=d.get("metadata") or {}, warnings=d.get("warnings") or [], raw=d,
        )

    @property
    def triangles(self) -> int | None:
        return self.metadata.get("triangle_count")


class Sculptary:
    """Synchronous API client.

    >>> sdb = Sculptary(token="sdb_pat_...")            # or SCULPTARY_TOKEN
    >>> repo = sdb.repos.get("northwind/medieval-castle")
    >>> [a.path for a in sdb.assets.list(repo.full_name)]
    """

    def __init__(self, token: str | None = None, host: str | None = None, *, timeout: float = 60.0,
                 transport: httpx.BaseTransport | None = None):
        host = (host or os.environ.get("SCULPTARY_HOST") or DEFAULT_HOST).rstrip("/")
        token = token if token is not None else os.environ.get("SCULPTARY_TOKEN")
        headers = {"User-Agent": "sculptary-python/0.2.0", "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.host = host
        self._http = httpx.Client(base_url=f"{host}/api/v1", headers=headers, timeout=timeout, transport=transport)
        self.users = Users(self)
        self.repos = Repos(self)
        self.contents = Contents(self)
        self.assets = Assets(self)
        self.releases = Releases(self)
        self.pulls = Pulls(self)
        self.issues = Issues(self)
        self.collections = Collections(self)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def request(self, method: str, path: str, **kw: Any) -> Any:
        r = self._http.request(method, path, **kw)
        if r.status_code >= 400:
            try:
                err = r.json().get("error", {})
            except ValueError:
                err = {}
            cls = {401: AuthenticationRequired, 403: PermissionDenied, 404: NotFound}.get(r.status_code, SculptaryError)
            raise cls(r.status_code, err.get("code", "error"), err.get("message", r.text[:200]), err.get("field"))
        if r.status_code == 204 or not r.content:
            return None
        return r.json()

    def search(self, q: str = "", **filters: Any) -> list[Repository]:
        """Search public (and, with a token, your accessible) repositories."""
        data = self.request("GET", "/search/repositories", params={"q": q, **filters})
        return [Repository.from_api(r) for r in data["items"]]


class _Resource:
    def __init__(self, client: Sculptary):
        self._c = client


class Users(_Resource):
    def me(self) -> dict[str, Any]:
        return self._c.request("GET", "/user")

    def get(self, username: str) -> dict[str, Any]:
        return self._c.request("GET", f"/users/{username}")


class Repos(_Resource):
    def get(self, full_name: str) -> Repository:
        return Repository.from_api(self._c.request("GET", f"/repos/{full_name}"))

    def list_for(self, owner: str) -> list[Repository]:
        return [Repository.from_api(r) for r in self._c.request("GET", f"/users/{owner}/repos")]

    def mine(self) -> list[Repository]:
        return [Repository.from_api(r) for r in self._c.request("GET", "/user/repos")]

    def create(self, name: str, *, owner: str | None = None, private: bool = False, description: str = "",
               license: str = "", topics: Iterable[str] = (), initialize_readme: bool = False) -> Repository:
        body = {"name": name, "owner": owner, "visibility": "private" if private else "public", "description": description,
                "license": license, "topics": list(topics), "initialize_readme": initialize_readme}
        return Repository.from_api(self._c.request("POST", "/repos", json=body))

    def update(self, full_name: str, **fields: Any) -> Repository:
        return Repository.from_api(self._c.request("PATCH", f"/repos/{full_name}", json=fields))

    def delete(self, full_name: str) -> None:
        self._c.request("DELETE", f"/repos/{full_name}")

    def fork(self, full_name: str, *, owner: str | None = None, name: str | None = None) -> Repository:
        return Repository.from_api(self._c.request("POST", f"/repos/{full_name}/forks", json={"owner": owner, "name": name}))

    def branches(self, full_name: str) -> list[dict[str, Any]]:
        return self._c.request("GET", f"/repos/{full_name}/branches")

    def commits(self, full_name: str, ref: str | None = None, path: str | None = None, page: int = 1) -> list[dict[str, Any]]:
        return self._c.request("GET", f"/repos/{full_name}/commits", params=_clean(ref=ref, path=path, page=page))["commits"]

    def compare(self, full_name: str, base: str, head: str) -> dict[str, Any]:
        return self._c.request("GET", f"/repos/{full_name}/compare", params={"base": base, "head": head})

    def star(self, full_name: str) -> None:
        self._c.request("PUT", f"/user/starred/{full_name}")

    def unstar(self, full_name: str) -> None:
        self._c.request("DELETE", f"/user/starred/{full_name}")


class Collections(_Resource):
    """Curated repositories owned by a user or organization (owner/slug)."""

    def list_for(self, owner: str) -> list[Collection]:
        """List all visible collections for a user or organization; no pagination."""
        return self._c.request("GET", f"/users/{owner}/collections")

    def get(self, full_name: str) -> Collection:
        """Get owner/slug with visible items, notes and positions."""
        return self._c.request("GET", f"/collections/{full_name}")

    def create(self, name: str, *, owner: str | None = None, slug: str | None = None,
               description: str = "", visibility: Visibility = "public") -> Collection:
        """Create a collection. Omit owner to use the signed-in user's namespace."""
        body = {"name": name, "owner": owner, "slug": slug, "description": description, "visibility": visibility}
        return self._c.request("POST", "/collections", json=body)

    def update(self, full_name: str, *, name: str | None = None, slug: str | None = None,
               description: str | None = None, visibility: Visibility | None = None) -> Collection:
        """Update supplied fields; an empty description clears it."""
        fields = {"name": name, "slug": slug, "description": description, "visibility": visibility}
        body = {k: v for k, v in fields.items() if v is not None}
        return self._c.request("PATCH", f"/collections/{full_name}", json=body)

    def delete(self, full_name: str) -> None:
        """Delete a collection, leaving its repositories intact."""
        self._c.request("DELETE", f"/collections/{full_name}")

    def add_item(self, full_name: str, repository: str, *, note: str | None = None) -> Collection:
        """Add owner/repo, or update its note. None preserves an existing note; '' clears it."""
        return self._c.request("PUT", f"/collections/{full_name}/items/{repository}", json={"note": note})

    def remove_item(self, full_name: str, repository: str) -> None:
        """Remove a repository from the collection."""
        self._c.request("DELETE", f"/collections/{full_name}/items/{repository}")

    def reorder(self, full_name: str, full_names: Iterable[str]) -> Collection:
        """Supply every visible repository's full name exactly once, in the desired order."""
        return self._c.request("POST", f"/collections/{full_name}/order", json={"full_names": list(full_names)})

    def containing(self, repository: str) -> list[Collection]:
        """List visible collections containing owner/repo; no pagination."""
        return self._c.request("GET", f"/repos/{repository}/collections")


class Contents(_Resource):
    def tree(self, full_name: str, path: str = "", ref: str | None = None) -> list[dict[str, Any]]:
        return self._c.request("GET", f"/repos/{full_name}/tree", params=_clean(path=path, ref=ref))["entries"]

    def read_text(self, full_name: str, path: str, ref: str | None = None) -> str:
        blob = self._c.request("GET", f"/repos/{full_name}/blob", params=_clean(path=path, ref=ref))
        if blob["content"] is None:
            raise ValueError(f"{path} is binary; use download()")
        return blob["content"]

    def download(self, full_name: str, path: str, dest: str | os.PathLike | BinaryIO, ref: str | None = None) -> int:
        """Download a file (LFS content is resolved transparently). Returns bytes written."""
        params = _clean(path=path, ref=ref, download="true")
        with self._c._http.stream("GET", f"/repos/{full_name}/raw", params=params, follow_redirects=True) as r:
            if r.status_code >= 400:
                r.read()
                self._c.request("GET", f"/repos/{full_name}/blob", params=_clean(path=path, ref=ref))
                raise SculptaryError(r.status_code, "error", "download failed")
            return _write(r.iter_bytes(), dest)

    def upload(self, full_name: str, files: dict[str, str | os.PathLike | bytes], *, message: str, branch: str | None = None,
               expected_head: str | None = None, directory: str = "", new_branch: str | None = None) -> dict[str, Any]:
        """Commit files (repository path -> local path or bytes) in one commit. Model files go to Git LFS."""
        if branch is None:
            branch = self._c.repos.get(full_name).default_branch
        if expected_head is None:
            heads = {b["name"]: b["commit"] for b in self._c.repos.branches(full_name)}
            expected_head = heads.get(branch, "")
        parts = []
        for repo_path, src in files.items():
            data = src if isinstance(src, bytes) else Path(src).read_bytes()
            parts.append(("files", (repo_path.rsplit("/", 1)[-1], data, "application/octet-stream")))
        form: dict[str, Any] = _clean(branch=branch, message=message, expected_head=expected_head, directory=directory, new_branch=new_branch)
        form["paths"] = list(files)
        return self._c.request("POST", f"/repos/{full_name}/contents", data=form, files=parts, timeout=600)

    def commit(self, full_name: str, operations: list[dict[str, Any]], *, message: str, branch: str,
               expected_head: str | None) -> dict[str, Any]:
        """Text edits, deletes and moves: operations like {"op": "put", "path": ..., "content": ...}."""
        body = {"branch": branch, "message": message, "expected_head": expected_head, "operations": operations}
        return self._c.request("POST", f"/repos/{full_name}/commits", json=body)


class Assets(_Resource):
    def list(self, full_name: str, ref: str | None = None) -> list[Asset]:
        data = self._c.request("GET", f"/repos/{full_name}/assets", params=_clean(ref=ref))
        return [Asset.from_api(a) for a in data["assets"]]

    def get(self, full_name: str, path: str, ref: str | None = None) -> Asset:
        return Asset.from_api(self._c.request("GET", f"/repos/{full_name}/asset", params=_clean(path=path, ref=ref)))

    def compare(self, full_name: str, path: str, base: str, head: str, *,
                base_path: str | None = None) -> AssetComparison:
        """Compare model metadata between refs; base_path supports renamed files.

        Returns the wire dictionary, including nullable sides and numeric deltas.
        """
        return self._c.request("GET", f"/repos/{full_name}/asset-compare",
                               params=_clean(path=path, base=base, head=head, base_path=base_path))

    def geometry_compare(self, full_name: str, path: str, base: str, head: str, *,
                         base_path: str | None = None) -> GeometryComparison:
        """Get or start a surface deviation comparison, including heatmap scale and URL.

        Call again for waiting/pending/running results. Hausdorff is the largest
        change; method, confidence and resolution describe its accuracy. The
        heatmap is a GLB at heatmap_url, not an inline array.
        """
        return self._c.request("GET", f"/repos/{full_name}/geometry-compare",
                               params=_clean(path=path, base=base, head=head, base_path=base_path))

    def download_preview(self, full_name: str, asset: Asset, dest: str | os.PathLike | BinaryIO) -> int:
        with self._c._http.stream("GET", f"/repos/{full_name}/assets/{asset.id}/preview", follow_redirects=True) as r:
            if r.status_code >= 400:
                raise SculptaryError(r.status_code, "error", "preview not available")
            return _write(r.iter_bytes(), dest)

    def formats(self) -> list[dict[str, Any]]:
        return self._c.request("GET", "/formats")


class Releases(_Resource):
    def list(self, full_name: str) -> list[dict[str, Any]]:
        return self._c.request("GET", f"/repos/{full_name}/releases")

    def create(self, full_name: str, tag: str, *, name: str = "", body: str = "", target: str | None = None,
               prerelease: bool = False, draft: bool = False) -> dict[str, Any]:
        payload = {"tag_name": tag, "name": name, "body": body, "target": target, "prerelease": prerelease, "draft": draft}
        return self._c.request("POST", f"/repos/{full_name}/releases", json=payload)


class Pulls(_Resource):
    def comments(self, full_name: str, number: int) -> list[Comment]:
        """List all PR comments, including spatial anchors, via PR detail (no pagination)."""
        return self._c.request("GET", f"/repos/{full_name}/pulls/{number}")["comments"]

    def comment(self, full_name: str, number: int, body: str, *, path: str | None = None,
                spatial_anchor: SpatialAnchor | None = None, line: int | None = None,
                commit_sha: str | None = None) -> Comment:
        """Create a comment. Model anchors use point/normal/camera; path identifies the asset."""
        payload = {"body": body, "path": path, "line": line, "commit_sha": commit_sha,
                   "spatial_anchor": spatial_anchor}
        return self._c.request("POST", f"/repos/{full_name}/pulls/{number}/comments", json=payload)

    def reply(self, full_name: str, number: int, comment_id: str, body: str) -> Comment:
        """Quote a comment and preserve its anchor, path, line and commit.

        The API has no threads or parent ID: this creates an ordinary PR comment
        with a Markdown quote of the source body, followed by the reply.
        """
        parent = next((c for c in self.comments(full_name, number) if c["id"] == comment_id), None)
        if parent is None:
            raise NotFound(404, "not_found", "Comment not found.")
        quote = "\n".join(f"> {line}" for line in parent["body"].splitlines())
        return self.comment(full_name, number, f"{quote}\n\n{body}", path=parent.get("path"),
                            line=parent.get("line"), commit_sha=parent.get("commit_sha"),
                            spatial_anchor=parent.get("spatial_anchor"))

    def list(self, full_name: str, state: str = "open") -> list[dict[str, Any]]:
        return self._c.request("GET", f"/repos/{full_name}/pulls", params={"state": state})["items"]

    def create(self, full_name: str, *, title: str, source_branch: str, target_branch: str | None = None, body: str = "",
               source_repository: str | None = None) -> dict[str, Any]:
        payload = _clean(title=title, body=body, source_branch=source_branch, target_branch=target_branch, source_repository=source_repository)
        return self._c.request("POST", f"/repos/{full_name}/pulls", json=payload)

    def merge(self, full_name: str, number: int, strategy: str = "merge") -> dict[str, Any]:
        return self._c.request("POST", f"/repos/{full_name}/pulls/{number}/merge", json={"strategy": strategy})


class Issues(_Resource):
    def list(self, full_name: str, state: str = "open") -> list[dict[str, Any]]:
        return self._c.request("GET", f"/repos/{full_name}/issues", params={"state": state})["items"]

    def create(self, full_name: str, title: str, body: str = "") -> dict[str, Any]:
        return self._c.request("POST", f"/repos/{full_name}/issues", json={"title": title, "body": body})


def _clean(**kw: Any) -> dict[str, Any]:
    return {k: v for k, v in kw.items() if v is not None and v != ""}


def _write(chunks: Iterator[bytes], dest: str | os.PathLike | BinaryIO) -> int:
    n = 0
    if hasattr(dest, "write"):
        for c in chunks:
            dest.write(c)  # type: ignore[union-attr]
            n += len(c)
        return n
    with open(dest, "wb") as f:  # type: ignore[arg-type]
        for c in chunks:
            f.write(c)
            n += len(c)
    return n
