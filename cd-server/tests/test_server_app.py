import logging

from cd.server.app import warn_if_cd_api_db_unconfigured


def test_unset_cd_api_pghost_outside_local_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="cd.server.app"):
        warn_if_cd_api_db_unconfigured("production", {})

    assert "CD_API_PGHOST is unset" in caplog.text


def test_set_cd_api_pghost_outside_local_is_quiet(caplog):
    with caplog.at_level(logging.WARNING, logger="cd.server.app"):
        warn_if_cd_api_db_unconfigured("production", {"CD_API_PGHOST": "db.internal"})

    assert caplog.text == ""


def test_local_dev_without_cd_api_pghost_is_quiet(caplog):
    # localhost is the right default for make start-server.
    with caplog.at_level(logging.WARNING, logger="cd.server.app"):
        warn_if_cd_api_db_unconfigured("local", {})

    assert caplog.text == ""
