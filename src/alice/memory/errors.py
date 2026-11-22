"""Memory-domain errors."""

from __future__ import annotations


class MemoryErrorBase(RuntimeError):
    """Base for memory failures."""


class CoreMemoryError(MemoryErrorBase):
    """The editable core-memory JSON file is missing or malformed."""


class MemoryCapError(MemoryErrorBase):
    """Hard memory exceeded its prompt budget. Prune it; never truncate."""


class ProposalError(MemoryErrorBase):
    """A proposal is missing or no longer pending."""


class ArchiveError(MemoryErrorBase):
    """Archive search or indexing failed."""
