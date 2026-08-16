from pathlib import Path


# package_dir is the caller's own Path(__file__).parent, not this module's --
# each component's VERSION file is written alongside its own deployed
# package at release time, not alongside cd-lib.
def read_version(package_dir: Path) -> str:
    try:
        return (package_dir / "VERSION").read_text().strip()
    except FileNotFoundError:
        return "dev"
