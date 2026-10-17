from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


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


class Profile(StrEnum):
    VAT = "vat"
    REGISTRATION = "registration"
    CUSTOM = "custom"


class OutputFormat(StrEnum):
    TIFF = "tiff"
    PDF = "pdf"


@dataclass(frozen=True)
class ProcessingOptions:
    dpi: int
    color_mode: str
    compression: str


@dataclass(frozen=True)
class ProcessingResult:
    output_path: Path
    page_count: int
