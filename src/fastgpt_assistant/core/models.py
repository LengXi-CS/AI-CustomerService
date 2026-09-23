"""Domain models for query execution, deliverables, and provenances."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


@dataclass(slots=True)
class Deliverable:
    """A physical file attachment deliverable."""

    media_id: str
    filename: str
    mime_type: str = "application/octet-stream"
    size_bytes: int = 0
    sha256: str = ""
    download_url: str = ""

    @property
    def file_name(self) -> str:
        return self.filename

    @property
    def file_path(self) -> str:
        return self.download_url

    @property
    def media_type(self) -> str:
        return self.mime_type

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Provenance:
    """The source record and column supporting an answer item."""

    table: str
    record_id: str
    field_name: str
    primary_key: str = ""
    display_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ExecutionResult:
    """Standardized execution outcome produced by SqliteQueryExecutor or FastGPT."""

    status: Literal["answered", "no_evidence", "clarify", "error"]
    text: str = ""
    deliverables: list[Deliverable] = field(default_factory=list)
    provenances: list[Provenance] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_success(self) -> bool:
        return self.status == "answered"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "text": self.text,
            "deliverables": [d.to_dict() for d in self.deliverables],
            "provenances": [p.to_dict() for p in self.provenances],
            "metadata": self.metadata,
        }
