"""Melt transform — pivots a dict field into one record per key/value pair (wide → long)."""

from __future__ import annotations

from tram.core.exceptions import TransformError
from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform


@register_transform("melt")
class MeltTransform(BaseTransform):
    """Pivot a dict-valued field into one record per key/value pair (wide → long format).

    Each output record contains:
      - all fields from the parent record (minus the melted field and any unnested label fields)
      - keys hoisted from each field listed in ``label_fields`` (like unnest, but for multiple fields)
      - a ``metric_name`` column (the key from the melted dict)
      - a ``metric_value`` column (the value from the melted dict)

    This is the standard melt / unpivot operation — useful for producing
    time-series rows from wide SNMP/telemetry records.

    Example::

        input:
          {
            "_metrics": {"ifInOctets": 1000, "ifOutOctets": 2000},
            "_labels":  {"ifIndex": "1", "ifDescr": "lo"},
            "_polled_at": "2026-04-09T10:00:00Z"
          }

        config:
          type: melt
          value_field: _metrics
          label_fields: [_labels]

        output (2 records):
          {"ifIndex": "1", "ifDescr": "lo", "metric_name": "ifInOctets",  "metric_value": 1000,  "_polled_at": "..."}
          {"ifIndex": "1", "ifDescr": "lo", "metric_name": "ifOutOctets", "metric_value": 2000,  "_polled_at": "..."}

    Config keys:
        value_field       (str, required)           Field containing the dict to melt.
        label_fields      (list[str], default [])   Dict fields to unnest as label columns.
                                                    Each must be a dict; its keys are hoisted
                                                    into every output record.
        metric_name_col   (str, default "metric_name")   Column name for the key.
        metric_value_col  (str, default "metric_value")  Column name for the value.
        drop_source       (bool, default True)       Remove value_field and label_fields
                                                     from output records.
        include_only      (list[str], default [])    If set, only melt these keys from
                                                     value_field (others are dropped).
        exclude           (list[str], default [])    Keys from value_field to skip.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        if not config.get("value_field"):
            raise TransformError("melt: 'value_field' is required")
        self.value_field: str = config["value_field"]
        self.label_fields: list[str] = list(config.get("label_fields") or [])
        self.metric_name_col: str = config.get("metric_name_col", "metric_name")
        self.metric_value_col: str = config.get("metric_value_col", "metric_value")
        self.drop_source: bool = bool(config.get("drop_source", True))
        self.include_only: set[str] = set(config.get("include_only") or [])
        self.exclude: set[str] = set(config.get("exclude") or [])

    def apply(self, records: list[dict]) -> list[dict]:
        result = []
        for record in records:
            value_dict = record.get(self.value_field)
            if not isinstance(value_dict, dict):
                # pass through unchanged if field is missing or not a dict
                result.append(record)
                continue

            # Build the base record: parent minus value_field and label_fields
            base = dict(record)
            if self.drop_source:
                base.pop(self.value_field, None)

            # Unnest each label_field into base
            for lf in self.label_fields:
                label_dict = base.pop(lf, None) if self.drop_source else record.get(lf)
                if isinstance(label_dict, dict):
                    base.update(label_dict)
                # if not a dict, leave as-is (don't crash)

            # Emit one record per key in value_dict
            for key, val in value_dict.items():
                if self.include_only and key not in self.include_only:
                    continue
                if key in self.exclude:
                    continue
                row = dict(base)
                row[self.metric_name_col] = key
                row[self.metric_value_col] = val
                result.append(row)

        return result
