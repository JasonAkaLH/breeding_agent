from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .models import FailureDetail, HintPayload, JoinHint, SchemaContextRequest, SchemaContextResult, TableSelection

_HINT_TABLE_KEYS = {"table", "tables", "table_name", "table_names", "selected_tables"}
_HINT_COLUMN_KEYS = {"column", "columns", "column_name", "column_names", "selected_columns", "metrics", "dimensions"}
_HINT_CROP_KEYS = {"crop", "crops", "crop_name", "crop_names"}
_HINT_ENTITY_KEYS = {"entity", "entities", "keyword", "keywords", "value", "values"}


@dataclass(slots=True)
class _NormalizedHints:
    search_text: str
    raw_strings: tuple[str, ...]
    table_names: tuple[str, ...]
    column_names: tuple[str, ...]
    crop_names: tuple[str, ...]
    entities: tuple[str, ...]


class SchemaContextBuilder:
    """Builds a trimmed SQLQuery schema context from static route/schema metadata only."""

    def __init__(
        self,
        routing_rules: Mapping[str, Any],
        schema_metadata: Mapping[str, Any],
    ) -> None:
        self._routing_rules = routing_rules
        self._schema_metadata = schema_metadata
        self._route_index = {
            route["route_id"]: route
            for route in routing_rules.get("routes", [])
            if isinstance(route, Mapping) and route.get("route_id")
        }
        self._profile_index = {
            profile["profile_id"]: profile
            for profile in schema_metadata.get("schema_profiles", [])
            if isinstance(profile, Mapping) and profile.get("profile_id")
        }
        self._table_index = {
            table_name: table_meta
            for table_name, table_meta in schema_metadata.get("tables", {}).items()
            if isinstance(table_meta, Mapping)
        }
        self._llm_context_rules = schema_metadata.get("llm_context_rules", {})
        self._configured_join_hints = [
            join_hint
            for join_hint in schema_metadata.get("join_hints", [])
            if isinstance(join_hint, Mapping)
        ]
        self._reverse_fk_index = self._build_reverse_fk_index(self._table_index)

    @classmethod
    async def from_yaml_files(
        cls,
        routing_rules_path: str | Path,
        schema_metadata_path: str | Path,
    ) -> "SchemaContextBuilder":
        routing_rules, schema_metadata = await asyncio.gather(
            asyncio.to_thread(cls._load_yaml, routing_rules_path),
            asyncio.to_thread(cls._load_yaml, schema_metadata_path),
        )
        return cls(routing_rules=routing_rules, schema_metadata=schema_metadata)

    async def build_context(self, request: SchemaContextRequest) -> SchemaContextResult:
        return await asyncio.to_thread(self._build_context_sync, request)

    def _build_context_sync(self, request: SchemaContextRequest) -> SchemaContextResult:
        route = self._route_index.get(request.route_id)
        if not route or not route.get("enabled", True):
            return self._failure(
                request,
                code="route_not_found",
                message=f"Unknown or disabled route_id: {request.route_id}",
                retriable=True,
            )

        profile = self._profile_index.get(request.schema_profile_id)
        if not profile:
            return self._failure(
                request,
                code="schema_profile_not_found",
                message=f"Unknown schema_profile_id: {request.schema_profile_id}",
                retriable=True,
            )

        expected_profile_id = route.get("schema_profile_id")
        if expected_profile_id and expected_profile_id != request.schema_profile_id:
            return self._failure(
                request,
                code="route_profile_mismatch",
                message=(
                    f"Route {request.route_id} expects schema_profile_id={expected_profile_id}, "
                    f"got {request.schema_profile_id}"
                ),
                retriable=True,
                metadata={"expected_schema_profile_id": expected_profile_id},
            )

        candidate_tables = self._candidate_tables(route, profile)
        if not candidate_tables:
            return self._failure(
                request,
                code="no_allowed_tables",
                message="No tables remain after intersecting route whitelist and schema profile.",
                retriable=False,
            )

        normalized_hints = self._normalize_hints(request.user_question, request.hints)
        filtered_tables, route_notes, crop_failure = self._apply_route_filters(route, candidate_tables, normalized_hints)
        if crop_failure is not None:
            return self._failure(request, **crop_failure)

        scored_tables: list[tuple[str, int, list[str]]] = []
        seed_reasons = self._route_seed_tables(route.get("route_id", ""), normalized_hints.search_text, filtered_tables)
        for table_name in filtered_tables:
            table_meta = self._table_index.get(table_name, {})
            score, reasons = self._score_table(table_name, table_meta, normalized_hints)
            for reason in seed_reasons.get(table_name, ()):
                score += 10
                reasons.append(reason)
            if not table_meta.get("allow_sql_generation", True):
                score -= 100
                reasons.append("table is marked as disallowed for SQL generation")
            scored_tables.append((table_name, score, self._unique_preserve_order(reasons)))

        max_tables = min(request.max_tables, int(self._llm_context_rules.get("max_tables_per_request", request.max_tables)))
        selected_tables = self._pick_tables(scored_tables, max_tables)
        if not selected_tables and len(filtered_tables) == 1:
            selected_tables = [filtered_tables[0]]

        selected_tables = self._ensure_bridge_tables(selected_tables, filtered_tables)
        if not selected_tables:
            return self._failure(
                request,
                code="no_matching_tables",
                message="No matching tables were found for the current route/question/hints.",
                retriable=True,
                metadata={"candidate_tables": tuple(filtered_tables)},
            )

        table_selections: list[TableSelection] = []
        selected_columns: dict[str, tuple[str, ...]] = {}
        table_reason_index = {table: reasons for table, _, reasons in scored_tables}
        table_score_index = {table: score for table, score, _ in scored_tables}
        for table_name in selected_tables:
            table_meta = self._table_index[table_name]
            columns = self._select_columns(
                table_name=table_name,
                table_meta=table_meta,
                selected_tables=selected_tables,
            )
            selected_columns[table_name] = tuple(columns)
            table_selections.append(
                TableSelection(
                    table_name=table_name,
                    description=str(table_meta.get("description", "")),
                    selected_columns=tuple(columns),
                    reasons=tuple(table_reason_index.get(table_name, ())),
                    score=table_score_index.get(table_name, 0),
                )
            )

        join_hints = self._build_join_hints(selected_tables)
        inferred_crop = self._infer_crop(route, normalized_hints)
        context_summary = self._build_summary(
            route=route,
            profile=profile,
            request=request,
            selected_tables=selected_tables,
            selected_columns=selected_columns,
            join_hints=join_hints,
            route_notes=route_notes,
            inferred_crop=inferred_crop,
            normalized_hints=normalized_hints,
        )
        return SchemaContextResult(
            ok=True,
            route_id=request.route_id,
            schema_profile_id=request.schema_profile_id,
            selected_tables=tuple(selected_tables),
            selected_columns=selected_columns,
            join_hints=tuple(join_hints),
            context_summary=context_summary,
            table_selections=tuple(table_selections),
            metadata={
                "route_display_name": route.get("display_name"),
                "route_description": route.get("description"),
                "inferred_crop": inferred_crop,
                "route_notes": tuple(route_notes),
                "no_crop_broad_query": bool(
                    route.get("route_id") == "approval_variety_db"
                    and not inferred_crop
                    and route.get("supports_no_crop_broad_query", False)
                ),
                "llm_context_rules": self._llm_context_rules,
                "column_selection_strategy": "llm_visible_all_exposed_columns",
            },
        )

    @staticmethod
    def _load_yaml(path: str | Path) -> Mapping[str, Any]:
        try:
            import yaml
        except ModuleNotFoundError as exc:  # pragma: no cover - depends on runtime env
            raise RuntimeError("PyYAML is required to load SQLQuery YAML configuration files.") from exc

        file_path = Path(path)
        with file_path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        if not isinstance(data, Mapping):
            raise ValueError(f"YAML file {file_path} must decode to a mapping.")
        return data

    def _candidate_tables(self, route: Mapping[str, Any], profile: Mapping[str, Any]) -> list[str]:
        profile_tables = [str(table) for table in profile.get("tables", []) if str(table) in self._table_index]
        allowed_tables = {str(table) for table in route.get("allowed_tables", [])}
        if not allowed_tables:
            return profile_tables
        return [table for table in profile_tables if table in allowed_tables]

    def _apply_route_filters(
        self,
        route: Mapping[str, Any],
        candidate_tables: Sequence[str],
        normalized_hints: _NormalizedHints,
    ) -> tuple[list[str], list[str], dict[str, Any] | None]:
        route_notes: list[str] = []
        explicit_tables = [table for table in normalized_hints.table_names if table in candidate_tables]
        if explicit_tables:
            route_notes.append(f"restricted by explicit table hints: {', '.join(explicit_tables)}")
            return explicit_tables, route_notes, None

        if route.get("route_id") == "variety_overview":
            route_notes.append("variety_overview broad first-principles lookup retained approval and genotype tables")
            return list(candidate_tables), route_notes, None

        crop_mapping = route.get("crop_table_mapping")
        if not isinstance(crop_mapping, Mapping):
            return list(candidate_tables), route_notes, None

        inferred_crop = self._infer_crop(route, normalized_hints)
        if inferred_crop:
            crop_tables = [
                str(table)
                for table in crop_mapping.get(inferred_crop, [])
                if str(table) in candidate_tables
            ]
            if crop_tables:
                route_notes.append(f"restricted to crop={inferred_crop}")
                return crop_tables, route_notes, None

        if route.get("supports_no_crop_broad_query", False):
            route_notes.append("no crop resolved; retained all approval crop tables for a broad query")
            return list(candidate_tables), route_notes, None

        if len(candidate_tables) == 1:
            return list(candidate_tables), route_notes, None

        return list(candidate_tables), route_notes, {
            "code": "crop_not_resolved",
            "message": "Route requires crop resolution before schema trimming can proceed.",
            "retriable": True,
            "metadata": {"candidate_tables": tuple(candidate_tables)},
        }

    def _score_table(
        self,
        table_name: str,
        table_meta: Mapping[str, Any],
        normalized_hints: _NormalizedHints,
    ) -> tuple[int, list[str]]:
        score = 0
        reasons: list[str] = []
        search_text = normalized_hints.search_text

        if table_name in normalized_hints.table_names:
            score += 100
            reasons.append(f"explicit table hint matched {table_name}")

        description = self._normalize_text(table_meta.get("description", ""))
        if description and description in search_text:
            score += 15
            reasons.append("table description matched user question or hints")

        if self._normalize_text(table_name) in search_text:
            score += 20
            reasons.append("table name matched user question or hints")

        for route_tag in table_meta.get("route_tags", []):
            normalized_tag = self._normalize_text(route_tag)
            if normalized_tag and normalized_tag in search_text:
                score += 6
                reasons.append(f"route tag matched: {route_tag}")

        for column_name, column_meta in table_meta.get("columns", {}).items():
            if not isinstance(column_meta, Mapping):
                continue
            normalized_column = self._normalize_text(column_name)
            if normalized_column and normalized_column in search_text:
                score += 8
                reasons.append(f"column name matched: {column_name}")
            column_description = self._normalize_text(column_meta.get("description", ""))
            if column_description and column_description in search_text:
                score += 6
                reasons.append(f"column description matched: {column_name}")
            if column_name in normalized_hints.column_names:
                score += 20
                reasons.append(f"explicit column hint matched: {column_name}")

        for join_hint in self._configured_join_hints:
            if table_name not in {str(join_hint.get("left_table")), str(join_hint.get("right_table"))}:
                continue
            description = self._normalize_text(join_hint.get("description", ""))
            if description and description in search_text:
                score += 10
                reasons.append("configured join hint description matched user question or hints")

        return score, reasons

    def _route_seed_tables(
        self,
        route_id: str,
        search_text: str,
        candidate_tables: Sequence[str],
    ) -> dict[str, tuple[str, ...]]:
        candidate_set = set(candidate_tables)
        seeds: dict[str, list[str]] = {}

        def add(table_name: str, reason: str) -> None:
            if table_name in candidate_set:
                seeds.setdefault(table_name, []).append(reason)

        if route_id == "approval_variety_db" and len(candidate_tables) == 1:
            add(candidate_tables[0], "route/crop resolution narrowed to a single approval table")

        if route_id == "approval_variety_db" and len(candidate_tables) > 1:
            for table_name in candidate_tables:
                add(table_name, "approval broad route seed: all approval crop tables retained")

        if route_id == "variety_overview":
            for table_name in candidate_tables:
                add(table_name, "variety_overview seed: broad first-principles lookup keeps approval and genotype sources")

        if route_id == "genotype_db":
            for table_name in candidate_tables:
                add(table_name, "genotype route seed: xiaoao-style SQL prompt keeps the complete gene schema")
            if any(keyword in search_text for keyword in ("qtn", "位点", "gene", "基因")):
                add("qtn", "genotype route seed: gene/QTN language detected")
            if any(keyword in search_text for keyword in ("基因型", "genotype", "表现型", "phenotype")):
                add("variety_genotype", "genotype route seed: genotype language detected")
            if any(keyword in search_text for keyword in ("成分", "比例", "籼", "粳")):
                add("rice_comp", "genotype route seed: composition language detected")
            if any(keyword in search_text for keyword in ("品种", "variety")):
                add("variety", "genotype route seed: variety language detected")
            if {"variety_genotype", "rice_comp"} & set(seeds):
                add("variety", "bridge seed: selected genotype tables often join through variety")

        return {table_name: tuple(self._unique_preserve_order(reasons)) for table_name, reasons in seeds.items()}

    def _pick_tables(
        self,
        scored_tables: Sequence[tuple[str, int, Sequence[str]]],
        max_tables: int,
    ) -> list[str]:
        ranked = sorted(scored_tables, key=lambda item: (-item[1], item[0]))
        positive = [table_name for table_name, score, _ in ranked if score > 0]
        return positive[:max_tables]

    def _ensure_bridge_tables(self, selected_tables: Sequence[str], candidate_tables: Sequence[str]) -> list[str]:
        selected = list(dict.fromkeys(selected_tables))
        selected_set = set(selected)
        for table_name in candidate_tables:
            if table_name in selected_set:
                continue
            neighbors = self._table_neighbors(table_name)
            if len(neighbors & selected_set) >= 2:
                selected.append(table_name)
                selected_set.add(table_name)
        return selected

    def _select_columns(
        self,
        table_name: str,
        table_meta: Mapping[str, Any],
        selected_tables: Sequence[str],
    ) -> list[str]:
        columns_meta = table_meta.get("columns", {})
        if not isinstance(columns_meta, Mapping):
            return []

        join_columns = {
            str(fk.get("column"))
            for fk in table_meta.get("foreign_keys", [])
            if str(fk.get("ref_table")) in selected_tables
        }
        reverse_join_columns = {
            str(fk.get("ref_column"))
            for fk in self._reverse_fk_index.get(table_name, [])
            if str(fk.get("table_name")) in selected_tables
        }

        selected: list[str] = []
        for column_name in columns_meta.keys():
            column_meta = columns_meta[column_name]
            if not isinstance(column_meta, Mapping):
                continue
            expose_to_llm = column_meta.get("expose_to_llm", True)
            drop_hidden_columns = bool(self._llm_context_rules.get("drop_hidden_columns", True))
            if drop_hidden_columns and expose_to_llm is False and column_name not in join_columns and column_name not in reverse_join_columns:
                continue
            selected.append(str(column_name))
        return selected

    def _build_join_hints(self, selected_tables: Sequence[str]) -> list[JoinHint]:
        if not bool(self._llm_context_rules.get("include_join_hints", True)):
            return []

        selected_set = set(selected_tables)
        join_hints: list[JoinHint] = []
        seen: set[tuple[str, str, str, str]] = set()

        for configured in self._configured_join_hints:
            left_table = str(configured.get("left_table"))
            right_table = str(configured.get("right_table"))
            if left_table not in selected_set or right_table not in selected_set:
                continue
            hint = JoinHint(
                left_table=left_table,
                left_column=str(configured.get("left_column")),
                right_table=right_table,
                right_column=str(configured.get("right_column")),
                reason=str(configured.get("description", "configured join hint")),
            )
            key = (hint.left_table, hint.left_column, hint.right_table, hint.right_column)
            if key not in seen:
                join_hints.append(hint)
                seen.add(key)

        for table_name in selected_tables:
            table_meta = self._table_index.get(table_name, {})
            for foreign_key in table_meta.get("foreign_keys", []):
                ref_table = str(foreign_key.get("ref_table"))
                if ref_table not in selected_set:
                    continue
                hint = JoinHint(
                    left_table=table_name,
                    left_column=str(foreign_key.get("column")),
                    right_table=ref_table,
                    right_column=str(foreign_key.get("ref_column")),
                    reason="declared foreign key relationship in schema metadata",
                )
                key = (hint.left_table, hint.left_column, hint.right_table, hint.right_column)
                if key not in seen:
                    join_hints.append(hint)
                    seen.add(key)
        return join_hints

    def _build_summary(
        self,
        *,
        route: Mapping[str, Any],
        profile: Mapping[str, Any],
        request: SchemaContextRequest,
        selected_tables: Sequence[str],
        selected_columns: Mapping[str, Sequence[str]],
        join_hints: Sequence[JoinHint],
        route_notes: Sequence[str],
        inferred_crop: str | None,
        normalized_hints: _NormalizedHints,
    ) -> str:
        selected_table_summary = ", ".join(selected_tables)
        selected_column_summary = "; ".join(
            f"{table}: {', '.join(columns)}"
            for table, columns in selected_columns.items()
        )
        join_summary = "; ".join(join.expression for join in join_hints) or "no joins required"
        note_bits = list(route_notes)
        if inferred_crop:
            note_bits.append(f"inferred_crop={inferred_crop}")
        if normalized_hints.entities:
            note_bits.append(f"entities={', '.join(normalized_hints.entities[:4])}")
        notes = "; ".join(note_bits) if note_bits else "no extra route notes"
        return (
            f"Route {route.get('display_name', request.route_id)} / profile {profile.get('profile_id')} "
            f"selected tables [{selected_table_summary}] and LLM-visible columns [{selected_column_summary}]. "
            f"Join hints: {join_summary}. Notes: {notes}."
        )

    def _failure(
        self,
        request: SchemaContextRequest,
        *,
        code: str,
        message: str,
        retriable: bool,
        metadata: Mapping[str, Any] | None = None,
    ) -> SchemaContextResult:
        failure = FailureDetail(code=code, message=message, retriable=retriable, metadata=metadata or {})
        return SchemaContextResult(
            ok=False,
            route_id=request.route_id,
            schema_profile_id=request.schema_profile_id,
            selected_tables=(),
            selected_columns={},
            join_hints=(),
            context_summary=message,
            failure=failure,
            metadata=metadata or {},
        )

    def _infer_crop(self, route: Mapping[str, Any], normalized_hints: _NormalizedHints) -> str | None:
        supported_crops = [str(crop) for crop in route.get("supported_crops", [])]
        crop_aliases = route.get("crop_aliases", {})
        for crop_name in normalized_hints.crop_names:
            if crop_name in supported_crops:
                return crop_name
        search_text = normalized_hints.search_text
        for crop_name in supported_crops:
            if self._normalize_text(crop_name) in search_text:
                return crop_name
            for alias in crop_aliases.get(crop_name, []):
                if self._normalize_text(alias) in search_text:
                    return crop_name
        return None

    @staticmethod
    def _normalize_hints(user_question: str, hints: HintPayload) -> _NormalizedHints:
        raw_strings = [user_question]
        tables: list[str] = []
        columns: list[str] = []
        crops: list[str] = []
        entities: list[str] = []

        def visit(value: Any, parent_key: str | None = None) -> None:
            if value is None:
                return
            if isinstance(value, Mapping):
                for key, inner_value in value.items():
                    visit(inner_value, str(key))
                return
            if isinstance(value, str):
                cleaned = value.strip()
                if not cleaned:
                    return
                raw_strings.append(cleaned)
                normalized = SchemaContextBuilder._normalize_text(cleaned)
                if parent_key in _HINT_TABLE_KEYS:
                    tables.append(normalized)
                elif parent_key in _HINT_COLUMN_KEYS:
                    columns.append(normalized)
                elif parent_key in _HINT_CROP_KEYS:
                    crops.append(normalized)
                elif parent_key in _HINT_ENTITY_KEYS:
                    entities.append(cleaned)
                return
            if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
                for item in value:
                    visit(item, parent_key)

        visit(hints)
        search_text = SchemaContextBuilder._normalize_text(" ".join(raw_strings))
        if not entities:
            entities = [part for part in re.findall(r"[A-Za-z0-9_]+", user_question) if len(part) > 1]
        return _NormalizedHints(
            search_text=search_text,
            raw_strings=tuple(dict.fromkeys(raw_strings)),
            table_names=tuple(dict.fromkeys(tables)),
            column_names=tuple(dict.fromkeys(columns)),
            crop_names=tuple(dict.fromkeys(crops)),
            entities=tuple(dict.fromkeys(entities)),
        )

    @staticmethod
    def _normalize_text(value: Any) -> str:
        if value is None:
            return ""
        text = str(value).strip().lower()
        return " ".join(text.split())

    @staticmethod
    def _unique_preserve_order(values: Iterable[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            if value not in seen:
                result.append(value)
                seen.add(value)
        return result

    @staticmethod
    def _build_reverse_fk_index(table_index: Mapping[str, Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        reverse_index: dict[str, list[dict[str, Any]]] = {}
        for table_name, table_meta in table_index.items():
            for foreign_key in table_meta.get("foreign_keys", []):
                ref_table = str(foreign_key.get("ref_table"))
                reverse_index.setdefault(ref_table, []).append(
                    {
                        "table_name": table_name,
                        "column": str(foreign_key.get("column")),
                        "ref_column": str(foreign_key.get("ref_column")),
                    }
                )
        return reverse_index

    def _table_neighbors(self, table_name: str) -> set[str]:
        neighbors = {
            str(foreign_key.get("ref_table"))
            for foreign_key in self._table_index.get(table_name, {}).get("foreign_keys", [])
        }
        neighbors.update(item["table_name"] for item in self._reverse_fk_index.get(table_name, []))
        return neighbors
