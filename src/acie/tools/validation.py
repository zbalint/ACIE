"""Validation helpers shared by structured MCP tool inputs."""

from acie.tools.errors import InvalidArgumentError


def require_dict_keys(value: object, required: set[str], *, param_name: str) -> dict:
    """Validate that ``value`` is a dict containing every required key.

    Extra keys are allowed and the original dict is returned unchanged.
    """
    if not isinstance(value, dict):
        raise InvalidArgumentError(f"{param_name} must be a dict, got {type(value).__name__}")

    missing = required - value.keys()
    if missing:
        names = ", ".join(sorted(missing))
        raise InvalidArgumentError(f"{param_name} is missing required key(s): {names}")

    return value
