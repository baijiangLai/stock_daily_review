"""Application-level errors mapped to transport status codes by interfaces."""

from __future__ import annotations


class WorkflowConflictError(RuntimeError):
    """A conflicting workflow already exists."""


class WorkflowNotFoundError(RuntimeError):
    """The requested workflow state does not exist."""
