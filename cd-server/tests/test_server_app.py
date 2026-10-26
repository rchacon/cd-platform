import pytest

from cd.server.app import require_cd_api_db_config


def test_unset_cd_api_pghost_outside_local_fails_fast():
    with pytest.raises(RuntimeError, match="CD_API_PGHOST must be set"):
        require_cd_api_db_config("production", {})


def test_set_cd_api_pghost_outside_local_passes():
    require_cd_api_db_config("production", {"CD_API_PGHOST": "db.internal"})


def test_local_dev_without_cd_api_pghost_passes():
    # localhost is the right default for make start-server.
    require_cd_api_db_config("local", {})
