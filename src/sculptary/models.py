"""Typed wire models for collection, spatial review and comparison responses."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from typing_extensions import NotRequired

Vector3 = tuple[float, float, float] | list[float]
Visibility = Literal["public", "private"]


class Owner(TypedDict):
    slug: str
    kind: Literal["user", "organization"]
    display_name: str
    avatar_url: str


class CollectionItem(TypedDict):
    """A repository in a collection, in ascending position order."""

    repository: dict[str, Any]
    note: str
    position: int


class Collection(TypedDict):
    """Collection summary; detail and mutation responses also include items."""

    id: str
    owner: Owner
    slug: str
    name: str
    description: str
    visibility: Visibility
    created_by: str | None
    created_at: str
    updated_at: str
    item_count: int
    viewer_can_manage: bool
    items: NotRequired[list[CollectionItem]]


class CameraPose(TypedDict):
    """Viewer camera in model coordinates."""

    position: Vector3
    target: Vector3
    zoom: float


class SpatialAnchor(TypedDict):
    """A model point. The asset path is the comment's top-level path field."""

    point: Vector3
    normal: NotRequired[Vector3 | None]
    camera: NotRequired[CameraPose | None]
    object: NotRequired[str]
    mesh: NotRequired[str]


class UserRef(TypedDict):
    username: str
    display_name: str
    avatar_url: str


class Comment(TypedDict):
    """A PR comment, optionally pinned to a file, line or model point."""

    id: str
    author: UserRef | None
    body: str
    body_html: str
    path: str | None
    line: int | None
    commit_sha: str | None
    spatial_anchor: SpatialAnchor | None
    review_id: str | None
    created_at: str
    updated_at: str


class AssetData(TypedDict):
    """Wire representation of an asset revision in a comparison."""

    id: str
    path: str
    commit: str
    format: str
    size: int
    status: str
    lfs: bool
    metadata: dict[str, Any]
    warnings: list[str]
    error: str | None
    preview_url: str | None
    thumbnail_url: str | None
    source_url: str
    processed_at: str | None


class NumericDelta(TypedDict):
    base: float | None
    head: float | None
    delta: float | None


class BoundingBox(TypedDict, total=False):
    min: Vector3
    max: Vector3
    size: Vector3
    center: Vector3


class BoundsDelta(TypedDict):
    base: BoundingBox | None
    head: BoundingBox | None
    size_changed: bool


class MaterialsDelta(TypedDict):
    added: list[str]
    removed: list[str]


class MetadataDiff(TypedDict):
    statistics: dict[str, NumericDelta]
    bounding_box: BoundsDelta
    materials: MaterialsDelta


class AssetComparison(TypedDict):
    """Metadata comparison; a side or diff can be absent for added/removed assets."""

    base: AssetData | None
    head: AssetData | None
    status: Literal["added", "removed", "modified", "unchanged"]
    diff: MetadataDiff | None


class SideValues(TypedDict):
    base: float | None
    head: float | None


class Topology(TypedDict):
    vertices: int
    faces: int
    euler: int


class TopologyComparison(TypedDict):
    base: Topology
    head: Topology


class Heatmap(TypedDict):
    """Colour scale for the GLB at heatmap_url, in model units."""

    scale_min: float
    scale_max: float
    unit: str


class DeviationSummary(TypedDict, total=False):
    """Surface measurements. Hausdorff is the largest change in model units.

    Fields are absent while waiting/running, or for failed/non-comparable models.
    Unknown server fields are retained in the returned dictionary.
    """

    comparable: bool
    identical: bool
    method: str
    approximate: bool
    hausdorff: float
    mean_deviation: float
    rms_deviation: float
    tolerance: float
    resolution: float
    confidence: Literal["high", "medium", "low"]
    removed_surface_percent: float
    added_surface_percent: float
    area: SideValues
    volume: SideValues
    topology: TopologyComparison
    heatmap: Heatmap
    notes: list[str]
    reason: str
    error: str


class GeometryComparison(TypedDict):
    """Asynchronous deviation result and an authenticated heatmap GLB URL."""

    id: str | None
    status: Literal["waiting", "pending", "running", "ready", "failed"]
    result: DeviationSummary
    heatmap_url: str | None
