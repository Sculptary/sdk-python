# Sculptary Python SDK

Python client for the [Sculptary](https://sculptary.com) API: repositories, model files and their
geometry metadata, uploads, releases, pull requests and search.

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

Tests run offline; set `SCULPTARY_TEST_HOST` to also run read-only checks against a live server.

Licensed under MIT or Apache-2.0, at your option.
