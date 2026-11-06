import json
import os

import httpx
import pytest

from shapesdb import NotFound, PermissionDenied, ShapesDB

REPO = {
    "id": "r1", "full_name": "alice/castle", "name": "castle", "owner": {"slug": "alice"}, "description": "A castle",
    "visibility": "public", "default_branch": "main", "clone_http": "https://x/alice/castle.git",
    "clone_ssh": "git@x:alice/castle.git", "topics": ["medieval"], "formats": ["glb"], "star_count": 3,
}


def client(handler) -> ShapesDB:
    return ShapesDB(token="sdb_pat_test", host="https://example.test", transport=httpx.MockTransport(handler))


def test_auth_header_and_repo_parsing():
    seen = {}

    def h(req: httpx.Request):
        seen["auth"] = req.headers.get("authorization")
        seen["path"] = req.url.path
        return httpx.Response(200, json=REPO)

    repo = client(h).repos.get("alice/castle")
    assert seen == {"auth": "Bearer sdb_pat_test", "path": "/api/v1/repos/alice/castle"}
    assert repo.full_name == "alice/castle" and repo.owner == "alice" and repo.formats == ["glb"]


def test_errors_map_to_exceptions():
    def h(req):
        status = 404 if "missing" in req.url.path else 403
        return httpx.Response(status, json={"error": {"code": "x", "message": "nope", "field": "name"}})

    c = client(h)
    with pytest.raises(NotFound):
        c.repos.get("a/missing")
    with pytest.raises(PermissionDenied) as e:
        c.repos.delete("a/b")
    assert e.value.field == "name" and e.value.message == "nope"


def test_create_and_assets():
    def h(req: httpx.Request):
        if req.method == "POST":
            body = json.loads(req.content)
            assert body["visibility"] == "private" and body["topics"] == ["a"]
            return httpx.Response(201, json=REPO)
        return httpx.Response(200, json={"commit": "c", "assets": [{"id": "1", "path": "m.glb", "commit": "c", "format": "glb",
                                                                     "status": "ready", "metadata": {"triangle_count": 12}, "warnings": []}]})

    c = client(h)
    assert c.repos.create("castle", private=True, topics=["a"]).name == "castle"
    assets = c.assets.list("alice/castle")
    assert assets[0].triangles == 12


def test_upload_is_multipart_with_head():
    calls = []

    def h(req: httpx.Request):
        calls.append(req)
        if req.url.path.endswith("/branches"):
            return httpx.Response(200, json=[{"name": "main", "commit": "abc", "protected": False, "default": True}])
        return httpx.Response(200, json={"commit": "def"})

    out = client(h).contents.upload("alice/castle", {"models/a.stl": b"solid a\nendsolid a\n"}, message="Add", branch="main")
    assert out["commit"] == "def"
    upload = calls[-1]
    assert upload.headers["content-type"].startswith("multipart/form-data")
    assert b'name="expected_head"' in upload.content and b"abc" in upload.content and b'filename="a.stl"' in upload.content and b"models/a.stl" in upload.content


@pytest.mark.skipif(not os.environ.get("SHAPESDB_TEST_HOST"), reason="set SHAPESDB_TEST_HOST to run against a live server")
def test_live_public_read():
    with ShapesDB(token="", host=os.environ["SHAPESDB_TEST_HOST"]) as c:
        results = c.search("castle")
        assert all(r.visibility == "public" for r in results)
