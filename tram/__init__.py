"""TRAM — Trishul Real-time Adapter & Mapper."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tram")
except PackageNotFoundError:
    # Running directly from source tree without installing the package
    __version__ = "0.0.0-dev"
