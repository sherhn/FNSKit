from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path


class Profile(StrEnum):
    VAT = "vat"
    REGISTRATION = "registration"
    CUSTOM = "custom"


class OutputFormat(StrEnum):
    TIFF = "tiff"
    PDF = "pdf"
    PDFA = "pdfa"
    ZIP = "zip"


class CheckStatus(StrEnum):
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class CheckItem:
    id: str
    title: str
    status: CheckStatus
    actual: str
    expected: str
    details: str


@dataclass(frozen=True)
class PageInspection:
    page: int
    dpi_x: int
    dpi_y: int
    color: str
    bits: int
    width_mm: float
    height_mm: float
    status: CheckStatus
    issues: list[str]


@dataclass(frozen=True)
class InspectionSummary:
    status: CheckStatus
    title: str
    description: str
    passed_count: int
    warning_count: int
    error_count: int


@dataclass(frozen=True)
class InspectionReport:
    valid: bool
    profile: Profile
    filename: str
    file_size: int
    file_size_formatted: str
    page_count: int
    format: str
    summary: InspectionSummary
    checks: list[CheckItem]
    pages: list[PageInspection]

    def to_dict(self) -> dict:
        return asdict(self)


class ProcessingError(Exception):
    """A safe processing failure that can be returned to the client."""

    def __init__(
        self,
        message: str,
        *,
        source_index: int | None = None,
        page_number: int | None = None,
    ) -> None:
        super().__init__(message)
        self.source_index = source_index
        self.page_number = page_number


from typing import NamedTuple


class PageSpec(NamedTuple):
    source_index: int
    page_number: int
    rotation: int = 0

    def __eq__(self, other: object) -> bool:
        if isinstance(other, tuple):
            if len(other) == 2:
                return (self.source_index, self.page_number) == other
            if len(other) == 3:
                return (self.source_index, self.page_number, self.rotation) == other
        return False

    def __hash__(self) -> int:
        return hash((self.source_index, self.page_number, self.rotation))


@dataclass(frozen=True)
class ProcessingOptions:
    dpi: int
    color_mode: str
    compression: str
    quality: int = 85
    target_max_mb: int | None = None
    threshold: int = 180


@dataclass(frozen=True)
class ProcessingResult:
    output_path: Path
    page_count: int
