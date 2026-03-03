"""Import all serializers to trigger @register_serializer decorators."""

from tram.serializers.csv_serializer import CsvSerializer  # noqa: F401
from tram.serializers.json_serializer import JsonSerializer  # noqa: F401
from tram.serializers.xml_serializer import XmlSerializer  # noqa: F401
