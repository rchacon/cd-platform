import pytest

from cd.server.settings import _int_env


def test_int_env_reads_a_numeric_value(monkeypatch):
    monkeypatch.setenv("CD_TEST_LIMIT", "7")
    assert _int_env("CD_TEST_LIMIT", 10) == 7


def test_int_env_reads_zero(monkeypatch):
    monkeypatch.setenv("CD_TEST_LIMIT", "0")
    assert _int_env("CD_TEST_LIMIT", 10) == 0


def test_int_env_falls_back_on_an_empty_string(monkeypatch):
    # A task-def passing NAME= with no value, or docker-compose's ${VAR:-}
    # -- must not crash startup with int("").
    monkeypatch.setenv("CD_TEST_LIMIT", "")
    assert _int_env("CD_TEST_LIMIT", 10) == 10


def test_int_env_falls_back_when_unset(monkeypatch):
    monkeypatch.delenv("CD_TEST_LIMIT", raising=False)
    assert _int_env("CD_TEST_LIMIT", 100) == 100


def test_int_env_still_raises_on_a_non_numeric_typo(monkeypatch):
    monkeypatch.setenv("CD_TEST_LIMIT", "1O0")
    with pytest.raises(ValueError):
        _int_env("CD_TEST_LIMIT", 10)
