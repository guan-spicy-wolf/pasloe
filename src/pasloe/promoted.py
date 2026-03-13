"""Dynamic promoted table management — build, validate, insert, query."""

  from __future__ import annotations

  import re
  from typing import Any
  from uuid import UUID

  from sqlalchemy import (
      Table, Column, String, Integer, Float, Boolean, Text,
      ForeignKey, Index, MetaData, select as sa_select,
  )
  from sqlalchemy.dialects.postgresql import UUID as PG_UUID
  from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


  # Separate MetaData for dynamic tables (isolated from Base.metadata)
  promoted_metadata = MetaData()

  # In-memory cache: table_name -> Table
  _table_cache: dict[str, Table] = {}

  # Schema type string -> SQLAlchemy column type
  SCHEMA_TYPE_MAP = {
      "string": String,
      "integer": Integer,
      "float": Float,
      "boolean": Boolean,
      "text": Text,
  }

  # Python type for strict validation
  PYTHON_TYPE_MAP: dict[str, type | tuple] = {
      "string": str,
      "text": str,
      "integer": int,
      "float": (int, float),
      "boolean": bool,
  }

  # Filter operators for promoted table queries
  FILTER_OPERATORS = {
      "eq": lambda col, val: col == val,
      "gt": lambda col, val: col > val,
      "lt": lambda col, val: col < val,
      "gte": lambda col, val: col >= val,
      "lte": lambda col, val: col <= val,
  }


  # ---------------------------------------------------------------------------
  # Table name generation
  # ---------------------------------------------------------------------------

  def _sanitize(s: str) -> str:
      s = re.sub(r'[^a-zA-Z0-9]', '_', s).lower().strip('_')
      return s[:30]


  def generate_table_name(source_id: str, type_name: str, schema_id: str) -> str:
      short_id = schema_id.replace('-', '')[:8]
      return f"promoted_{_sanitize(source_id)}_{_sanitize(type_name)}_{short_id}"


  # ---------------------------------------------------------------------------
  # Table construction
  # ---------------------------------------------------------------------------

  def build_promoted_table(table_name: str, schema_def: dict) -> Table:
      columns = [
          Column("event_id", PG_UUID(as_uuid=True), ForeignKey("events.id"), primary_key=True),
      ]
      indexes = []

      for field_name, field_def in schema_def.items():
          type_str = field_def["type"] if isinstance(field_def, dict) else field_def
          col_type_cls = SCHEMA_TYPE_MAP.get(type_str.lower())
          if col_type_cls is None:
              raise ValueError(f"Unsupported schema type: {type_str}. Supported: {list(SCHEMA_TYPE_MAP.keys())}")
          columns.append(Column(field_name, col_type_cls, nullable=True))

          should_index = field_def.get("index", False) if isinstance(field_def, dict) else False
          if should_index:
              indexes.append(Index(f"idx_{table_name}_{field_name}", field_name))

      return Table(table_name, promoted_metadata, *columns, *indexes, extend_existing=True)


  def get_or_build_table(table_name: str, schema_def: dict) -> Table:
      if table_name not in _table_cache:
          _table_cache[table_name] = build_promoted_table(table_name, schema_def)
      return _table_cache[table_name]


  def clear_table_cache():
      _table_cache.clear()
      promoted_metadata.clear()


  # ---------------------------------------------------------------------------
  # DDL
  # ---------------------------------------------------------------------------

  async def create_promoted_table(engine: AsyncEngine, table: Table):
      async with engine.begin() as conn:
          await conn.run_sync(table.create, checkfirst=True)


  # ---------------------------------------------------------------------------
  # Validation
  # ---------------------------------------------------------------------------

  def validate_event_data(data: dict, schema_def: dict) -> None:
      for field_name, field_def in schema_def.items():
          type_str = field_def["type"] if isinstance(field_def, dict) else field_def
          if field_name not in data:
              raise ValueError(f"Missing required field '{field_name}' (schema requires: {list(schema_def.keys())})")
          expected = PYTHON_TYPE_MAP.get(type_str)
          if expected and not isinstance(data[field_name], expected):
              raise ValueError(
                  f"Field '{field_name}' has type {type(data[field_name]).__name__}, expected {type_str}"
              )


  # ---------------------------------------------------------------------------
  # Insert
  # ---------------------------------------------------------------------------

  async def insert_promoted_row(
      session: AsyncSession, table: Table, event_id: UUID, data: dict, schema_def: dict
  ):
      row: dict[str, Any] = {"event_id": event_id}
      for field_name in schema_def:
          if field_name in data:
              row[field_name] = data[field_name]
      await session.execute(table.insert().values(**row))


  # ---------------------------------------------------------------------------
  # Query
  # ---------------------------------------------------------------------------

  def parse_filter_key(key: str) -> tuple[str, str]:
      for suffix in ("_gte", "_lte", "_gt", "_lt"):
          if key.endswith(suffix):
              return key[: len(key) - len(suffix)], suffix[1:]
      return key, "eq"


  def coerce_value(value_str: str, type_str: str):
      if type_str in ("string", "text"):
          return value_str
      elif type_str == "integer":
          return int(value_str)
      elif type_str == "float":
          return float(value_str)
      elif type_str == "boolean":
          return value_str.lower() in ("true", "1", "yes")
      raise ValueError(f"Cannot coerce to type: {type_str}")


  async def query_promoted_table(
      session: AsyncSession,
      table: Table,
      schema_def: dict,
      filters: dict[str, str],
      limit: int = 100,
      offset: int = 0,
  ) -> list[dict]:
      stmt = sa_select(table)

      for key, value_str in filters.items():
          field_name, op = parse_filter_key(key)
          if field_name not in schema_def:
              raise ValueError(f"Unknown field: '{field_name}'. Valid fields: {list(schema_def.keys())}")
          if op not in FILTER_OPERATORS:
              raise ValueError(f"Unknown operator: '{op}'. Valid: {list(FILTER_OPERATORS.keys())}")

          field_def = schema_def[field_name]
          type_str = field_def["type"] if isinstance(field_def, dict) else field_def
          col = table.c[field_name]
          typed_value = coerce_value(value_str, type_str)
          stmt = stmt.where(FILTER_OPERATORS[op](col, typed_value))

      stmt = stmt.limit(limit).offset(offset)
      result = await session.execute(stmt)
      return [dict(row._mapping) for row in result]
