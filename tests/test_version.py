import asyncio
from importlib.metadata import version
from typing import get_type_hints

import sculptary
from sculptary import AsyncSculptary, Sculptary


def test_package_and_both_clients_report_the_same_version():
    assert version("sculptary") == sculptary.__version__

    async def scenario():
        async with AsyncSculptary(token="") as client:
            with Sculptary(token="") as sync:
                assert (
                    client._http.headers["User-Agent"]
                    == sync._http.headers["User-Agent"]
                    == f"sculptary-python/{sculptary.__version__}"
                )

    asyncio.run(scenario())


def test_current_wire_types_include_preview_variants_and_animation_context():
    assert "animation" in get_type_hints(sculptary.SpatialAnchor)
    assert {"lod_url", "standard_preview_url"} <= get_type_hints(sculptary.AssetData).keys()
