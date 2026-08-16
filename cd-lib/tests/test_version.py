from cd.lib.version import read_version


def test_read_version_returns_dev_when_missing(tmp_path):
    assert read_version(tmp_path) == "dev"


def test_read_version_reads_and_strips_file(tmp_path):
    (tmp_path / "VERSION").write_text("1.2.3\n")
    assert read_version(tmp_path) == "1.2.3"
