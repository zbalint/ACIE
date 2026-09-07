import pytest

from acie.tools.errors import InvalidArgumentError
from acie.tools.validation import require_dict_keys


def test_require_dict_keys_returns_the_original_dict_with_extra_keys():
    value = {"file": "pkg/mod.py", "line": 1, "column": 0, "extra": True}

    result = require_dict_keys(value, {"file", "line", "column"}, param_name="position")

    assert result is value


def test_require_dict_keys_rejects_a_missing_key_with_an_actionable_error():
    with pytest.raises(InvalidArgumentError, match=r"position.*column"):
        require_dict_keys({"file": "pkg/mod.py", "line": 1}, {"file", "line", "column"}, param_name="position")


@pytest.mark.parametrize("value", ["a string", None, ["a", "list"]])
def test_require_dict_keys_rejects_non_dict_values_with_the_parameter_name(value):
    with pytest.raises(InvalidArgumentError, match="position"):
        require_dict_keys(value, {"file"}, param_name="position")
