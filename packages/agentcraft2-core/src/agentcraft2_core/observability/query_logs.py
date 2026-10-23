"""Bounded local query parameters; operational logs never include result rows.

The masking lives with the other redaction helpers in ``contracts.redaction``,
so the pipeline can use it without depending on this package. This module
keeps the name importable from where it used to be.
"""

from agentcraft2_core.contracts.redaction import query_parameters

__all__ = ["query_parameters"]
