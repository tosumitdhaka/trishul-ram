"""Import all transforms to trigger @register_transform decorators."""

from tram.transforms.add_field import AddFieldTransform  # noqa: F401
from tram.transforms.aggregate import AggregateTransform  # noqa: F401
from tram.transforms.cast import CastTransform  # noqa: F401
from tram.transforms.drop import DropTransform  # noqa: F401
from tram.transforms.enrich import EnrichTransform  # noqa: F401
from tram.transforms.filter_rows import FilterRowsTransform  # noqa: F401
from tram.transforms.flatten import FlattenTransform  # noqa: F401
from tram.transforms.rename import RenameTransform  # noqa: F401
from tram.transforms.timestamp_normalize import TimestampNormalizeTransform  # noqa: F401
from tram.transforms.value_map import ValueMapTransform  # noqa: F401
