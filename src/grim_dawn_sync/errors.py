"""Shared exit-code and error contracts for the sync CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


EXIT_OK = 0
EXIT_CONFIGURATION = 2
EXIT_VALIDATION = 3
EXIT_CONFLICT = 4
EXIT_LAUNCH = 5
EXIT_RECOVERY_REQUIRED = 6


@dataclass
class SyncError(Exception):
    """An expected, machine-readable failure that never exposes secrets."""

    code: str
    message: str
    exit_code: int = EXIT_CONFIGURATION
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}
