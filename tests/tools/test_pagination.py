from acie.tools.pagination import decode_cursor, encode_cursor


def test_round_trips_a_string_last_key():
    cursor = encode_cursor(3, "pkg/mod.py:foo#function")

    assert decode_cursor(cursor) == (3, "pkg/mod.py:foo#function")


def test_round_trips_a_composite_list_last_key():
    # find_references' ordering key has no single string identity -- it's
    # the (site_file, site_line, site_col, predicate, source) tuple. JSON
    # already round-trips a list faithfully; this pins that as a supported,
    # tested contract rather than an incidental side effect of json.dumps.
    last_key = ["pkg/mod.py", 2, 4, "calls", "pkg/mod.py:caller#function"]

    cursor = encode_cursor(5, last_key)

    assert decode_cursor(cursor) == (5, last_key)
