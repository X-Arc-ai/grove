"""Scalar checks reject input before conversion, regex work, or hashing."""

import pytest

from grove.authority import scalars


@pytest.mark.parametrize("value", ["A", "z9_.:-", "x" * 128])
def test_identifier_returns_the_same_string(value):
    assert scalars.validate_identifier(value) is value


@pytest.mark.parametrize("value", ["", "x" * 129, "naïve", "a\n", "a\0", "a/b", "a b", "😀"])
def test_identifier_rejects_invalid_strings(value):
    with pytest.raises(scalars.ScalarRefused) as error:
        scalars.validate_identifier(value)
    assert str(error.value) in ("identifier:length", "identifier:format")


@pytest.mark.parametrize(
    "validate,value",
    [
        (scalars.validate_identifier, "x" * 129),
        (scalars.validate_digest, "f" * 65),
    ],
)
def test_length_refuses_before_regex(validate, value, monkeypatch):
    class NoRegex:
        def fullmatch(self, _value):
            pytest.fail("overlength input reached the regex")

    monkeypatch.setattr(scalars, "_IDENTIFIER", NoRegex())
    monkeypatch.setattr(scalars, "_DIGEST", NoRegex())
    with pytest.raises(scalars.ScalarRefused, match=":length$"):
        validate(value)


def test_no_input_conversion_or_hash():
    class NoConversion:
        def __str__(self):
            pytest.fail("input converted to text")

        def __int__(self):
            pytest.fail("input converted to integer")

        def __hash__(self):
            pytest.fail("input hashed")

    for validate in (scalars.validate_identifier, scalars.validate_digest, scalars.validate_count):
        with pytest.raises(scalars.ScalarRefused, match=":type$"):
            validate(NoConversion())


def test_digest_accepts_only_lowercase_hex():
    value = "0123456789abcdef" * 4
    assert scalars.validate_digest(value) is value
    for bad in (value.upper(), "g" * 64, "é" * 64, "f" * 63, "f" * 63 + "\n"):
        with pytest.raises(scalars.ScalarRefused):
            scalars.validate_digest(bad)


@pytest.mark.parametrize("value", [0, 1, 2**63 - 1])
def test_count_exact_boundaries(value):
    assert scalars.validate_count(value) is value


@pytest.mark.parametrize("value", [-1, 2**63, True, False, "1", 1.0, None, b"1"])
def test_count_refuses_malformed_values(value):
    with pytest.raises(scalars.ScalarRefused) as error:
        scalars.validate_count(value)
    assert str(error.value) in ("count:type", "count:range")


def test_exact_types_refuse_subclass_hooks():
    class Text(str):
        def __len__(self):
            pytest.fail("subclass hook ran")

    class Count(int):
        def __le__(self, _other):
            pytest.fail("subclass hook ran")

    for validate, value in (
        (scalars.validate_identifier, Text("a")),
        (scalars.validate_digest, Text("f" * 64)),
        (scalars.validate_count, Count(1)),
    ):
        with pytest.raises(scalars.ScalarRefused, match=":type$"):
            validate(value)
