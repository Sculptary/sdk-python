# Sculptary Python SDK

Python client for the [Sculptary](https://sculptary.com) API: repositories, model files and their
geometry metadata, uploads, releases, pull requests and search.

Version 0.4 adds the async client, lazy pagination and bounded retries. The sync
API keeps its existing request and resource behavior.

`AsyncSculptary` provides the same resources and method arguments with awaitable
network operations. Use an async context manager to close its connection pool:

```python
from sculptary import AsyncSculptary

async def inspect_models():
    async with AsyncSculptary() as client:
        repo = await client.repos.get("northwind/medieval-castle")
        assets = await client.assets.list(repo.full_name)
        await client.contents.download(repo.full_name, "models/castle.glb", "castle.glb")
        return assets
```

Async downloads stream to a temporary sibling file and replace a path destination
only after success. Failed or cancelled downloads preserve an existing file.
Caller-owned binary streams stay open and may contain partial output on failure.
Multipart uploads stream local files through HTTPX and close file handles on exit.
Signed storage redirects do not carry the API credential to a different origin.
The synchronous client retains its existing behavior.

Async iterators fetch additional pages only as they are consumed:

```python
async with AsyncSculptary() as client:
    async for repo in client.repos.iter_mine():
        print(repo.full_name)
    async for pull in client.pulls.iter("northwind/medieval-castle", state="all"):
        print(pull["number"], pull["title"])
    async for repo in client.iter_search("castle", license="CC0-1.0"):
        print(repo.full_name)
```

Paged helpers are `iter_search`, `repos.iter_for`, `repos.iter_mine`,
`repos.iter_commits`, `pulls.iter`, `issues.iter` and `releases.iter`. Use
`per_page=` to set the page size. Search allows 50 per page, repository lists
allow 200, and the other paged lists allow 100. Filters are retained on every
page. Pull and issue iterators accept extra API filters such as `label=` and
`milestone=`; issues also accept `q=`.

`collections.iter_for`, `collections.iter_containing`, `repos.iter_branches`,
`contents.iter_tree`, `assets.iter`, `assets.iter_formats` and
`pulls.iter_comments` iterate complete, unpaginated API responses. Existing
`list` methods still return one response. Use immutable refs for repeatable
commit history; live data and permissions are checked on each page.

Paged iterators require a server with pagination metadata. An older server
that caps a list without exposing its next page raises `SculptaryError` with
code `invalid_pagination`. External next links, cycles and malformed paging
metadata raise the same error; authorization failures retain their usual
exception type.

The async client retries transient network failures and HTTP 408, 429, 500, 502,
503 and 504 for GET, HEAD, OPTIONS, PUT and DELETE when their bodies are replayable.
It retries at most twice by default, with exponential backoff and full jitter.
POST, PATCH and one-shot request streams are sent once. Configure this with
`max_retries=`, `retry_base_delay=` (default 0.5 seconds) and `retry_max_delay=`
(default 30 seconds); `max_retries=0` disables retries.

`Retry-After` seconds and HTTP dates are respected. If the server asks for longer
than `retry_max_delay`, its error is returned without retrying early. Invalid
hints use bounded backoff. Cancelling an operation cancels its backoff as well.
Path downloads can restart after a network failure without replacing the old
file or appending duplicate data. Caller-owned output streams are retried only
before any bytes have been written. The final response error or transport
exception is preserved after retry exhaustion.

```bash
pip install git+https://github.com/Sculptary/sdk-python
```

```python
from sculptary import Sculptary

sdb = Sculptary(token="sdb_pat_...")            # or set SCULPTARY_TOKEN; SCULPTARY_HOST for other hosts

repo = sdb.repos.create("castle-kit", private=True, topics=["medieval"])
sdb.contents.upload(repo.full_name, {"models/castle.glb": "build/castle.glb"}, message="Add castle")

for asset in sdb.assets.list(repo.full_name):     # processed after each commit
    print(asset.path, asset.status, asset.triangles)

sdb.contents.download("northwind/medieval-castle", "models/castle.glb", "castle.glb")
sdb.releases.create(repo.full_name, "v1.0.0", name="First release")
```

Uploads made through the API become real Git commits; model files are stored with Git LFS. Errors raise
`SculptaryError` subclasses (`NotFound`, `PermissionDenied`, `AuthenticationRequired`).

```python
collection = sdb.collections.create("Castle kits", owner="northwind", slug="castle-kits")
sdb.collections.add_item("northwind/castle-kits", repo.full_name, note="Use the tower")
sdb.collections.reorder("northwind/castle-kits", [repo.full_name])
for collection in sdb.collections.list_for("northwind"):  # user or organization
    print(collection["name"], collection["item_count"])

comment = sdb.pulls.comment(repo.full_name, 1, "Round this edge", path="models/castle.glb",
                            spatial_anchor={"point": [1, 2, 3], "normal": [0, 1, 0],
                                            "camera": {"position": [4, 5, 6], "target": [1, 2, 3], "zoom": 1}})
comments = sdb.pulls.comments(repo.full_name, 1)
sdb.pulls.reply(repo.full_name, 1, comment["id"], "Fixed")
sdb.pulls.resolve(repo.full_name, comment["id"])
sdb.pulls.unresolve(repo.full_name, comment["id"])

metadata = sdb.assets.compare(repo.full_name, "models/castle.glb", "v1.0.0", "main")
deviation = sdb.assets.geometry_compare(repo.full_name, "models/castle.glb", "v1.0.0", "main")
print(metadata["diff"], deviation["status"], deviation["result"].get("hausdorff"))
```

Collections also support `get`, `update`, `delete`, `remove_item` and `containing(repository)`.
Use `owner/slug` for a collection and `owner/repository` for an item. Lists are unpaginated; collection
details include visible items with notes and positions. Reordering requires every visible repository
exactly once. An omitted item note preserves its current value; `note=""` clears it. Updates preserve
omitted fields and allow `description=""` to clear the description.

New responses have exported `TypedDict` models (`Collection`, `Comment`, `AssetComparison`,
`GeometryComparison`, `SpatialAnchor`, `CameraPose`) and retain the API's dictionary shape and unknown
fields. Spatial positions use `point`; the asset path is the comment's `path`. Version 0.3 uses real
replies through `parent_id`. Reply to a root comment; threads support one level of replies. PR detail
contains root comments with nested `replies`, `resolved_at` and `resolved_by`. The comment author,
PR author and repository writers can resolve or reopen a thread. Replies share the root's pin.

`SpatialAnchor.animation` can hold a clip, name and time or `{ "rest": True }`
for the authored pose. Asset wire types also expose optional LOD and standard
desktop preview URLs. Unknown response fields continue to be retained.

Both comparison methods accept `base_path=` for renamed models. Added/removed assets can have null
sides and a null metadata diff. Deviation may be `waiting`, `pending`, `running`, `ready` or `failed`;
call again while processing. Available measurements include `method`, `confidence`, `resolution` and
`hausdorff` (largest change in model units). Non-comparable and failed results include `reason` or
`error` instead. Heatmap data consists of a scale under `result.heatmap` and an authenticated GLB URL
at `heatmap_url`; it is not an inline array.

Tests run offline; set `SCULPTARY_TEST_HOST` to also run read-only checks against a live server.

Licensed under MIT or Apache-2.0, at your option.
