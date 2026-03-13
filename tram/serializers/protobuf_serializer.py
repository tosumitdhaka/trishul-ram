"""Protobuf serializer — compiles .proto at runtime via grpcio-tools."""
from __future__ import annotations
import atexit
import io
import os
import shutil
import struct
import sys
import tempfile
from tram.core.exceptions import SerializerError
from tram.interfaces.base_serializer import BaseSerializer
from tram.registry.registry import register_serializer

# Cache: (schema_file_abs, mtime) -> compiled module
_MODULE_CACHE: dict[tuple[str, float], object] = {}

@register_serializer("protobuf")
class ProtobufSerializer(BaseSerializer):
    """Serialize/deserialize Protobuf data using runtime .proto compilation.
    Requires protobuf>=4.25 and grpcio-tools>=1.64.
    Wire format: length-delimited framing [4-byte BE length][proto bytes] per record.

    Optional Schema Registry config:
        schema_registry_url (str): Base URL.
        schema_registry_subject (str): Subject for latest lookup.
        schema_registry_id (int): Specific schema ID.
        use_magic_bytes (bool): Strip/prepend Confluent framing. Default True.
    """
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        if "schema_file" not in config:
            raise SerializerError("Protobuf serializer requires 'schema_file' config")
        if "message_class" not in config:
            raise SerializerError("Protobuf serializer requires 'message_class' config")
        self.schema_file: str = os.path.abspath(config["schema_file"])
        self.message_class: str = config["message_class"]
        self.framing: str = config.get("framing", "length_delimited")
        self.registry_url: str | None = config.get("schema_registry_url")
        self.registry_subject: str | None = config.get("schema_registry_subject")
        self.registry_id: int | None = config.get("schema_registry_id")
        self.use_magic_bytes: bool = config.get("use_magic_bytes", True)
        self._tmpdir: str | None = None
        self._module = None
        self._registry_schema_id: int | None = None

    def _compile_proto(self):
        try:
            from grpc_tools import protoc
        except ImportError as exc:
            raise SerializerError(
                "Protobuf serializer requires grpcio-tools — install with: pip install tram[protobuf_ser]"
            ) from exc
        try:
            import google.protobuf  # noqa: F401
        except ImportError as exc:
            raise SerializerError(
                "Protobuf serializer requires protobuf — install with: pip install tram[protobuf_ser]"
            ) from exc

        schema_abs = self.schema_file
        if not os.path.isfile(schema_abs):
            raise SerializerError(f"Proto schema file not found: {schema_abs}")

        mtime = os.path.getmtime(schema_abs)
        cache_key = (schema_abs, mtime)
        if cache_key in _MODULE_CACHE:
            return _MODULE_CACHE[cache_key]

        proto_dir = os.path.dirname(schema_abs)
        proto_filename = os.path.basename(schema_abs)

        tmpdir = tempfile.mkdtemp(prefix="tram_proto_")
        self._tmpdir = tmpdir
        atexit.register(shutil.rmtree, tmpdir, True)

        # Compile the entry-point schema AND all other .proto files in the same
        # directory so that import statements (e.g. import "Custom.proto") resolve
        # at Python import time.
        import glob as _glob
        all_protos = _glob.glob(os.path.join(proto_dir, "*.proto"))
        if not all_protos:
            all_protos = [schema_abs]

        ret = protoc.main([
            "",
            f"-I{proto_dir}",
            f"--python_out={tmpdir}",
        ] + all_protos)
        if ret != 0:
            raise SerializerError(f"protoc compilation failed (exit code {ret}) for {schema_abs}")

        if tmpdir not in sys.path:
            sys.path.insert(0, tmpdir)

        module_name = proto_filename.replace(".proto", "_pb2")
        import importlib
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise SerializerError(f"Failed to import compiled proto module '{module_name}': {exc}") from exc

        _MODULE_CACHE[cache_key] = module
        return module

    def _get_message_class(self):
        module = self._compile_proto()
        try:
            return getattr(module, self.message_class)
        except AttributeError:
            raise SerializerError(
                f"Message class '{self.message_class}' not found in compiled proto module"
            )

    def _get_registry_schema_id(self) -> int | None:
        """Resolve registry schema ID if configured."""
        if not self.registry_url:
            return None
        if self._registry_schema_id is not None:
            return self._registry_schema_id

        from tram.schema_registry.client import SchemaRegistryClient
        client = SchemaRegistryClient(self.registry_url)
        try:
            if self.registry_id is not None:
                # Just confirm it exists and cache it
                client.get_schema_by_id(self.registry_id)
                self._registry_schema_id = self.registry_id
            elif self.registry_subject:
                self._registry_schema_id, _ = client.get_latest_schema(self.registry_subject)
        finally:
            client.close()
        return self._registry_schema_id

    def parse(self, data: bytes) -> list[dict]:
        try:
            from google.protobuf.json_format import MessageToDict
        except ImportError as exc:
            raise SerializerError("Protobuf serializer requires protobuf") from exc

        # Strip magic bytes if applicable
        if self.use_magic_bytes and self.registry_url and len(data) >= 5 and data[0:1] == b"\x00":
            from tram.schema_registry.client import decode_magic
            _, data = decode_magic(data)

        MsgClass = self._get_message_class()

        # framing=none: entire file is a single serialized message (no length prefix)
        if self.framing == "none":
            try:
                msg = MsgClass()
                msg.ParseFromString(data)
                return [MessageToDict(msg)]
            except Exception as exc:
                raise SerializerError(f"Protobuf parse error: {exc}") from exc

        # framing=length_delimited (default): [4-byte BE length][proto bytes] per record
        records = []
        buf = io.BytesIO(data)
        while True:
            length_bytes = buf.read(4)
            if not length_bytes:
                break
            if len(length_bytes) < 4:
                raise SerializerError("Truncated length prefix in protobuf stream")
            (length,) = struct.unpack(">I", length_bytes)
            proto_bytes = buf.read(length)
            if len(proto_bytes) < length:
                raise SerializerError("Truncated protobuf record")
            try:
                msg = MsgClass()
                msg.ParseFromString(proto_bytes)
                records.append(MessageToDict(msg))
            except Exception as exc:
                raise SerializerError(f"Protobuf parse error: {exc}") from exc
        return records

    def serialize(self, records: list[dict]) -> bytes:
        try:
            from google.protobuf.json_format import ParseDict
        except ImportError as exc:
            raise SerializerError("Protobuf serializer requires protobuf") from exc
        MsgClass = self._get_message_class()
        buf = io.BytesIO()
        for rec in records:
            try:
                msg = ParseDict(rec, MsgClass())
                proto_bytes = msg.SerializeToString()
                buf.write(struct.pack(">I", len(proto_bytes)))
                buf.write(proto_bytes)
            except Exception as exc:
                raise SerializerError(f"Protobuf serialize error: {exc}") from exc

        payload = buf.getvalue()

        # Add magic bytes if using registry
        if self.use_magic_bytes and self.registry_url:
            schema_id = self._get_registry_schema_id()
            if schema_id is not None:
                from tram.schema_registry.client import encode_with_magic
                payload = encode_with_magic(schema_id, payload)

        return payload
