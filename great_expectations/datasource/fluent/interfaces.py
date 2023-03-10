from __future__ import annotations

import dataclasses
import functools
import logging
import re
import uuid
from pprint import pformat as pf
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    ClassVar,
    Dict,
    Generic,
    List,
    MutableMapping,
    Optional,
    Sequence,
    Set,
    Type,
    TypeVar,
    Union,
)

import pandas as pd
import pydantic
from pydantic import Field, StrictBool, StrictInt, root_validator, validate_arguments
from pydantic import dataclasses as pydantic_dc
from typing_extensions import TypeAlias, TypeGuard

from great_expectations.core.id_dict import BatchSpec  # noqa: TCH001
from great_expectations.datasource.fluent.constants import _FIELDS_ALWAYS_SET
from great_expectations.datasource.fluent.fluent_base_model import (
    FluentBaseModel,
)
from great_expectations.datasource.fluent.metadatasource import MetaDatasource
from great_expectations.validator.metrics_calculator import MetricsCalculator

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    # TODO: We should try to import the annotations from core.batch so we no longer need to call
    #  Batch.update_forward_refs() before instantiation.
    from great_expectations.core.batch import (
        BatchData,
        BatchDefinition,
        BatchMarkers,
    )
    from great_expectations.core.config_provider import _ConfigurationProvider
    from great_expectations.datasource.fluent.data_asset.data_connector import (
        DataConnector,
    )
    from great_expectations.datasource.fluent.type_lookup import TypeLookup

try:
    import pyspark
    from pyspark.sql import Row as pyspark_sql_Row
except ImportError:
    pyspark = None  # type: ignore[assignment]
    pyspark_sql_Row = None  # type: ignore[assignment,misc]
    logger.debug("No spark sql dataframe module available.")


class TestConnectionError(Exception):
    pass


# BatchRequestOptions is a dict that is composed into a BatchRequest that specifies the
# Batches one wants as returned. The keys represent dimensions one can slice the data along
# and the values are the realized. If a value is None or unspecified, the batch_request
# will capture all data along this dimension. For example, if we have a year and month
# splitter, and we want to query all months in the year 2020, the batch request options
# would look like:
#   options = { "year": 2020 }
BatchRequestOptions: TypeAlias = Dict[str, Any]


@dataclasses.dataclass(frozen=True)
class BatchRequest:
    datasource_name: str
    data_asset_name: str
    options: BatchRequestOptions


@pydantic_dc.dataclass(frozen=True)
class Sorter:
    key: str
    reverse: bool = False


SortersDefinition: TypeAlias = List[Union[Sorter, str, dict]]


def _is_sorter_list(
    sorters: SortersDefinition,
) -> TypeGuard[list[Sorter]]:
    if len(sorters) == 0 or isinstance(sorters[0], Sorter):
        return True
    return False


def _is_str_sorter_list(sorters: SortersDefinition) -> TypeGuard[list[str]]:
    if len(sorters) > 0 and isinstance(sorters[0], str):
        return True
    return False


def _sorter_from_list(sorters: SortersDefinition) -> list[Sorter]:
    if _is_sorter_list(sorters):
        return sorters

    # mypy doesn't successfully type-narrow sorters to a list[str] here, so we use
    # another TypeGuard. We could cast instead which may be slightly faster.
    sring_valued_sorter: str
    if _is_str_sorter_list(sorters):
        return [
            _sorter_from_str(sring_valued_sorter) for sring_valued_sorter in sorters
        ]

    # This should never be reached because of static typing but is necessary because
    # mypy doesn't know of the if conditions must evaluate to True.
    raise ValueError(f"sorters is a not a SortersDefinition but is a {type(sorters)}")


def _sorter_from_str(sort_key: str) -> Sorter:
    """Convert a list of strings to Sorter objects

    Args:
        sort_key: A batch metadata key which will be used to sort batches on a data asset.
                  This can be prefixed with a + or - to indicate increasing or decreasing
                  sorting.  If not specified, defaults to increasing order.
    """
    if sort_key[0] == "-":
        return Sorter(key=sort_key[1:], reverse=True)

    if sort_key[0] == "+":
        return Sorter(key=sort_key[1:], reverse=False)

    return Sorter(key=sort_key, reverse=False)


# It would be best to bind this to Datasource, but we can't now due to circular dependencies
_DatasourceT = TypeVar("_DatasourceT")


class DataAsset(FluentBaseModel, Generic[_DatasourceT]):
    # To subclass a DataAsset one must define `type` as a Class literal explicitly on the sublass
    # as well as implementing the methods in the `Abstract Methods` section below.
    # Some examples:
    # * type: Literal["MyAssetTypeID"] = "MyAssetTypeID",
    # * type: Literal["table"] = "table"
    # * type: Literal["csv"] = "csv"
    name: str
    type: str
    id: Optional[uuid.UUID] = None

    order_by: List[Sorter] = Field(default_factory=list)

    # non-field private attributes
    _datasource: _DatasourceT = pydantic.PrivateAttr()
    _data_connector: Optional[DataConnector] = pydantic.PrivateAttr(default=None)
    _test_connection_error_message: Optional[str] = pydantic.PrivateAttr(default=None)

    @property
    def datasource(self) -> _DatasourceT:
        return self._datasource

    def test_connection(self) -> None:
        """Test the connection for the DataAsset.

        Raises:
            TestConnectionError: If the connection test fails.
        """
        raise NotImplementedError(
            """One needs to implement "test_connection" on a DataAsset subclass."""
        )

    # Abstract Methods
    def batch_request_options_template(
        self,
    ) -> BatchRequestOptions:
        """A BatchRequestOptions template for build_batch_request.

        Returns:
            A BatchRequestOptions dictionary with the correct shape that build_batch_request
            will understand. All the option values are defaulted to None.
        """
        raise NotImplementedError

    def get_batch_list_from_batch_request(
        self, batch_request: BatchRequest
    ) -> List[Batch]:
        raise NotImplementedError

    # End Abstract Methods

    def build_batch_request(
        self, options: Optional[BatchRequestOptions] = None
    ) -> BatchRequest:
        """A batch request that can be used to obtain batches for this DataAsset.

        Args:
            options: A dict that can be used to limit the number of batches returned from the asset.
                The dict structure depends on the asset type. A template of the dict can be obtained by
                calling batch_request_options_template.

        Returns:
            A BatchRequest object that can be used to obtain a batch list from a Datasource by calling the
            get_batch_list_from_batch_request method.
        """
        raise NotImplementedError(
            """One must implement "build_batch_request" on a DataAsset subclass."""
        )

    def _valid_batch_request_options(self, options: BatchRequestOptions) -> bool:
        return set(options.keys()).issubset(
            set(self.batch_request_options_template().keys())
        )

    def _validate_batch_request(self, batch_request: BatchRequest) -> None:
        """Validates the batch_request has the correct form.

        Args:
            batch_request: A batch request object to be validated.
        """
        raise NotImplementedError(
            """One must implement "_validate_batch_request" on a DataAsset subclass."""
        )

    # Sorter methods
    @pydantic.validator("order_by", pre=True)
    def _parse_order_by_sorters(
        cls, order_by: Optional[List[Union[Sorter, str, dict]]] = None
    ) -> List[Sorter]:
        return Datasource.parse_order_by_sorters(order_by=order_by)

    def add_sorters(self: _DataAssetT, sorters: SortersDefinition) -> _DataAssetT:
        """Associates a sorter to this DataAsset

        The passed in sorters will replace any previously associated sorters.
        Batches returned from this DataAsset will be sorted on the batch's
        metadata in the order specified by `sorters`. Sorters work left to right.
        That is, batches will be sorted first by sorters[0].key, then
        sorters[1].key, and so on. If sorter[i].reverse is True, that key will
        sort the batches in descending, as opposed to ascending, order.

        Args:
            sorters: A list of either Sorter objects or strings. The strings
              are a shorthand for Sorter objects and are parsed as follows:
              r'[+-]?.*'
              An optional prefix of '+' or '-' sets Sorter.reverse to
              'False' or 'True' respectively. It is 'False' if no prefix is present.
              The rest of the string gets assigned to the Sorter.key.
              For example:
              ["key1", "-key2", "key3"]
              is equivalent to:
              [
                  Sorter(key="key1", reverse=False),
                  Sorter(key="key2", reverse=True),
                  Sorter(key="key3", reverse=False),
              ]

        Returns:
            This DataAsset with the passed in sorters accessible via self.order_by
        """
        # NOTE: (kilo59) we could use pydantic `validate_assignment` for this
        # https://docs.pydantic.dev/usage/model_config/#options
        self.order_by = _sorter_from_list(sorters)
        return self

    def sort_batches(self, batch_list: List[Batch]) -> None:
        """Sorts batch_list in place in the order configured in this DataAsset.

        Args:
            batch_list: The list of batches to sort in place.
        """
        for sorter in reversed(self.order_by):
            try:
                batch_list.sort(
                    key=functools.cmp_to_key(
                        _sort_batches_with_none_metadata_values(sorter.key)
                    ),
                    reverse=sorter.reverse,
                )
            except KeyError as e:
                raise KeyError(
                    f"Trying to sort {self.name} table asset batches on key {sorter.key} "
                    "which isn't available on all batches."
                ) from e


def _sort_batches_with_none_metadata_values(
    key: str,
) -> Callable[[Batch, Batch], int]:
    def _compare_function(a: Batch, b: Batch) -> int:
        if a.metadata[key] is not None and b.metadata[key] is not None:
            if a.metadata[key] < b.metadata[key]:
                return -1

            if a.metadata[key] > b.metadata[key]:
                return 1

            return 0

        if a.metadata[key] is None and b.metadata[key] is None:
            return 0

        if a.metadata[key] is None:  # b.metadata[key] is not None
            return -1

        if a.metadata[key] is not None:  # b.metadata[key] is None
            return 1

        # This line should never be reached; hence, "ValueError" with corresponding error message is raised.
        raise ValueError(
            f'Unexpected Batch metadata key combination, "{a.metadata[key]}" and "{b.metadata[key]}", was encountered.'
        )

    return _compare_function


# If a Datasource can have more than 1 _DataAssetT, this will need to change.
_DataAssetT = TypeVar("_DataAssetT", bound=DataAsset)


# It would be best to bind this to ExecutionEngine, but we can't now due to circular imports
_ExecutionEngineT = TypeVar("_ExecutionEngineT")


class Datasource(
    FluentBaseModel,
    Generic[_DataAssetT, _ExecutionEngineT],
    metaclass=MetaDatasource,
):
    # To subclass Datasource one needs to define:
    # asset_types
    # type
    # assets
    #
    # The important part of defining `assets` is setting the Dict type correctly.
    # In addition, one must define the methods in the `Abstract Methods` section below.
    # If one writes a class level docstring, this will become the documenation for the
    # data context method `data_context.sources.add_my_datasource` method.

    # class attrs
    asset_types: ClassVar[Sequence[Type[DataAsset]]] = []
    # Datasource instance attrs but these will be fed into the `execution_engine` constructor
    _EXCLUDED_EXEC_ENG_ARGS: ClassVar[Set[str]] = {
        "name",
        "type",
        "execution_engine",
        "assets",
        "base_directory",  # filesystem argument
        "glob_directive",  # filesystem argument
        "data_context_root_directory",  # filesystem argument
        "bucket",  # s3 argument
        "boto3_options",  # s3 argument
        "prefix",  # s3 argument and gcs argument
        "delimiter",  # s3 argument and gcs argument
        "max_keys",  # s3 argument
        "bucket_or_name",  # gcs argument
        "gcs_options",  # gcs argument
        "max_results",  # gcs argument
    }
    _type_lookup: ClassVar[  # This attribute is set in `MetaDatasource.__new__`
        TypeLookup
    ]
    # Setting this in a Datasource subclass will override the execution engine type.
    # The primary use case is to inject an execution engine for testing.
    execution_engine_override: ClassVar[Optional[Type[_ExecutionEngineT]]] = None  # type: ignore[misc]  # ClassVar cannot contain type variables

    # instance attrs
    type: str
    name: str
    id: Optional[uuid.UUID] = None
    assets: MutableMapping[str, _DataAssetT] = {}

    # private attrs
    _data_context = pydantic.PrivateAttr()
    _cached_execution_engine_kwargs: Dict[str, Any] = pydantic.PrivateAttr({})
    _execution_engine: Union[_ExecutionEngineT, None] = pydantic.PrivateAttr(None)
    _config_provider: Union[_ConfigurationProvider, None] = pydantic.PrivateAttr(None)

    @pydantic.validator("assets", each_item=True)
    @classmethod
    def _load_asset_subtype(
        cls: Type[Datasource[_DataAssetT, _ExecutionEngineT]], data_asset: DataAsset
    ) -> _DataAssetT:
        """
        Some `data_asset` may be loaded as a less specific asset subtype different than
        what was intended.
        If a more specific subtype is needed the `data_asset` will be converted to a
        more specific `DataAsset`.
        """
        logger.info(f"Loading '{data_asset.name}' asset ->\n{pf(data_asset, depth=4)}")
        asset_type_name: str = data_asset.type
        asset_type: Type[_DataAssetT] = cls._type_lookup[asset_type_name]

        if asset_type is type(data_asset):
            # asset is already the intended type
            return data_asset

        # strip out asset default kwargs
        kwargs = data_asset.dict(exclude_unset=True)
        logger.debug(f"{asset_type_name} - kwargs\n{pf(kwargs)}")

        asset_of_intended_type = asset_type(**kwargs)
        logger.debug(f"{asset_type_name} - {repr(asset_of_intended_type)}")
        return asset_of_intended_type

    def _execution_engine_type(self) -> Type[_ExecutionEngineT]:
        """Returns the execution engine to be used"""
        return self.execution_engine_override or self.execution_engine_type

    def get_execution_engine(self) -> _ExecutionEngineT:
        current_execution_engine_kwargs = self.dict(
            exclude=self._EXCLUDED_EXEC_ENG_ARGS, config_provider=self._config_provider
        )
        if (
            current_execution_engine_kwargs != self._cached_execution_engine_kwargs
            or not self._execution_engine
        ):
            self._execution_engine = self._execution_engine_type()(
                **current_execution_engine_kwargs
            )
            self._cached_execution_engine_kwargs = current_execution_engine_kwargs
        return self._execution_engine

    def get_batch_list_from_batch_request(
        self, batch_request: BatchRequest
    ) -> List[Batch]:
        """A list of batches that correspond to the BatchRequest.

        Args:
            batch_request: A batch request for this asset. Usually obtained by calling
                build_batch_request on the asset.

        Returns:
            A list of batches that match the options specified in the batch request.
        """
        data_asset = self.get_asset(batch_request.data_asset_name)
        return data_asset.get_batch_list_from_batch_request(batch_request)

    def get_asset(self, asset_name: str) -> _DataAssetT:
        """Returns the DataAsset referred to by name"""
        # This default implementation will be used if protocol is inherited
        try:
            return self.assets[asset_name]
        except KeyError as exc:
            raise LookupError(
                f"'{asset_name}' not found. Available assets are {list(self.assets.keys())}"
            ) from exc

    def add_asset(self, asset: _DataAssetT) -> _DataAssetT:
        """Adds an asset to a datasource

        Args:
            asset: The DataAsset to be added to this datasource.
        """
        # The setter for datasource is non-functional, so we access _datasource directly.
        # See the comment in DataAsset for more information.
        asset._datasource = self

        asset.test_connection()

        self.assets[asset.name] = asset

        # pydantic needs to know that an asset has been set so that it doesn't get excluded
        # when dumping to dict, json, yaml etc.
        self.__fields_set__.update(_FIELDS_ALWAYS_SET)

        return asset

    @staticmethod
    def parse_order_by_sorters(
        order_by: Optional[List[Union[Sorter, str, dict]]] = None
    ) -> List[Sorter]:
        order_by_sorters: list[Sorter] = []
        if order_by:
            for idx, sorter in enumerate(order_by):
                if isinstance(sorter, str):
                    if not sorter:
                        raise ValueError(
                            '"order_by" list cannot contain an empty string'
                        )
                    order_by_sorters.append(_sorter_from_str(sorter))
                elif isinstance(sorter, dict):
                    key: Optional[Any] = sorter.get("key")
                    reverse: Optional[Any] = sorter.get("reverse")
                    if key and reverse:
                        order_by_sorters.append(Sorter(key=key, reverse=reverse))
                    elif key:
                        order_by_sorters.append(Sorter(key=key))
                    else:
                        raise ValueError(
                            '"order_by" list dict must have a key named "key"'
                        )
                else:
                    order_by_sorters.append(sorter)
        return order_by_sorters

    @staticmethod
    def parse_batching_regex_string(
        batching_regex: Optional[Union[re.Pattern, str]] = None
    ) -> re.Pattern:
        pattern: re.Pattern
        if not batching_regex:
            pattern = re.compile(".*")
        elif isinstance(batching_regex, str):
            pattern = re.compile(batching_regex)
        elif isinstance(batching_regex, re.Pattern):
            pattern = batching_regex
        else:
            raise ValueError('"batching_regex" must be either re.Pattern, str, or None')
        return pattern

    # Abstract Methods
    @property
    def execution_engine_type(self) -> Type[_ExecutionEngineT]:
        """Return the ExecutionEngine type use for this Datasource"""
        raise NotImplementedError(
            """One needs to implement "execution_engine_type" on a Datasource subclass."""
        )

    def test_connection(self, test_assets: bool = True) -> None:
        """Test the connection for the Datasource.

        Args:
            test_assets: If assets have been passed to the Datasource, an attempt can be made to test them as well.

        Raises:
            TestConnectionError: If the connection test fails.
        """
        raise NotImplementedError(
            """One needs to implement "test_connection" on a Datasource subclass."""
        )

    def _build_data_connector(self, data_asset_name: str, **kwargs) -> None:
        """Any Datasource subclass that utilizes DataConnector should overwrite this method.

        Specific implementations instantiate appropriate DataConnector class and set "self._data_connector" to it.

        Args:
            data_asset_name: The name of the DataAsset using this DataConnector instance
            kwargs: Extra keyword arguments allow specification of arguments used by particular DataConnector subclasses
        """
        pass

    def _build_test_connection_error_message(
        self, data_asset_name: str, **kwargs
    ) -> None:
        """Any Datasource subclass can overwrite this method.

        Specific implementations create appropriate error message and set "self._test_connection_error_message" to it.

        Args:
            data_asset_name: The name of the DataAsset using this DataConnector instance
            kwargs: Extra keyword arguments allow specification of arguments used by particular subclass' error message
        """
        pass

    # End Abstract Methods


@dataclasses.dataclass(frozen=True)
class HeadData:
    """
    An immutable wrapper around pd.DataFrame for .head() methods which
        are intended to be used for visual inspection of BatchData.
    """

    data: pd.DataFrame

    def __repr__(self) -> str:
        return self.data.__repr__()


class Batch(FluentBaseModel):
    """This represents a batch of data.

    This is usually not the data itself but a hook to the data on an external datastore such as
    a spark or a sql database. An exception exists for pandas or any in-memory datastore.
    """

    datasource: Datasource
    data_asset: DataAsset
    batch_request: BatchRequest
    data: BatchData
    id: str = ""
    # metadata is any arbitrary data one wants to associate with a batch. GX will add arbitrary metadata
    # to a batch so developers may want to namespace any custom metadata they add.
    metadata: Dict[str, Any] = {}

    # TODO: These legacy fields are currently required. They are only used in usage stats so we
    #       should figure out a better way to anonymize and delete them.
    batch_markers: BatchMarkers = Field(..., alias="legacy_batch_markers")
    batch_spec: BatchSpec = Field(..., alias="legacy_batch_spec")
    batch_definition: BatchDefinition = Field(..., alias="legacy_batch_definition")

    class Config:
        allow_mutation = False
        arbitrary_types_allowed = True

    @root_validator(pre=True)
    def _set_id(cls, values: dict) -> dict:
        # We need a unique identifier. This will likely change as we get more input.
        options_list = []
        for key, value in values["batch_request"].options.items():
            if key != "path":
                options_list.append(f"{key}_{value}")

        values["id"] = "-".join(
            [values["datasource"].name, values["data_asset"].name, *options_list]
        )

        return values

    @classmethod
    def update_forward_refs(cls):
        from great_expectations.core.batch import (
            BatchData,
            BatchDefinition,
            BatchMarkers,
        )

        super().update_forward_refs(
            BatchData=BatchData,
            BatchDefinition=BatchDefinition,
            BatchMarkers=BatchMarkers,
        )

    @validate_arguments
    def head(
        self,
        n_rows: StrictInt = 5,
        fetch_all: StrictBool = False,
    ) -> HeadData:
        """Return the first n rows of this Batch.

        This method returns the first n rows for the Batch based on position.

        For negative values of n_rows, this method returns all rows except the last n rows.

        If n_rows is larger than the number of rows, this method returns all rows.

        Parameters
            n_rows: The number of rows to return from the Batch.
            fetch_all: If True, ignore n_rows and return the entire Batch.

        Returns
            HeadData
        """
        self.data.execution_engine.batch_manager.load_batch_list(batch_list=[self])
        metrics_calculator = MetricsCalculator(
            execution_engine=self.data.execution_engine,
            show_progress_bars=True,
        )
        table_head_df: pd.DataFrame = metrics_calculator.head(
            n_rows=n_rows,
            domain_kwargs={"batch_id": self.id},
            fetch_all=fetch_all,
        )
        return HeadData(data=table_head_df.reset_index(drop=True, inplace=False))
