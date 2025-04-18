# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import json
from collections.abc import Mapping
from collections.abc import Sequence
from textwrap import dedent
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple
from typing import Union
from urllib.parse import unquote_plus

from sqlalchemy import exc
from sqlalchemy import sql
from sqlalchemy.engine import Engine
from sqlalchemy.engine.base import Connection
from sqlalchemy.engine.default import DefaultDialect
from sqlalchemy.engine.default import DefaultExecutionContext
from sqlalchemy.engine.url import URL
from sqlalchemy.sql import sqltypes

from .datatype import JSONIndexType
from .datatype import JSONPathType
from trino import dbapi as trino_dbapi
from trino import logging
from trino.auth import BasicAuthentication
from trino.auth import CertificateAuthentication
from trino.auth import JWTAuthentication
from trino.auth import OAuth2Authentication
from trino.dbapi import Cursor
from trino.sqlalchemy import compiler
from trino.sqlalchemy import datatype
from trino.sqlalchemy import error

logger = logging.get_logger(__name__)

colspecs = {
    sqltypes.JSON.JSONIndexType: JSONIndexType,
    sqltypes.JSON.JSONPathType: JSONPathType,
}


class TrinoDialect(DefaultDialect):
    def __init__(self,
                 json_serializer=None,
                 json_deserializer=None,
                 **kwargs):
        DefaultDialect.__init__(self, **kwargs)
        self._json_serializer = json_serializer
        self._json_deserializer = json_deserializer

    name = "trino"
    driver = "rest"

    statement_compiler = compiler.TrinoSQLCompiler
    ddl_compiler = compiler.TrinoDDLCompiler
    type_compiler = compiler.TrinoTypeCompiler
    preparer = compiler.TrinoIdentifierPreparer

    # Data Type
    supports_native_enum = False
    supports_native_boolean = True
    supports_native_decimal = True

    # Column options
    supports_sequences = False
    supports_comments = True
    inline_comments = True
    supports_default_values = False

    # DDL
    supports_alter = True

    # DML
    # Queries of the form `INSERT () VALUES ()` is not supported by Trino.
    supports_empty_insert = False
    supports_multivalues_insert = True
    postfetch_lastrowid = False

    # Caching
    # Warnings are generated by SQLAlchmey if this flag is not explicitly set
    # and tests are needed before being enabled
    supports_statement_cache = False

    # Support proper ordering of CTEs in regard to an INSERT statement
    cte_follows_insert = True
    colspecs = colspecs

    @classmethod
    def dbapi(cls):
        """
        ref: https://www.python.org/dev/peps/pep-0249/#module-interface
        """
        return trino_dbapi

    @classmethod
    def import_dbapi(cls):
        """
        ref: https://www.python.org/dev/peps/pep-0249/#module-interface
        """
        return trino_dbapi

    def create_connect_args(self, url: URL) -> Tuple[Sequence[Any], Mapping[str, Any]]:
        args: Sequence[Any] = list()
        kwargs: Dict[str, Any] = dict(host=url.host)

        if url.port:
            kwargs["port"] = url.port

        db_parts = (url.database or "system").split("/")
        if len(db_parts) == 1:
            kwargs["catalog"] = unquote_plus(db_parts[0])
        elif len(db_parts) == 2:
            kwargs["catalog"] = unquote_plus(db_parts[0])
            kwargs["schema"] = unquote_plus(db_parts[1])
        else:
            raise ValueError(f"Unexpected database format {url.database}")

        if url.username:
            kwargs["user"] = unquote_plus(url.username)

        if url.password:
            if not url.username:
                raise ValueError("Username is required when specify password in connection URL")
            kwargs["auth"] = BasicAuthentication(unquote_plus(url.username), unquote_plus(url.password))

        if "access_token" in url.query:
            kwargs["auth"] = JWTAuthentication(unquote_plus(url.query["access_token"]))

        if "cert" in url.query and "key" in url.query:
            kwargs["auth"] = CertificateAuthentication(unquote_plus(url.query['cert']), unquote_plus(url.query['key']))

        if "externalAuthentication" in url.query:
            kwargs["auth"] = OAuth2Authentication()

        if "source" in url.query:
            kwargs["source"] = unquote_plus(url.query["source"])
        else:
            kwargs["source"] = "trino-sqlalchemy"

        if "session_properties" in url.query:
            kwargs["session_properties"] = json.loads(unquote_plus(url.query["session_properties"]))

        if "http_headers" in url.query:
            kwargs["http_headers"] = json.loads(unquote_plus(url.query["http_headers"]))

        if "extra_credential" in url.query:
            kwargs["extra_credential"] = [
                tuple(extra_credential) for extra_credential in json.loads(unquote_plus(url.query["extra_credential"]))
            ]

        if "client_tags" in url.query:
            kwargs["client_tags"] = json.loads(unquote_plus(url.query["client_tags"]))

        if "legacy_primitive_types" in url.query:
            kwargs["legacy_primitive_types"] = json.loads(unquote_plus(url.query["legacy_primitive_types"]))

        if "legacy_prepared_statements" in url.query:
            kwargs["legacy_prepared_statements"] = json.loads(unquote_plus(url.query["legacy_prepared_statements"]))

        if "verify" in url.query:
            kwargs["verify"] = json.loads(unquote_plus(url.query["verify"]))

        if "roles" in url.query:
            kwargs["roles"] = json.loads(url.query["roles"])

        return args, kwargs

    def get_columns(self, connection: Connection, table_name: str, schema: str = None, **kw) -> List[Dict[str, Any]]:
        if not self.has_table(connection, table_name, schema):
            raise exc.NoSuchTableError(f"schema={schema}, table={table_name}")
        return self._get_columns(connection, table_name, schema, **kw)

    def _get_columns(self, connection: Connection, table_name: str, schema: str = None, **kw) -> List[Dict[str, Any]]:
        schema = schema or self._get_default_schema_name(connection)
        query = dedent(
            """
            SELECT
                "column_name",
                "data_type",
                "column_default",
                UPPER("is_nullable") AS "is_nullable"
            FROM "information_schema"."columns"
            WHERE "table_schema" = :schema
              AND "table_name" = :table
            ORDER BY "ordinal_position" ASC
        """
        ).strip()
        res = connection.execute(sql.text(query), {"schema": schema, "table": table_name})
        columns = []
        for record in res:
            column = dict(
                name=record.column_name,
                type=datatype.parse_sqltype(record.data_type),
                nullable=record.is_nullable == "YES",
                default=record.column_default,
            )
            columns.append(column)
        return columns

    def _get_partitions(
        self,
        connection: Connection,
        table_name: str,
        schema: str = None
    ) -> List[Dict[str, List[Any]]]:
        schema = schema or self._get_default_schema_name(connection)
        query = dedent(
            f"""
            SELECT * FROM {schema}."{table_name}$partitions"
        """
        ).strip()
        res = connection.execute(sql.text(query))
        partition_names = [desc[0] for desc in res.cursor.description]
        return partition_names

    def get_pk_constraint(self, connection: Connection, table_name: str, schema: str = None, **kw) -> Dict[str, Any]:
        """Trino has no support for primary keys. Returns a dummy"""
        return dict(name=None, constrained_columns=[])

    def get_primary_keys(self, connection: Connection, table_name: str, schema: str = None, **kw) -> List[str]:
        pk = self.get_pk_constraint(connection, table_name, schema)
        return pk.get("constrained_columns")  # type: ignore

    def get_foreign_keys(
        self, connection: Connection, table_name: str, schema: str = None, **kw
    ) -> List[Dict[str, Any]]:
        """Trino has no support for foreign keys. Returns an empty list."""
        return []

    def get_catalog_names(self, connection: Connection, **kw) -> List[str]:
        query = dedent(
            """
            SELECT "table_cat"
            FROM "system"."jdbc"."catalogs"
        """
        ).strip()
        res = connection.execute(sql.text(query))
        return [row.table_cat for row in res]

    def get_schema_names(self, connection: Connection, **kw) -> List[str]:
        query = dedent(
            """
            SELECT "schema_name"
            FROM "information_schema"."schemata"
        """
        ).strip()
        res = connection.execute(sql.text(query))
        return [row.schema_name for row in res]

    def get_table_names(self, connection: Connection, schema: str = None, **kw) -> List[str]:
        schema = schema or self._get_default_schema_name(connection)
        if schema is None:
            raise exc.NoSuchTableError("schema is required")
        query = dedent(
            """
            SELECT "table_name"
            FROM "information_schema"."tables"
            WHERE "table_schema" = :schema
              AND "table_type" = 'BASE TABLE'
        """
        ).strip()
        res = connection.execute(sql.text(query), {"schema": schema})
        return [row.table_name for row in res]

    def get_temp_table_names(self, connection: Connection, schema: str = None, **kw) -> List[str]:
        """Trino has no support for temporary tables. Returns an empty list."""
        return []

    def get_view_names(self, connection: Connection, schema: str = None, **kw) -> List[str]:
        schema = schema or self._get_default_schema_name(connection)
        if schema is None:
            raise exc.NoSuchTableError("schema is required")

        # Querying the information_schema.views table is subpar as it compiles the view definitions.
        query = dedent(
            """
            SELECT "table_name"
            FROM "information_schema"."tables"
            WHERE "table_schema" = :schema
              AND "table_type" = 'VIEW'
        """
        ).strip()
        res = connection.execute(sql.text(query), {"schema": schema})
        return [row.table_name for row in res]

    def get_temp_view_names(self, connection: Connection, schema: str = None, **kw) -> List[str]:
        """Trino has no support for temporary views. Returns an empty list."""
        return []

    def get_view_definition(self, connection: Connection, view_name: str, schema: str = None, **kw) -> str:
        schema = schema or self._get_default_schema_name(connection)
        if schema is None:
            raise exc.NoSuchTableError("schema is required")
        query = dedent(
            """
            SELECT "view_definition"
            FROM "information_schema"."views"
            WHERE "table_schema" = :schema
              AND "table_name" = :view
        """
        ).strip()
        res = connection.execute(sql.text(query), {"schema": schema, "view": view_name})
        return res.scalar()

    def get_indexes(self, connection: Connection, table_name: str, schema: str = None, **kw) -> List[Dict[str, Any]]:
        if not self.has_table(connection, table_name, schema):
            raise exc.NoSuchTableError(f"schema={schema}, table={table_name}")

        partitioned_columns = None
        try:
            partitioned_columns = self._get_partitions(connection, f"{table_name}", schema)
        except Exception as e:
            # e.g. it's not a Hive table or an unpartitioned Hive table
            logger.debug("Couldn't fetch partition columns. schema: %s, table: %s, error: %s", schema, table_name, e)
        if not partitioned_columns:
            return []
        partition_index = dict(
            name="partition",
            column_names=partitioned_columns,
            unique=False
        )
        return [partition_index]

    def get_sequence_names(self, connection: Connection, schema: str = None, **kw) -> List[str]:
        """Trino has no support for sequences. Returns an empty list."""
        return []

    def get_unique_constraints(
        self, connection: Connection, table_name: str, schema: str = None, **kw
    ) -> List[Dict[str, Any]]:
        """Trino has no support for unique constraints. Returns an empty list."""
        return []

    def get_check_constraints(
        self, connection: Connection, table_name: str, schema: str = None, **kw
    ) -> List[Dict[str, Any]]:
        """Trino has no support for check constraints. Returns an empty list."""
        return []

    def get_table_comment(self, connection: Connection, table_name: str, schema: str = None, **kw) -> Dict[str, Any]:
        catalog_name = self._get_default_catalog_name(connection)
        if catalog_name is None:
            raise exc.NoSuchTableError("catalog is required in connection")
        schema_name = schema or self._get_default_schema_name(connection)
        if schema_name is None:
            raise exc.NoSuchTableError("schema is required")
        query = dedent(
            """
            SELECT "comment"
            FROM "system"."metadata"."table_comments"
            WHERE "catalog_name" = :catalog_name
              AND "schema_name" = :schema_name
              AND "table_name" = :table_name
        """
        ).strip()
        try:
            res = connection.execute(
                sql.text(query),
                {"catalog_name": catalog_name, "schema_name": schema_name, "table_name": table_name}
            )
            return dict(text=res.scalar())
        except error.TrinoQueryError as e:
            if e.error_name in (
                error.PERMISSION_DENIED,
            ):
                return dict(text=None)
            raise

    def has_schema(self, connection: Connection, schema: str) -> bool:
        query = dedent(
            """
            SELECT "schema_name"
            FROM "information_schema"."schemata"
            WHERE "schema_name" = :schema
        """
        ).strip()
        res = connection.execute(sql.text(query), {"schema": schema})
        return res.first() is not None

    def has_table(self, connection: Connection, table_name: str, schema: str = None, **kw) -> bool:
        schema = schema or self._get_default_schema_name(connection)
        if schema is None:
            return False
        query = dedent(
            """
            SELECT "table_name"
            FROM "information_schema"."tables"
            WHERE "table_schema" = :schema
              AND "table_name" = :table
        """
        ).strip()
        res = connection.execute(sql.text(query), {"schema": schema, "table": table_name})
        return res.first() is not None

    def has_sequence(self, connection: Connection, sequence_name: str, schema: str = None, **kw) -> bool:
        """Trino has no support for sequence. Returns False indicate that given sequence does not exists."""
        return False

    @classmethod
    def _get_server_version_info(cls, connection: Connection) -> Any:
        def get_server_version_info(_):
            query = "SELECT version()"
            try:
                res = connection.execute(sql.text(query))
                version = res.scalar()
                return tuple([version])
            except exc.ProgrammingError as e:
                logger.debug(f"Failed to get server version: {e.orig.message}")
                return None

        # Make server_version_info lazy in order to only make HTTP calls if user explicitly requests it.
        cls.server_version_info = property(get_server_version_info, lambda instance, value: None)

    def _raw_connection(self, connection: Union[Engine, Connection]) -> trino_dbapi.Connection:
        if isinstance(connection, Engine):
            return connection.raw_connection()
        return connection.connection

    def _get_default_catalog_name(self, connection: Connection) -> Optional[str]:
        dbapi_connection: trino_dbapi.Connection = self._raw_connection(connection)
        return dbapi_connection.catalog

    def _get_default_schema_name(self, connection: Connection) -> Optional[str]:
        dbapi_connection: trino_dbapi.Connection = self._raw_connection(connection)
        return dbapi_connection.schema

    def do_execute(
        self, cursor: Cursor, statement: str, parameters: Tuple[Any, ...], context: DefaultExecutionContext = None
    ):
        cursor.execute(statement, parameters)

    def do_rollback(self, dbapi_connection: trino_dbapi.Connection):
        if dbapi_connection.transaction is not None:
            dbapi_connection.rollback()

    def set_isolation_level(self, dbapi_conn: trino_dbapi.Connection, level: str) -> None:
        dbapi_conn._isolation_level = trino_dbapi.IsolationLevel[level]

    def get_isolation_level(self, dbapi_conn: trino_dbapi.Connection) -> str:
        return dbapi_conn.isolation_level.name

    def get_default_isolation_level(self, dbapi_conn: trino_dbapi.Connection) -> str:
        return trino_dbapi.IsolationLevel.AUTOCOMMIT.name

    def _get_full_table(self, table_name: str, schema: str = None, quote: bool = True) -> str:
        table_part = self.identifier_preparer.quote_identifier(table_name) if quote else table_name
        if schema:
            schema_part = self.identifier_preparer.quote_identifier(schema) if quote else schema
            return f"{schema_part}.{table_part}"

        return table_part
