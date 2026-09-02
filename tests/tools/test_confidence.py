import pytest

from acie.ir.symbol import Confidence
from acie.tools.confidence import filter_by_min_confidence
from acie.tools.errors import InvalidArgumentError


class _Item:
    def __init__(self, confidence: Confidence) -> None:
        self.confidence = confidence


def test_filter_by_min_confidence_none_is_a_no_op():
    items = [_Item(Confidence.EXTRACTED), _Item(Confidence.AMBIGUOUS)]
    assert filter_by_min_confidence(items, None) == items


def test_filter_by_min_confidence_extracted_keeps_only_extracted():
    extracted = _Item(Confidence.EXTRACTED)
    ambiguous = _Item(Confidence.AMBIGUOUS)
    inferred = _Item(Confidence.INFERRED)

    result = filter_by_min_confidence([extracted, ambiguous, inferred], "EXTRACTED")

    assert result == [extracted]


def test_filter_by_min_confidence_inferred_keeps_extracted_and_inferred_not_ambiguous():
    extracted = _Item(Confidence.EXTRACTED)
    ambiguous = _Item(Confidence.AMBIGUOUS)
    inferred = _Item(Confidence.INFERRED)

    result = filter_by_min_confidence([extracted, ambiguous, inferred], "INFERRED")

    assert result == [extracted, inferred]


def test_filter_by_min_confidence_ambiguous_keeps_everything():
    items = [_Item(Confidence.EXTRACTED), _Item(Confidence.AMBIGUOUS), _Item(Confidence.INFERRED)]
    assert filter_by_min_confidence(items, "AMBIGUOUS") == items


def test_filter_by_min_confidence_accepts_enum_member_directly():
    extracted = _Item(Confidence.EXTRACTED)
    ambiguous = _Item(Confidence.AMBIGUOUS)
    assert filter_by_min_confidence([extracted, ambiguous], Confidence.EXTRACTED) == [extracted]


def test_filter_by_min_confidence_rejects_invalid_value():
    with pytest.raises(InvalidArgumentError):
        filter_by_min_confidence([_Item(Confidence.EXTRACTED)], "NOT_A_REAL_TIER")


def test_filter_by_min_confidence_custom_key():
    class _Wrapped:
        def __init__(self, c):
            self.tier = c

    extracted = _Wrapped(Confidence.EXTRACTED)
    ambiguous = _Wrapped(Confidence.AMBIGUOUS)
    result = filter_by_min_confidence([extracted, ambiguous], "EXTRACTED", key=lambda w: w.tier)
    assert result == [extracted]
