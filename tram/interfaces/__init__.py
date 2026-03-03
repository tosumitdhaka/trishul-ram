"""TRAM plugin interfaces — public API for plugin authors."""

from tram.interfaces.base_serializer import BaseSerializer
from tram.interfaces.base_sink import BaseSink
from tram.interfaces.base_source import BaseSource
from tram.interfaces.base_transform import BaseTransform

__all__ = ["BaseSource", "BaseSink", "BaseTransform", "BaseSerializer"]
