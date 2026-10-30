"""Controlled errors for the built-in container feature."""

from __future__ import annotations


class ContainerError(RuntimeError):
    """Safe, user-facing container error with a stable machine code."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "container_error",
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})

    def safe_message(self) -> str:
        return str(self)


__all__ = ["ContainerError"]
