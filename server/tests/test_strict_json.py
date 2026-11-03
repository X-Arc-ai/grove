"""loads_strict refuses what json.loads lets through and an answer could not carry back."""

import json

import pytest

from grove.strict_json import loads_strict

TEXT = '{"name": "Grüße"}'


@pytest.mark.parametrize(
    ("raw", "parsed"),
    [
        (b"{}", {}),
        (b"[1, 2.5, -3, true, null]", [1, 2.5, -3, True, None]),
        (b'  {"a": "b"}\n', {"a": "b"}),
        (TEXT.encode("utf-8"), {"name": "Grüße"}),
        ('"東京 😀"'.encode(), "東京 😀"),
        (b'"\\ud83d\\ude00"', "\U0001f600"),
        (b'"\\u00e9"', "é"),
        (b"1e308", 1e308),
        (b"0", 0),
    ],
)
def test_strict_json_passes(raw, parsed):
    assert loads_strict(raw) == parsed


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"{not json", id="not-json"),
        pytest.param(b"\xff", id="a-bad-byte"),
        pytest.param(b'"\xc3\x28"', id="bad-utf8-sequence"),
        pytest.param(b'"\xed\xa0\x80"', id="surrogate-as-utf8-bytes"),
        pytest.param(b"\xef\xbb\xbf{}", id="utf-8-bom"),
        pytest.param(TEXT.encode("utf-16"), id="utf-16"),
        pytest.param(TEXT.encode("utf-16-le"), id="utf-16-le"),
        pytest.param(TEXT.encode("utf-16-be"), id="utf-16-be"),
        pytest.param(TEXT.encode("utf-32"), id="utf-32"),
        pytest.param(TEXT.encode("utf-32-le"), id="utf-32-le"),
        pytest.param(TEXT.encode("utf-32-be"), id="utf-32-be"),
        pytest.param(b"NaN", id="nan"),
        pytest.param(b"Infinity", id="infinity"),
        pytest.param(b"-Infinity", id="negative-infinity"),
        pytest.param(b'{"a": [NaN]}', id="nan-inside"),
        pytest.param(b"1e999", id="overflow"),
        pytest.param(b"-1e999", id="negative-overflow"),
        pytest.param(b'{"a": [1e999]}', id="overflow-inside"),
        pytest.param(b'"\\ud800"', id="lone-high-surrogate"),
        pytest.param(b'"\\udc00"', id="lone-low-surrogate"),
        pytest.param(b'"\\ude00\\ud83d"', id="surrogates-in-the-wrong-order"),
        pytest.param(b'{"\\ud800": 1}', id="surrogate-in-a-key"),
        pytest.param(b'[{"a": ["\\ud800"]}]', id="surrogate-inside"),
        pytest.param(b"[" * 200_000 + b"]" * 200_000, id="too-deep"),
        pytest.param(b'{"a":' * 200_000 + b"1" + b"}" * 200_000, id="too-deep-objects"),
        pytest.param(b"1" * 5000, id="an-integer-too-long-for-python"),
    ],
)
def test_strict_json_refuses_with_a_value_error(raw):
    with pytest.raises(ValueError):
        loads_strict(raw)


def test_what_passes_can_be_written_back():
    parsed = loads_strict(b'{"a": ["\\ud83d\\ude00", 1e308, -0.0, 12345678901234567890]}')
    text = json.dumps(parsed, allow_nan=False, ensure_ascii=False)
    assert loads_strict(text.encode("utf-8")) == parsed
