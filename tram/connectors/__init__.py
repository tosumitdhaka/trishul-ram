"""Import all connectors to trigger registration decorators."""

from tram.connectors.sftp import sink as _sftp_sink  # noqa: F401
from tram.connectors.sftp import source as _sftp_source  # noqa: F401
