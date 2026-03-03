"""TRAM exception hierarchy."""


class TramError(Exception):
    """Base exception for all TRAM errors."""


class ConfigError(TramError):
    """Raised when configuration is invalid or missing."""


class SourceError(TramError):
    """Raised when a source connector fails to read data."""


class SinkError(TramError):
    """Raised when a sink connector fails to write data."""


class TransformError(TramError):
    """Raised when a transform fails to process records."""


class SerializerError(TramError):
    """Raised when a serializer fails to parse or serialize data."""


class PluginNotFoundError(TramError):
    """Raised when a requested plugin key is not registered."""


class PipelineNotFoundError(TramError):
    """Raised when a pipeline name is not registered in the manager."""


class PipelineAlreadyExistsError(TramError):
    """Raised when attempting to register a pipeline that already exists."""
