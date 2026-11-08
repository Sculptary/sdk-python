import json

import httpx
import pytest

from sculptary import AuthenticationRequired, NotFound, PermissionDenied, Sculptary, SculptaryError

OWNER = {"slug": "studio", "kind": "organization", "display_name": "Studio", "avatar_url": ""}
COLLECTION = {
    "id": "collection-1", "owner": OWNER, "slug": "kits", "name": "Kits", "description": "Reusable models",
    "visibility": "private", "created_by": "user-1", "created_at": "2026-11-01T00:00:00Z",
    "updated_at": "2026-11-02T00:00:00Z", "item_count": 1, "viewer_can_manage": True,
    "items": [{"repository": {"full_name": "alice/castle"}, "note": "Use the tower", "position": 0}],
    "future_field": "retained",
}
ANCHOR = {
    "point": [1, -2, 3], "normal": [0, 1, 0], "object": "Tower",
    "camera": {"position": [4, 5, 6], "target": [1, 2, 3], "zoom": 1.5},
    "future_field": True,
}
COMMENT = {
    "id": "comment-1", "author": {"username": "alice", "display_name": "Alice", "avatar_url": ""},
    "body": "Check this\nedge", "body_html": "<p>Check this</p>", "path": "models/castle.glb", "line": None,
    "commit_sha": "abc", "spatial_anchor": ANCHOR, "review_id": None,
    "created_at": "2026-11-01T00:00:00Z", "updated_at": "2026-11-01T00:00:00Z",
}


def client(handler):
    return Sculptary(token="sdb_pat_test", host="https://example.test/", transport=httpx.MockTransport(handler))


def test_collection_operations():
    expected = [
        ("GET", "/users/alice/collections", None, [COLLECTION]),
        ("GET", "/users/studio/collections", None, [COLLECTION]),
        ("GET", "/collections/studio/kits", None, COLLECTION),
        ("POST", "/collections", {"name": "Kits", "owner": "studio", "slug": "kits",
                                  "description": "Reusable models", "visibility": "private"}, COLLECTION),
        ("PATCH", "/collections/studio/kits", {"name": "New kits", "slug": "new", "description": "",
                                               "visibility": "public"}, COLLECTION),
        ("PUT", "/collections/studio/kits/items/alice/castle", {"note": "Use the tower"}, COLLECTION),
        ("PUT", "/collections/studio/kits/items/alice/castle", {"note": None}, COLLECTION),
        ("PUT", "/collections/studio/kits/items/alice/castle", {"note": ""}, COLLECTION),
        ("POST", "/collections/studio/kits/order", {"full_names": ["alice/castle"]}, COLLECTION),
        ("GET", "/repos/alice/castle/collections", None, [COLLECTION]),
        ("DELETE", "/collections/studio/kits/items/alice/castle", None, None),
        ("DELETE", "/collections/studio/kits", None, None),
    ]

    def h(req):
        method, path, body, response = expected.pop(0)
        assert req.method == method and req.url.path == f"/api/v1{path}"
        assert req.headers["authorization"] == "Bearer sdb_pat_test"
        assert not req.url.query  # These lists are not paginated.
        assert (json.loads(req.content) if req.content else None) == body
        return httpx.Response(204) if response is None else httpx.Response(200, json=response)

    with client(h) as c:
        assert c.collections.list_for("alice") == [COLLECTION]
        assert c.collections.list_for("studio") == [COLLECTION]
        assert c.collections.get("studio/kits")["items"][0]["note"] == "Use the tower"
        assert c.collections.create("Kits", owner="studio", slug="kits", description="Reusable models",
                                    visibility="private")["future_field"] == "retained"
        c.collections.update("studio/kits", name="New kits", slug="new", description="", visibility="public")
        c.collections.add_item("studio/kits", "alice/castle", note="Use the tower")
        c.collections.add_item("studio/kits", "alice/castle")
        c.collections.add_item("studio/kits", "alice/castle", note="")
        c.collections.reorder("studio/kits", iter(["alice/castle"]))
        assert c.collections.containing("alice/castle") == [COLLECTION]
        assert c.collections.remove_item("studio/kits", "alice/castle") is None
        assert c.collections.delete("studio/kits") is None
    assert not expected


def test_collection_defaults_and_partial_updates():
    bodies = []

    def h(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json=COLLECTION)

    with client(h) as c:
        c.collections.create("Kits")
        c.collections.update("studio/kits", description="")
        c.collections.reorder("studio/kits", [])
    assert bodies == [
        {"name": "Kits", "owner": None, "slug": None, "description": "", "visibility": "public"},
        {"description": ""}, {"full_names": []},
    ]


def test_comments_and_spatial_reply():
    calls = []

    def h(req):
        calls.append(req)
        if req.method == "GET":
            assert req.url.path == "/api/v1/repos/alice/castle/pulls/7"
            return httpx.Response(200, json={"comments": [COMMENT, {**COMMENT, "spatial_anchor": None}]})
        assert req.url.path == "/api/v1/repos/alice/castle/pulls/7/comments"
        return httpx.Response(201, json={**COMMENT, **json.loads(req.content)})

    with client(h) as c:
        comments = c.pulls.comments("alice/castle", 7)
        assert comments[0]["spatial_anchor"] == ANCHOR and comments[1]["spatial_anchor"] is None
        comment = c.pulls.comment("alice/castle", 7, "Fix this", path="models/castle.glb",
                                  spatial_anchor=ANCHOR, commit_sha="abc")
        assert comment["spatial_anchor"] == ANCHOR
        reply = c.pulls.reply("alice/castle", 7, "comment-1", "Fixed")
    assert reply["body"] == "Fixed"
    assert json.loads(calls[-1].content) == {"body": "Fixed", "parent_id": "comment-1"}
    assert [req.method for req in calls] == ["GET", "POST", "POST"]
    assert all(req.headers["authorization"] == "Bearer sdb_pat_test" for req in calls)


def test_reply_to_plain_comment_and_missing_comment():
    calls = []

    def h(req):
        calls.append(req)
        if json.loads(req.content)["parent_id"] == "missing":
            return httpx.Response(404, json={"error": {"code": "not_found", "message": "Comment not found."}})
        return httpx.Response(201, json=json.loads(req.content))

    with client(h) as c:
        assert c.pulls.reply("alice/castle", 7, "comment-1", "Fixed")["parent_id"] == "comment-1"
        with pytest.raises(NotFound, match="Comment not found"):
            c.pulls.reply("alice/castle", 7, "missing", "Hello")
    assert [req.method for req in calls] == ["POST", "POST"]


def test_resolve_and_unresolve_threads():
    expected = ["resolve", "unresolve"]

    def h(req):
        action = expected.pop(0)
        assert req.method == "POST"
        assert req.url.path == f"/api/v1/repos/alice/castle/comments/comment-1/{action}"
        assert json.loads(req.content) == {}
        return httpx.Response(200, json={**COMMENT, "resolved_at": "2026-11-08" if action == "resolve" else None,
                                         "replies": [{**COMMENT, "id": "reply-1", "parent_id": "comment-1"}]})

    with client(h) as c:
        assert c.pulls.resolve("alice/castle", "comment-1")["resolved_at"]
        assert c.pulls.unresolve("alice/castle", "comment-1")["replies"][0]["parent_id"] == "comment-1"
    assert not expected


@pytest.mark.parametrize("endpoint", ["compare", "geometry_compare"])
def test_compare_queries_and_heatmap_preservation(endpoint):
    response = {
        "id": "cmp-1", "status": "ready", "heatmap_url": "/api/v1/repos/a/b/comparisons/cmp-1/heatmap",
        "result": {"method": "sampled-surface-nearest-neighbour", "confidence": "medium", "resolution": 0.02,
                   "hausdorff": 0.4, "heatmap": {"scale_min": 0, "scale_max": 0.4, "unit": "model units"},
                   "future_heatmap_samples": [0, 0.2, 0.4]},
    } if endpoint == "geometry_compare" else {
        "base": None, "head": {"path": "models/new #1.glb"}, "status": "added", "diff": None,
    }

    def h(req):
        assert req.method == "GET"
        suffix = "geometry-compare" if endpoint == "geometry_compare" else "asset-compare"
        assert req.url.path == f"/api/v1/repos/alice/castle/{suffix}"
        assert dict(req.url.params) == {"path": "models/new #1.glb", "base": "feature/a+b", "head": "release/2",
                                       "base_path": "models/old &1.glb"}
        return httpx.Response(200, json=response)

    with client(h) as c:
        assert getattr(c.assets, endpoint)("alice/castle", "models/new #1.glb", "feature/a+b", "release/2",
                                            base_path="models/old &1.glb") == response


@pytest.mark.parametrize("status,result", [
    ("waiting", {}), ("pending", {}), ("running", {}),
    ("failed", {"error": "Unsupported geometry"}),
    ("ready", {"comparable": False, "reason": "One side has no surface geometry."}),
])
def test_geometry_states(status, result):
    def h(req):
        assert "base_path" not in req.url.params
        return httpx.Response(200, json={"id": None, "status": status, "result": result, "heatmap_url": None})

    with client(h) as c:
        out = c.assets.geometry_compare("alice/castle", "model.glb", "main", "feature")
        assert out["status"] == status and out["result"] == result and out["heatmap_url"] is None


def test_metadata_deltas_and_nullable_sides():
    diff = {"statistics": {"triangle_count": {"base": 10, "head": 12, "delta": 2},
                           "node_count": {"base": None, "head": 2, "delta": None}},
            "bounding_box": {"base": None, "head": {"size": [1, 2, 3]}, "size_changed": True},
            "materials": {"added": ["Stone"], "removed": ["Wood"]}}
    responses = [{"base": None, "head": {}, "status": "added", "diff": None},
                 {"base": {}, "head": None, "status": "removed", "diff": None},
                 {"base": {}, "head": {}, "status": "modified", "diff": diff}]

    def h(req):
        assert "base_path" not in req.url.params
        return httpx.Response(200, json=responses.pop(0))

    with client(h) as c:
        assert c.assets.compare("a/b", "m.glb", "base", "head")["base"] is None
        assert c.assets.compare("a/b", "m.glb", "base", "head")["head"] is None
        assert c.assets.compare("a/b", "m.glb", "base", "head")["diff"] == diff


@pytest.mark.parametrize("status,exception", [
    (401, AuthenticationRequired), (403, PermissionDenied), (404, NotFound), (422, SculptaryError),
])
def test_new_resources_use_existing_errors(status, exception):
    def h(req):
        return httpx.Response(status, json={"error": {"code": "invalid", "message": "No access", "field": "name"}})

    with client(h) as c:
        for operation in [lambda: c.collections.get("studio/kits"),
                          lambda: c.collections.delete("studio/kits"),
                          lambda: c.pulls.comment("a/b", 1, "Hello"),
                          lambda: c.pulls.reply("a/b", 1, "root", "Hello"),
                          lambda: c.pulls.resolve("a/b", "root"),
                          lambda: c.pulls.unresolve("a/b", "root"),
                          lambda: c.assets.geometry_compare("a/b", "m.glb", "a", "b")]:
            with pytest.raises(exception) as error:
                operation()
            assert error.value.code == "invalid" and error.value.field == "name"
