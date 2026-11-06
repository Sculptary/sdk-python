# Sculptary Python SDK

Python client for the [Sculptary](https://sculptary.com) API: repositories, model files and their
geometry metadata, uploads, releases, pull requests and search.

Version 0.2 adds collections, spatial PR comments and surface deviation comparisons.

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
fields. Spatial positions use `point`; the asset path is the comment's `path`. Replies quote the source
body and reuse its anchor, path, line and commit. The API stores these as ordinary PR comments without
threads or parent IDs.

Both comparison methods accept `base_path=` for renamed models. Added/removed assets can have null
sides and a null metadata diff. Deviation may be `waiting`, `pending`, `running`, `ready` or `failed`;
call again while processing. Available measurements include `method`, `confidence`, `resolution` and
`hausdorff` (largest change in model units). Non-comparable and failed results include `reason` or
`error` instead. Heatmap data consists of a scale under `result.heatmap` and an authenticated GLB URL
at `heatmap_url`; it is not an inline array.

Tests run offline; set `SCULPTARY_TEST_HOST` to also run read-only checks against a live server.

Licensed under MIT or Apache-2.0, at your option.
