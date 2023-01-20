"""
# EngineAdapter

Engine adapters are how SQLMesh connects and interacts with various data stores. They allow SQLMesh to
generalize its functionality to different engines that have Python Database API 2.0-compliant
connections. Rather than executing queries directly against your data stores, SQLMesh components such as
the SnapshotEvaluator delegate them to engine adapters so these components can be engine-agnostic.
"""
from __future__ import annotations

import contextlib
import itertools
import logging
import typing as t

import pandas as pd
from sqlglot import Dialect, exp, parse_one

from sqlmesh.core.dialect import pandas_to_sql
from sqlmesh.core.engine_adapter._typing import (
    DF_TYPES,
    QUERY_TYPES,
    SOURCE_ALIAS,
    TARGET_ALIAS,
    PySparkDataFrame,
    PySparkSession,
    Query,
)
from sqlmesh.core.engine_adapter.shared import TransactionType
from sqlmesh.core.model.kind import TimeColumn
from sqlmesh.utils import optional_import
from sqlmesh.utils.connection_pool import create_connection_pool
from sqlmesh.utils.date import TimeLike, make_inclusive
from sqlmesh.utils.errors import SQLMeshError

if t.TYPE_CHECKING:
    from sqlmesh.core._typing import TableName
    from sqlmesh.core.engine_adapter._typing import DF, QueryOrDF

logger = logging.getLogger(__name__)


class EngineAdapter:
    """Base class wrapping a Database API compliant connection.

    The EngineAdapter is an easily-subclassable interface that interacts
    with the underlying engine and data store.

    Args:
        connection_factory: a callable which produces a new Database API-compliant
            connection on every call.
        dialect: The dialect with which this adapter is associated.
        multithreaded: Indicates whether this adapter will be used by more than one thread.
    """

    DIALECT = ""
    DEFAULT_BATCH_SIZE = 10000
    DEFAULT_SQL_GEN_KWARGS: t.Dict[str, str | bool | int] = {}

    def __init__(
        self,
        connection_factory: t.Callable[[], t.Any],
        dialect: str = "",
        sql_gen_kwargs: t.Optional[t.Dict[str, Dialect | bool | str]] = None,
        multithreaded: bool = False,
    ):
        self.dialect = dialect.lower() or self.DIALECT
        self._connection_pool = create_connection_pool(
            connection_factory, multithreaded
        )
        self._transaction = False
        self.sql_gen_kwargs = sql_gen_kwargs or {}

    @property
    def cursor(self) -> t.Any:
        return self._connection_pool.get_cursor()

    @property
    def spark(self) -> t.Optional[PySparkSession]:
        return None

    def recycle(self) -> t.Any:
        """Closes all open connections and releases all allocated resources associated with any thread
        except the calling one."""
        self._connection_pool.close_all(exclude_calling_thread=True)

    def close(self) -> t.Any:
        """Closes all open connections and releases all allocated resources."""
        self._connection_pool.close_all()

    def replace_query(
        self,
        table_name: TableName,
        query_or_df: QueryOrDF,
        columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
    ) -> None:
        """Replaces an existing table with a query.

        For partition based engines (hive, spark), insert override is used. For other systems, create or replace is used.

        Args:
            table_name: The name of the table (eg. prod.table)
            query_or_df: The SQL query to run or a dataframe.
            columns_to_types: Only used if a dataframe is provided. A mapping between the column name and its data type.
                Expected to be ordered to match the order of values in the dataframe.
        """
        table = exp.to_table(table_name)
        if isinstance(query_or_df, pd.DataFrame):
            if not columns_to_types:
                raise ValueError("columns_to_types must be provided for dataframes")
            expression = next(
                self._pandas_to_sql(
                    query_or_df,
                    alias=table.alias_or_name,
                    columns_to_types=columns_to_types,
                )
            )
            create = exp.Create(
                this=table,
                kind="TABLE",
                replace=True,
                expression=expression,
            )
        else:
            create = exp.Create(
                this=table,
                kind="TABLE",
                replace=True,
                expression=query_or_df,
            )
        self.execute(create)

    def create_table(
        self,
        table_name: TableName,
        query_or_columns_to_types: Query | t.Dict[str, exp.DataType],
        exists: bool = True,
        **kwargs: t.Any,
    ) -> None:
        """Create a table using a DDL statement or a CTAS.

        If a query is passed in instead of column type map, CREATE TABLE AS will be used.

        Args:
            table_name: The name of the table to create. Can be fully qualified or just table name.
            query_or_columns_to_types: A query or mapping between the column name and its data type.
            exists: Indicates whether to include the IF NOT EXISTS check.
            kwargs: Optional create table properties.
        """
        if isinstance(query_or_columns_to_types, dict):
            return self._create_table_from_columns(
                table_name, query_or_columns_to_types, exists, **kwargs
            )
        return self._create_table_from_query(
            table_name, query_or_columns_to_types, exists, **kwargs
        )

    def _create_table_from_columns(
        self,
        table_name: TableName,
        columns_to_types: t.Dict[str, exp.DataType],
        exists: bool = True,
        **kwargs: t.Any,
    ) -> None:
        """
        Create a table using a DDL statement.

        Args:
            table_name: The name of the table to create. Can be fully qualified or just table name.
            columns_to_types: Mapping between the column name and its data type.
            exists: Indicates whether to include the IF NOT EXISTS check.
            kwargs: Optional create table properties.
        """
        properties = self._create_table_properties(**kwargs)
        schema: t.Optional[exp.Schema | exp.Table] = exp.to_table(table_name)
        schema = exp.Schema(
            this=schema,
            expressions=[
                exp.ColumnDef(this=exp.to_identifier(column), kind=kind)
                for column, kind in columns_to_types.items()
            ],
        )
        create_expression = exp.Create(
            this=schema,
            kind="TABLE",
            exists=exists,
            properties=properties,
            expression=None,
        )
        self.execute(create_expression)

    def _create_table_from_query(
        self,
        table_name: TableName,
        query: Query,
        exists: bool = True,
        **kwargs: t.Any,
    ) -> None:
        """
        Create a table using a DDL statement.

        Args:
            table_name: The name of the table to create. Can be fully qualified or just table name.
            query: The query to use for creating the table
            exists: Indicates whether to include the IF NOT EXISTS check.
            kwargs: Optional create table properties.
        """
        properties = self._create_table_properties(**kwargs)
        schema: t.Optional[exp.Schema | exp.Table] = exp.to_table(table_name)
        create_expression = exp.Create(
            this=schema,
            kind="TABLE",
            exists=exists,
            properties=properties,
            expression=query,
        )
        self.execute(create_expression)

    def create_table_like(
        self,
        target_table_name: TableName,
        source_table_name: TableName,
        exists: bool = True,
    ) -> None:
        """
        Create a table like another table or view.
        """
        target_table = exp.to_table(target_table_name)
        source_table = exp.to_table(source_table_name)
        create_expression = exp.Create(
            this=target_table,
            kind="TABLE",
            exists=exists,
            properties=exp.Properties(
                expressions=[
                    exp.LikeProperty(this=source_table),
                ]
            ),
        )
        self.execute(create_expression)

    def drop_table(self, table_name: str, exists: bool = True) -> None:
        """Drops a table.

        Args:
            table_name: The name of the table to drop.
            exists: If exists, defaults to True.
        """
        drop_expression = exp.Drop(this=table_name, kind="TABLE", exists=exists)
        self.execute(drop_expression)

    def alter_table(
        self,
        table_name: TableName,
        added_columns: t.Dict[str, str],
        dropped_columns: t.Sequence[str],
    ) -> None:
        with self.transaction(TransactionType.DDL):
            alter_table = exp.AlterTable(this=exp.to_table(table_name))

            for column_name, column_type in added_columns.items():
                add_column = exp.ColumnDef(
                    this=exp.to_identifier(column_name),
                    kind=parse_one(column_type, into=exp.DataType),  # type: ignore
                )
                alter_table.set("actions", [add_column])

                self.execute(alter_table)

            for column_name in dropped_columns:
                drop_column = exp.Drop(
                    this=exp.column(column_name, quoted=True), kind="COLUMN"
                )
                alter_table.set("actions", [drop_column])

                self.execute(alter_table)

    def create_view(
        self,
        view_name: TableName,
        query_or_df: QueryOrDF,
        columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
        replace: bool = True,
        **create_kwargs: t.Any,
    ) -> None:
        """Create a view with a query or dataframe.

        If a dataframe is passed in, it will be converted into a literal values statement.
        This should only be done if the dataframe is very small!

        Args:
            view_name: The view name.
            query_or_df: A query or dataframe.
            columns_to_types: Columns to use in the view statement.
            replace: Whether or not to replace an existing view defaults to True.
            create_kwargs: Additional kwargs to pass into the Create expression
        """
        schema: t.Optional[exp.Table | exp.Schema] = exp.to_table(view_name)

        if isinstance(query_or_df, DF_TYPES):
            if PySparkDataFrame is not None and isinstance(
                query_or_df, PySparkDataFrame
            ):
                query_or_df = query_or_df.toPandas()

            if not isinstance(query_or_df, pd.DataFrame):
                raise SQLMeshError("Can only create views with pandas dataframes.")

            if not columns_to_types:
                raise SQLMeshError(
                    "Creating a view with a dataframe requires passing in columns_to_types."
                )
            schema = exp.Schema(
                this=schema,
                expressions=[exp.column(column) for column in columns_to_types],
            )
            query_or_df = next(
                self._pandas_to_sql(query_or_df, columns_to_types=columns_to_types)
            )

        self.execute(
            exp.Create(
                this=schema,
                kind="VIEW",
                replace=replace,
                expression=query_or_df,
                **create_kwargs,
            )
        )

    def create_schema(self, schema_name: str, ignore_if_exists: bool = True) -> None:
        """Create a schema from a name or qualified table name."""
        self.execute(
            exp.Create(
                this=exp.to_identifier(schema_name.split(".")[0]),
                kind="SCHEMA",
                exists=ignore_if_exists,
            )
        )

    def drop_schema(
        self, schema_name: str, ignore_if_not_exists: bool = True, cascade: bool = False
    ) -> None:
        """Drop a schema from a name or qualified table name."""
        self.execute(
            exp.Drop(
                this=exp.to_identifier(schema_name.split(".")[0]),
                kind="SCHEMA",
                exists=ignore_if_not_exists,
                cascade=cascade,
            )
        )

    def drop_view(
        self, view_name: TableName, ignore_if_not_exists: bool = True
    ) -> None:
        """Drop a view."""
        self.execute(
            exp.Drop(
                this=exp.to_table(view_name), exists=ignore_if_not_exists, kind="VIEW"
            )
        )

    def columns(self, table_name: TableName) -> t.Dict[str, str]:
        """Fetches column names and types for the target table."""
        self.execute(exp.Describe(this=exp.to_table(table_name), kind="TABLE"))
        describe_output = self.cursor.fetchall()
        return {
            t[0]: t[1].upper()
            for t in itertools.takewhile(
                lambda t: not t[0].startswith("#"),
                describe_output,
            )
        }

    def table_exists(self, table_name: TableName) -> bool:
        try:
            self.execute(exp.Describe(this=exp.to_table(table_name), kind="TABLE"))
            return True
        except Exception:
            return False

    def delete_from(
        self, table_name: TableName, where: t.Union[str, exp.Expression]
    ) -> None:
        self.execute(exp.delete(table_name, where))

    @classmethod
    def _insert_into_expression(
        cls,
        table_name: TableName,
        columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
    ) -> t.Optional[exp.Table] | exp.Schema:
        if not columns_to_types:
            return exp.to_table(table_name)
        return exp.Schema(
            this=exp.to_table(table_name),
            expressions=[exp.column(c) for c in columns_to_types],
        )

    def insert_append(
        self,
        table_name: TableName,
        query_or_df: QueryOrDF,
        columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
    ) -> None:
        if isinstance(query_or_df, QUERY_TYPES):
            query_or_df = t.cast(Query, query_or_df)
            return self._insert_append_query(table_name, query_or_df, columns_to_types)
        if isinstance(query_or_df, pd.DataFrame):
            return self._insert_append_pandas_df(
                table_name, query_or_df, columns_to_types
            )
        raise SQLMeshError(f"Unsupported type for insert_append: {type(query_or_df)}")

    def _insert_append_query(
        self,
        table_name: TableName,
        query: Query,
        columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
    ) -> None:
        self.execute(
            exp.Insert(
                this=self._insert_into_expression(table_name, columns_to_types),
                expression=query,
                overwrite=False,
            )
        )

    def _insert_append_pandas_df(
        self,
        table_name: TableName,
        df: pd.DataFrame,
        columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
    ) -> None:
        connection = self._connection_pool.get()
        table = exp.to_table(table_name)
        into = self._insert_into_expression(table_name, columns_to_types)

        sqlalchemy = optional_import("sqlalchemy")
        # pandas to_sql doesn't support insert overwrite, it only supports deleting the table or appending
        if sqlalchemy and isinstance(connection, sqlalchemy.engine.Connectable):
            df.to_sql(
                table.sql(dialect=self.dialect),
                connection,
                if_exists="append",
                index=False,
                chunksize=self.DEFAULT_BATCH_SIZE,
                method="multi",
            )
        else:
            if not columns_to_types:
                raise SQLMeshError(
                    "Column Mapping must be specified when using a Pandas DataFrame and not using SQLAlchemy"
                )
            with self.transaction():
                for i, expression in enumerate(
                    self._pandas_to_sql(df, columns_to_types, self.DEFAULT_BATCH_SIZE)
                ):
                    self.execute(
                        exp.Insert(
                            this=into,
                            expression=expression,
                            overwrite=False,
                        )
                    )

    def insert_overwrite_by_time_partition(
        self,
        table_name: TableName,
        query_or_df: QueryOrDF,
        start: TimeLike,
        end: TimeLike,
        time_formatter: t.Callable[[TimeLike], exp.Expression],
        time_column: TimeColumn | exp.Column | str,
        columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
    ) -> None:
        low, high = [time_formatter(dt) for dt in make_inclusive(start, end)]
        if isinstance(time_column, TimeColumn):
            time_column = time_column.column
        where = exp.Between(
            this=exp.to_column(time_column),
            low=low,
            high=high,
        )
        return self._insert_overwrite_by_condition(
            table_name, query_or_df, where, columns_to_types
        )

    @classmethod
    def _pandas_to_sql(
        cls,
        df: pd.DataFrame,
        columns_to_types: t.Dict[str, exp.DataType],
        batch_size: int = 0,
        alias: str = "t",
    ) -> t.Generator[exp.Select, None, None]:
        yield from pandas_to_sql(df, columns_to_types, batch_size, alias)

    def _insert_overwrite_by_condition(
        self,
        table_name: TableName,
        query_or_df: QueryOrDF,
        where: t.Optional[exp.Condition] = None,
        columns_to_types: t.Optional[t.Dict[str, exp.DataType]] = None,
    ) -> None:
        if where is None:
            raise SQLMeshError(
                "Where condition is required when doing a delete/insert for insert/overwrite"
            )
        with self.transaction():
            self.delete_from(table_name, where=where)
            self.insert_append(
                table_name, query_or_df, columns_to_types=columns_to_types
            )

    def update_table(
        self,
        table_name: TableName,
        properties: t.Optional[t.Dict[str, t.Any]] = None,
        where: t.Optional[str | exp.Condition] = None,
    ) -> None:
        self.execute(exp.update(table_name, properties, where=where))

    def merge(
        self,
        target_table: TableName,
        source_table: QueryOrDF,
        column_names: t.Iterable[str],
        unique_key: t.Iterable[str],
    ) -> None:
        this = exp.alias_(exp.to_table(target_table), TARGET_ALIAS)
        using = exp.Subquery(this=source_table, alias=SOURCE_ALIAS)
        on = exp.and_(
            *(
                exp.EQ(
                    this=exp.column(part, TARGET_ALIAS),
                    expression=exp.column(part, SOURCE_ALIAS),
                )
                for part in unique_key
            )
        )
        when_matched = exp.When(
            this="MATCHED",
            then=exp.update(
                None,
                properties={
                    exp.column(col, TARGET_ALIAS): exp.column(col, SOURCE_ALIAS)
                    for col in column_names
                },
            ),
        )
        when_not_matched = exp.When(
            this=exp.Not(this="MATCHED"),
            then=exp.Insert(
                this=exp.Tuple(expressions=[exp.column(col) for col in column_names]),
                expression=exp.Tuple(
                    expressions=[exp.column(col, SOURCE_ALIAS) for col in column_names]
                ),
            ),
        )
        self.execute(
            exp.Merge(
                this=this,
                using=using,
                on=on,
                expressions=[
                    when_matched,
                    when_not_matched,
                ],
            )
        )

    def rename_table(
        self,
        old_table_name: TableName,
        new_table_name: TableName,
    ) -> None:
        self.execute(exp.rename_table(old_table_name, new_table_name))

    def fetchone(self, query: t.Union[exp.Expression, str]) -> t.Tuple:
        self.execute(query)
        return self.cursor.fetchone()

    def fetchall(self, query: t.Union[exp.Expression, str]) -> t.List[t.Tuple]:
        self.execute(query)
        return self.cursor.fetchall()

    def _fetch_native_df(self, query: t.Union[exp.Expression, str]) -> DF:
        """Fetches a DataFrame that can be either Pandas or PySpark from the cursor"""
        self.execute(query)
        return self.cursor.fetchdf()

    def fetchdf(self, query: t.Union[exp.Expression, str]) -> pd.DataFrame:
        """Fetches a Pandas DataFrame from the cursor"""
        df = self._fetch_native_df(query)
        if not isinstance(df, pd.DataFrame):
            raise NotImplementedError(
                "The cursor's `fetch_native_df` method is not returning a pandas DataFrame. Need to update `fetchdf` so a Pandas DataFrame is returned"
            )
        return df

    def fetch_pyspark_df(self, query: t.Union[exp.Expression, str]) -> PySparkDataFrame:
        """Fetches a PySpark DataFrame from the cursor"""
        raise NotImplementedError(
            f"Engine does not support PySpark DataFrames: {type(self)}"
        )

    @contextlib.contextmanager
    def transaction(
        self, transaction_type: TransactionType = TransactionType.DML
    ) -> t.Generator[None, None, None]:
        """A transaction context manager."""
        if self._transaction or not self.supports_transactions(transaction_type):
            yield
            return
        self._transaction = True
        self.execute(exp.Transaction())
        try:
            yield
        except Exception as e:
            self.execute(exp.Rollback())
            raise e
        else:
            self.execute(exp.Commit())
        finally:
            self._transaction = False

    def supports_transactions(self, transaction_type: TransactionType) -> bool:
        """Whether or not the engine adapter supports transactions for the given transaction type."""
        return True

    def execute(self, sql: t.Union[str, exp.Expression], **kwargs: t.Any) -> None:
        """Execute a sql query."""
        sql = self._to_sql(sql) if isinstance(sql, exp.Expression) else sql
        logger.debug(f"Executing SQL:\n{sql}")
        self.cursor.execute(sql, **kwargs)

    def _create_table_properties(
        self,
        storage_format: t.Optional[str] = None,
        partitioned_by: t.Optional[t.List[str]] = None,
    ) -> t.Optional[exp.Properties]:
        return None

    def _to_sql(self, e: exp.Expression, **kwargs: t.Any) -> str:
        """
        Converts an expression to a SQL string. Has a set of default kwargs to apply, and then default
        kwargs defined for the given dialect, and then kwargs provided by the user when defining the engine
        adapter, and then finally kwargs provided by the user when calling this method.
        """
        sql_gen_kwargs = {
            "dialect": self.dialect,
            "pretty": False,
            "comments": False,
            "identify": True,
            **self.DEFAULT_SQL_GEN_KWARGS,
            **self.sql_gen_kwargs,
            **kwargs,
        }
        return e.sql(**sql_gen_kwargs)  # type: ignore