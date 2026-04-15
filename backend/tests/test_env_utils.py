import logging
from datetime import datetime

import pytest

from env_utils import (
    env_bool,
    env_float,
    env_int,
    parse_iso_datetime,
    parse_iso_datetime_with_options,
    utc_now_naive,
)


def test_env_int_uses_default_verbatim_when_env_missing(monkeypatch) -> None:
    monkeypatch.delenv("TEST_ENV_INT_VALUE", raising=False)

    assert env_int("TEST_ENV_INT_VALUE", -1, minimum=0) == -1


def test_env_int_applies_minimum_only_to_explicit_env_values(monkeypatch) -> None:
    monkeypatch.setenv("TEST_ENV_INT_VALUE", "-2")

    assert env_int("TEST_ENV_INT_VALUE", -1, minimum=0) == 0


def test_env_int_returns_default_on_invalid_env(monkeypatch) -> None:
    monkeypatch.setenv("TEST_ENV_INT_VALUE", "not-a-number")

    assert env_int("TEST_ENV_INT_VALUE", -1, minimum=0) == -1


def test_env_int_logs_warning_on_invalid_env(monkeypatch, caplog) -> None:
    monkeypatch.setenv("TEST_ENV_INT_VALUE", "not-a-number")

    with caplog.at_level(logging.WARNING):
        assert env_int("TEST_ENV_INT_VALUE", -1, minimum=0) == -1

    assert "TEST_ENV_INT_VALUE" in caplog.text
    assert "invalid integer environment value" in caplog.text


def test_env_int_treats_blank_env_as_unset_without_warning(monkeypatch, caplog) -> None:
    monkeypatch.setenv("TEST_ENV_INT_VALUE", "   ")

    with caplog.at_level(logging.WARNING):
        assert env_int("TEST_ENV_INT_VALUE", -1, minimum=0) == -1

    assert caplog.text == ""


def test_env_int_logs_warning_when_value_is_clamped(monkeypatch, caplog) -> None:
    monkeypatch.setenv("TEST_ENV_INT_VALUE", "-2")

    with caplog.at_level(logging.WARNING):
        assert env_int("TEST_ENV_INT_VALUE", -1, minimum=0) == 0

    assert "TEST_ENV_INT_VALUE" in caplog.text
    assert "below minimum 0" in caplog.text


def test_env_bool_parses_truthy_and_falsey_values(monkeypatch) -> None:
    monkeypatch.setenv("TEST_ENV_BOOL_VALUE", "yes")
    assert env_bool("TEST_ENV_BOOL_VALUE", False) is True

    monkeypatch.setenv("TEST_ENV_BOOL_VALUE", "FALSE")
    assert env_bool("TEST_ENV_BOOL_VALUE", True) is False


def test_env_bool_returns_default_and_logs_warning_on_invalid_env(
    monkeypatch,
    caplog,
) -> None:
    monkeypatch.setenv("TEST_ENV_BOOL_VALUE", "2")

    with caplog.at_level(logging.WARNING):
        assert env_bool("TEST_ENV_BOOL_VALUE", True) is True

    assert "TEST_ENV_BOOL_VALUE" in caplog.text
    assert "invalid boolean environment value" in caplog.text


def test_env_float_applies_minimum_only_to_explicit_env_values(monkeypatch) -> None:
    monkeypatch.setenv("TEST_ENV_FLOAT_VALUE", "-2.5")

    assert env_float("TEST_ENV_FLOAT_VALUE", -1.0, minimum=0.0) == 0.0


def test_env_float_logs_warning_on_invalid_env(monkeypatch, caplog) -> None:
    monkeypatch.setenv("TEST_ENV_FLOAT_VALUE", "not-a-float")

    with caplog.at_level(logging.WARNING):
        assert env_float("TEST_ENV_FLOAT_VALUE", -1.0, minimum=0.0) == 0.0

    assert "TEST_ENV_FLOAT_VALUE" in caplog.text
    assert "invalid float environment value" in caplog.text


def test_env_float_treats_blank_env_as_unset_without_warning(monkeypatch, caplog) -> None:
    monkeypatch.setenv("TEST_ENV_FLOAT_VALUE", "   ")

    with caplog.at_level(logging.WARNING):
        assert env_float("TEST_ENV_FLOAT_VALUE", -1.0, minimum=0.0) == 0.0

    assert caplog.text == ""


def test_env_float_logs_warning_when_value_is_clamped(monkeypatch, caplog) -> None:
    monkeypatch.setenv("TEST_ENV_FLOAT_VALUE", "-2.5")

    with caplog.at_level(logging.WARNING):
        assert env_float("TEST_ENV_FLOAT_VALUE", -1.0, minimum=0.0) == 0.0

    assert "TEST_ENV_FLOAT_VALUE" in caplog.text
    assert "below minimum 0.0" in caplog.text


@pytest.mark.parametrize("raw_value", ["nan", "inf", "-inf"])
def test_env_float_rejects_non_finite_values(
    monkeypatch, caplog, raw_value: str
) -> None:
    monkeypatch.setenv("TEST_ENV_FLOAT_VALUE", raw_value)

    with caplog.at_level(logging.WARNING):
        assert env_float("TEST_ENV_FLOAT_VALUE", -1.0, minimum=0.0) == 0.0

    assert "TEST_ENV_FLOAT_VALUE" in caplog.text
    assert "invalid float environment value" in caplog.text


def test_utc_now_naive_returns_naive_datetime() -> None:
    value = utc_now_naive()

    assert isinstance(value, datetime)
    assert value.tzinfo is None


def test_parse_iso_datetime_accepts_utc_suffix() -> None:
    parsed = parse_iso_datetime("2026-03-23T13:18:40Z")

    assert parsed is not None
    assert parsed.isoformat() == "2026-03-23T13:18:40+00:00"


def test_parse_iso_datetime_with_options_can_return_naive_utc() -> None:
    parsed = parse_iso_datetime_with_options(
        "2026-03-23T13:18:40+08:00",
        naive_utc=True,
    )

    assert parsed is not None
    assert parsed.isoformat() == "2026-03-23T05:18:40"


def test_parse_iso_datetime_with_options_can_raise_custom_error() -> None:
    with pytest.raises(ValueError, match="custom error"):
        parse_iso_datetime_with_options(
            "not-a-datetime",
            raise_on_error=True,
            error_message="custom error",
        )
