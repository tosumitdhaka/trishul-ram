"""Import all serializers to trigger @register_serializer decorators."""

from tram.serializers.avro_serializer import AvroSerializer  # noqa: F401
from tram.serializers.bytes_serializer import BytesSerializer  # noqa: F401
from tram.serializers.csv_serializer import CsvSerializer  # noqa: F401
from tram.serializers.json_serializer import JsonSerializer  # noqa: F401
from tram.serializers.msgpack_serializer import MsgpackSerializer  # noqa: F401
from tram.serializers.ndjson_serializer import NdjsonSerializer  # noqa: F401
from tram.serializers.parquet_serializer import ParquetSerializer  # noqa: F401
from tram.serializers.protobuf_serializer import ProtobufSerializer  # noqa: F401
from tram.serializers.text_serializer import TextSerializer  # noqa: F401
from tram.serializers.xml_serializer import XmlSerializer  # noqa: F401
