"""Where Docket keeps the owner's data. Runtime state never lives in the plugin install dir."""

from __future__ import annotations

from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
DEMO_PRICEBOOK = PLUGIN_DIR / "demo_data" / "pricebook.yaml"


def data_dir() -> Path:
    from plugins.plugin_storage import plugin_data_dir
    return Path(plugin_data_dir("docket"))


def pricebook_path() -> tuple[Path, bool]:
    """(path, is_demo). The owner's price book once set up, otherwise the bundled demo book."""
    try:
        return pricebook_path_in(data_dir())
    except Exception:
        return DEMO_PRICEBOOK, True


def pricebook_path_in(folder: Path) -> tuple[Path, bool]:
    """pricebook_path() for a given Docket data folder."""
    own = Path(folder) / "pricebook.yaml"
    return (own, False) if own.exists() else (DEMO_PRICEBOOK, True)
