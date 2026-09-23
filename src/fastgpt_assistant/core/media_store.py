"""Media storage and physical attachment resolution."""

from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path

from .models import Deliverable


def derive_media_id(
    *,
    record_id: str,
    field_name: str,
    display_name: str,
    content_hash: str,
    attachment_index: int = 1,
) -> str:
    """Generate a stable opaque media identifier (med_<hex32>)."""
    canonical = "\x1f".join(
        (
            record_id.strip(),
            field_name.strip(),
            str(max(1, attachment_index)),
            display_name.strip(),
            content_hash.strip(),
        )
    )
    return f"med_{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:32]}"


class MediaStore:
    """Resolves media_ids to local physical files and deliverables."""

    def __init__(self, resources_root: Path):
        self.resources_root = resources_root.resolve()
        self._media_registry: dict[str, Path] = {}

    def register(self, media_id: str, physical_path: Path) -> None:
        """Register a media_id to its absolute local file path."""
        self._media_registry[media_id] = physical_path.resolve()

    def get_path(self, media_id: str) -> Path | None:
        """Look up the local physical path for a media_id."""
        path = self._media_registry.get(media_id)
        if path and path.is_file():
            return path
        return None

    def build_deliverable(
        self,
        media_id: str,
        filename: str,
        path: Path | None = None,
        base_download_url: str = "",
    ) -> Deliverable:
        """Build a Deliverable object with verified metadata."""
        target_path = path or self.get_path(media_id)
        size_bytes = 0
        sha256 = ""
        mime_type, _ = mimetypes.guess_type(filename)
        mime_type = mime_type or "application/octet-stream"

        if target_path and target_path.is_file():
            size_bytes = target_path.stat().st_size
            hasher = hashlib.sha256()
            with target_path.open("rb") as f:
                while chunk := f.read(65536):
                    hasher.update(chunk)
            sha256 = hasher.hexdigest()

        download_url = (
            f"{base_download_url.rstrip('/')}/v1/media/{media_id}{Path(filename).suffix}"
            if base_download_url
            else ""
        )

        return Deliverable(
            media_id=media_id,
            filename=filename,
            mime_type=mime_type,
            size_bytes=size_bytes,
            sha256=sha256,
            download_url=download_url,
        )
