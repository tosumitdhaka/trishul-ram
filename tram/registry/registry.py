"""Plugin registry — decorator-based registration of sources, sinks, transforms, serializers."""

from __future__ import annotations

import logging
from typing import Type

from tram.core.exceptions import PluginNotFoundError
from tram.interfaces.base_serializer import BaseSerializer
from tram.interfaces.base_sink import BaseSink
from tram.interfaces.base_source import BaseSource
from tram.interfaces.base_transform import BaseTransform

logger = logging.getLogger(__name__)

_sources: dict[str, Type[BaseSource]] = {}
_sinks: dict[str, Type[BaseSink]] = {}
_transforms: dict[str, Type[BaseTransform]] = {}
_serializers: dict[str, Type[BaseSerializer]] = {}


# ── Registration decorators ────────────────────────────────────────────────


def register_source(key: str):
    """Class decorator to register a BaseSource implementation."""
    def decorator(cls: Type[BaseSource]) -> Type[BaseSource]:
        _sources[key] = cls
        logger.debug("Registered source: %s → %s", key, cls.__name__)
        return cls
    return decorator


def register_sink(key: str):
    """Class decorator to register a BaseSink implementation."""
    def decorator(cls: Type[BaseSink]) -> Type[BaseSink]:
        _sinks[key] = cls
        logger.debug("Registered sink: %s → %s", key, cls.__name__)
        return cls
    return decorator


def register_transform(key: str):
    """Class decorator to register a BaseTransform implementation."""
    def decorator(cls: Type[BaseTransform]) -> Type[BaseTransform]:
        _transforms[key] = cls
        logger.debug("Registered transform: %s → %s", key, cls.__name__)
        return cls
    return decorator


def register_serializer(key: str):
    """Class decorator to register a BaseSerializer implementation."""
    def decorator(cls: Type[BaseSerializer]) -> Type[BaseSerializer]:
        _serializers[key] = cls
        logger.debug("Registered serializer: %s → %s", key, cls.__name__)
        return cls
    return decorator


# ── Lookup helpers ─────────────────────────────────────────────────────────


def get_source(key: str) -> Type[BaseSource]:
    if key not in _sources:
        raise PluginNotFoundError(f"No source registered for key '{key}'. Available: {list(_sources)}")
    return _sources[key]


def get_sink(key: str) -> Type[BaseSink]:
    if key not in _sinks:
        raise PluginNotFoundError(f"No sink registered for key '{key}'. Available: {list(_sinks)}")
    return _sinks[key]


def get_transform(key: str) -> Type[BaseTransform]:
    if key not in _transforms:
        raise PluginNotFoundError(
            f"No transform registered for key '{key}'. Available: {list(_transforms)}"
        )
    return _transforms[key]


def get_serializer(key: str) -> Type[BaseSerializer]:
    if key not in _serializers:
        raise PluginNotFoundError(
            f"No serializer registered for key '{key}'. Available: {list(_serializers)}"
        )
    return _serializers[key]


def list_plugins() -> dict[str, list[str]]:
    """Return all registered plugin keys by category."""
    return {
        "sources": sorted(_sources.keys()),
        "sinks": sorted(_sinks.keys()),
        "transforms": sorted(_transforms.keys()),
        "serializers": sorted(_serializers.keys()),
    }
