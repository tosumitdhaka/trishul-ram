"""JMESPath extract transform — projects fields from records using JMESPath expressions."""

from __future__ import annotations

from tram.core.exceptions import TransformError
from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform


@register_transform("jmespath")
class JmesPathExtractTransform(BaseTransform):
    """Extract or reshape fields using JMESPath expressions.

    Optional dependency: jmespath>=1.0
    Install with: pip install tram[jmespath]

    Config keys:
        fields  (dict[str, str], required)  Mapping of output_field_name → JMESPath expression.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        fields = config.get("fields")
        if not fields:
            raise TransformError("jmespath: 'fields' dict is required and must not be empty")
        self.fields: dict[str, str] = fields
        # Compiled expressions are cached lazily on first apply() call
        self._compiled: dict[str, object] = {}

    def apply(self, records: list[dict]) -> list[dict]:
        try:
            import jmespath as _jmespath
        except ImportError as exc:
            raise TransformError(
                "jmespath transform requires jmespath — install with: pip install tram[jmespath]"
            ) from exc

        # Compile any not-yet-compiled expressions
        for expr in self.fields.values():
            if expr not in self._compiled:
                try:
                    self._compiled[expr] = _jmespath.compile(expr)
                except Exception as exc:  # jmespath.exceptions.ParseError
                    raise TransformError(
                        f"jmespath: failed to compile expression '{expr}': {exc}"
                    ) from exc

        result = []
        for record in records:
            new_record: dict = {}
            for output_field, expr in self.fields.items():
                try:
                    new_record[output_field] = self._compiled[expr].search(record)
                except Exception as exc:
                    raise TransformError(
                        f"jmespath: expression '{expr}' failed: {exc}"
                    ) from exc
            result.append(new_record)
        return result
