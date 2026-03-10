"""TRAM — Trishul Real-time Aggregation & Mediation."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tram")
except PackageNotFoundError:
    # Running directly from source tree without installing the package
    __version__ = "0.0.0-dev"
