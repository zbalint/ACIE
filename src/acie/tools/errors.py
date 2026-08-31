"""Structured error codes for the MCP tool surface.

See ARCHITECTURE.md "MCP Tool Surface" -- cross-cutting rule names the full
set: STALE_INDEX_GENERATION, SYMBOL_NOT_FOUND, INVALID_PATTERN,
INDEX_NOT_READY, and (specific to explain) EDGE_NOT_FOUND. Only the codes
an actual tool implementation currently raises get a class here -- the rest
are added alongside the tool (graph, etc.) that first needs them, each
driven by its own failing test, not declared speculatively ahead of time.

One exception class per code, each carrying its code as a class attribute
so a future MCP-transport layer can map exception type -> wire error code
without a string-matching lookup table.
"""


class AcieToolError(Exception):
    code: str = "ACIE_TOOL_ERROR"


class StaleIndexGenerationError(AcieToolError):
    code = "STALE_INDEX_GENERATION"


class SymbolNotFoundError(AcieToolError):
    code = "SYMBOL_NOT_FOUND"


class InvalidPatternError(AcieToolError):
    code = "INVALID_PATTERN"


class EdgeNotFoundError(AcieToolError):
    code = "EDGE_NOT_FOUND"
