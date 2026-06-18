"""Cloud-ingest (MediaMTX) runtime path state — org-scoped view for the
Pipeline Canvas stage 2 ("is video actually arriving at the cloud right now")."""

from pydantic import BaseModel


class IngestPath(BaseModel):
    """Runtime state of one MediaMTX path (one camera's cloud stream)."""

    path: str
    name: str
    # A publisher is active and the stream is being served (video arriving).
    ready: bool
    readers: int


class IngestPathsResponse(BaseModel):
    """`available` is False when MediaMTX's control API is not configured or
    unreachable — the canvas renders stage 2 as "unknown" rather than "0 ready"."""

    available: bool
    paths: list[IngestPath]
