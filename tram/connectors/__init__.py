"""Import all connectors to trigger registration decorators."""

from tram.connectors.kafka import sink as _kafka_sink  # noqa: F401
from tram.connectors.kafka import source as _kafka_source  # noqa: F401
from tram.connectors.local import sink as _local_sink  # noqa: F401
from tram.connectors.local import source as _local_source  # noqa: F401
from tram.connectors.opensearch import sink as _opensearch_sink  # noqa: F401
from tram.connectors.rest import sink as _rest_sink  # noqa: F401
from tram.connectors.rest import source as _rest_source  # noqa: F401
from tram.connectors.sftp import sink as _sftp_sink  # noqa: F401
from tram.connectors.sftp import source as _sftp_source  # noqa: F401
