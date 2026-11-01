"""Version numbers must match between pyproject.toml and the package."""

from __future__ import annotations

from importlib.metadata import version

import nostrbot_sdk


def test_package_version_matches_installed_distribution() -> None:
    """__version__ is the value consumers see from the installed package."""
    assert nostrbot_sdk.__version__ == version("nostrbot-sdk")


def test_package_version_is_0_6_0() -> None:
    assert nostrbot_sdk.__version__ == "0.9.0"
