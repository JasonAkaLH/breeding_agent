from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any, Mapping

from src.core.contracts import CapabilityContract, CapabilityExecutionRequest, CapabilityExecutionResult
from src.core.models import Interrupt
from .route_understanding import QueryUnderstandingService

from .helpers import SQL_QUERY_PUBLIC_CAPABILITY_ID, load_yaml, make_artifact, skill_root
from .llm_utils import LLMOutputError, TextGenerator, call_text_generator, parse_json_object

_MIN_SEMANTIC_CONFIDENCE = 0.6


class SQLQueryIntentRouteCapability(CapabilityContract):
    capability_id = SQL_QUERY_PUBLIC_CAPABILITY_ID
    version = "1"
    description = "判断请求是否属于 SQLQuery 路由，并收集最小路由上下文。"

    def __init__(self, *, routing_rules_path: str | None = None, semantic_text_generator: TextGenerator | None = None) -> None:
        self._routing_rules_path = routing_rules_path or str(skill_root() / "configs/routing_rules.yaml")
        self._routing_rules = load_yaml(self._routing_rules_path)
        self._understanding = QueryUnderstandingService(self._routing_rules)
        self._semantic_text_generator = semantic_text_generator

    async def execute(self, request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
        user_question = str(request.input_payload.get("user_question", "")).strip()
        if not user_question:
            return self._missing_input_result(
                request,
                question="请先提供要查询的问题内容。",
                reason_code="missing_user_question",
                required_fields={"query": {"type": "string"}},
            )

        route_hint = self._optional_string(request.input_payload.get("route_hint"))
        subtask_label = self._optional_string(request.input_payload.get("subtask_label"))
        parent_question = self._optional_string(request.input_payload.get("parent_question"))
        semantic_fallback_reason: str | None = None
        semantic_payload: Mapping[str, Any] | None = None
        semantic_repair_attempted = False
        understanding = self._understanding.understand(
            user_question,
            route_hint=route_hint,
            subtask_label=subtask_label,
            parent_question=parent_question,
        )
        semantic_understanding = None
        if (
            route_hint is None
            and self._semantic_text_generator is not None
        ):
            (
                semantic_understanding,
                semantic_fallback_reason,
                semantic_payload,
                semantic_repair_attempted,
            ) = await self._try_semantic_route_understanding(
                user_question,
                request=request,
                subtask_label=subtask_label,
                parent_question=parent_question,
            )
            if semantic_understanding is not None:
                understanding = semantic_understanding
        route = understanding.route
        if route is None:
            return self._missing_input_result(
                request,
                question="请确认你要查的是审定品种库，还是基因型数据库。",
                reason_code="route_not_resolved",
                required_fields={"route_id": {"options": ["approval_variety_db", "genotype_db"]}},
            )

        inferred_crop = self._optional_string((semantic_payload or {}).get("crop")) or understanding.inferred_crop
        variety_name_candidates = self._clean_string_list((semantic_payload or {}).get("variety_name_candidates"))
        if not variety_name_candidates:
            variety_name_candidates = self._extract_variety_candidates(user_question)
        entities = self._clean_entities((semantic_payload or {}).get("entities"))
        cross_crop_allowed = self._decoded_bool((semantic_payload or {}).get("cross_crop_allowed")) or understanding.no_crop_broad_query

        output = {
            "route_id": route.get("route_id"),
            "schema_profile_id": route.get("schema_profile_id"),
            "sql_policy_profile": route.get("sql_policy_profile"),
            "allowed_tables": list(route.get("allowed_tables", [])),
            "user_question": user_question,
            "original_user_query": user_question,
            "resolved_user_query": user_question,
            "inferred_crop": inferred_crop,
            "crop": inferred_crop,
            "variety_name_candidates": variety_name_candidates,
            "entities": entities,
            "entity_type": str((semantic_payload or {}).get("entity_type") or "other"),
            "cross_crop_allowed": cross_crop_allowed,
            "confidence": self._coerce_confidence((semantic_payload or {}).get("confidence")),
            "route_resolution_strategy": understanding.route_resolution_strategy,
            "candidate_routes": [candidate.as_dict() for candidate in understanding.candidate_routes],
            "needs_decomposition": understanding.needs_decomposition,
            "subquestions": [subquestion.as_dict() for subquestion in understanding.subquestions],
            "no_crop_broad_query": cross_crop_allowed,
            "llm_router_used": semantic_understanding is not None,
            "llm_understanding_repair_attempted": semantic_repair_attempted,
        }
        if semantic_fallback_reason:
            output["llm_router_fallback_reason"] = semantic_fallback_reason
        if understanding.route_hint:
            output["route_hint"] = understanding.route_hint
        if understanding.subtask_label:
            output["subtask_label"] = understanding.subtask_label
        if understanding.parent_question:
            output["parent_question"] = understanding.parent_question
        artifact = make_artifact(
            name="intent_summary",
            task_id=request.task_id,
            node_id=request.node_id,
            payload=output,
            summary=f"intent route resolved to {output['route_id']}",
        )
        return CapabilityExecutionResult(
            capability_id=request.capability_id,
            task_id=request.task_id,
            node_id=request.node_id,
            output_payload=output,
            artifacts=(artifact,),
        )

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        stripped = value.strip()
        return stripped or None

    async def _try_semantic_route_understanding(
        self,
        user_question: str,
        *,
        request: CapabilityExecutionRequest,
        subtask_label: str | None,
        parent_question: str | None,
    ):
        raw_output = ""
        try:
            raw_output = await call_text_generator(
                self._semantic_text_generator,
                self._build_semantic_route_prompt(user_question),
                request=request,
            )
            decoded = parse_json_object(raw_output)
        except LLMOutputError as exc:
            repaired = await self._repair_semantic_understanding(
                user_question,
                raw_output=raw_output,
                reason=exc.reason,
                request=request,
                subtask_label=subtask_label,
                parent_question=parent_question,
            )
            if repaired[0] is not None or repaired[2] is not None:
                return repaired[0], repaired[1], repaired[2], True
            return None, exc.reason, None, True
        except Exception:
            return None, "provider_failed", None, False

        result, reason = self._semantic_understanding_from_payload(
            decoded,
            user_question=user_question,
            subtask_label=subtask_label,
            parent_question=parent_question,
        )
        if result is not None:
            return result, None, decoded, False
        repaired = await self._repair_semantic_understanding(
            user_question,
            raw_output=raw_output,
            reason=reason or "validation_failed",
            request=request,
            subtask_label=subtask_label,
            parent_question=parent_question,
        )
        if repaired[0] is not None or repaired[2] is not None:
            return repaired[0], repaired[1], repaired[2], True
        return None, reason, decoded, True

    async def _repair_semantic_understanding(
        self,
        user_question: str,
        *,
        raw_output: str,
        reason: str,
        request: CapabilityExecutionRequest,
        subtask_label: str | None,
        parent_question: str | None,
    ):
        try:
            repaired_raw = await call_text_generator(
                self._semantic_text_generator,
                self._build_semantic_route_repair_prompt(
                    user_question,
                    raw_output=raw_output,
                    reason=reason,
                ),
                request=request,
            )
            decoded = parse_json_object(repaired_raw)
        except LLMOutputError as exc:
            return None, exc.reason, None
        except Exception:
            return None, "provider_failed", None

        result, validation_reason = self._semantic_understanding_from_payload(
            decoded,
            user_question=user_question,
            subtask_label=subtask_label,
            parent_question=parent_question,
        )
        return result, validation_reason, decoded

    def _semantic_understanding_from_payload(
        self,
        decoded: Mapping[str, Any],
        *,
        user_question: str,
        subtask_label: str | None,
        parent_question: str | None,
    ):
        intent = str(decoded.get("intent") or "").strip().lower()
        route_id_value = str(decoded.get("route_id") or "").strip().lower()
        if (
            self._decoded_bool(decoded.get("clarification_needed"))
            or intent in {"non_database", "unsupported"}
            or route_id_value == "unsupported"
        ):
            return None, "semantic_router_declined"

        confidence = self._coerce_confidence(decoded.get("confidence"))
        if confidence is not None and confidence < _MIN_SEMANTIC_CONFIDENCE:
            return None, "low_confidence"

        route_id = self._optional_string(decoded.get("route_id"))
        if route_id is None:
            route_ids = decoded.get("route_ids")
            if isinstance(route_ids, list) and route_ids:
                route_id = self._optional_string(route_ids[0])
        if route_id is None:
            return None, "missing_route_id"

        understanding = self._understanding.understand(
            user_question,
            route_hint=route_id,
            subtask_label=subtask_label,
            parent_question=parent_question,
        )
        if understanding.route is None or understanding.route_resolution_strategy != "route_hint":
            return None, "unsupported_route_id"

        return replace(
            understanding,
            route_resolution_strategy="llm_semantic",
            route_hint=None,
        ), None

    def _build_semantic_route_prompt(self, user_question: str) -> str:
        routes = [
            {
                "route_id": route.get("route_id"),
                "display_name": route.get("display_name"),
                "description": route.get("description"),
                "intent_keywords": route.get("intent_keywords", []),
                "semantic_hints": route.get("semantic_hints", []),
                "supported_crops": route.get("supported_crops", []),
            }
            for route in self._routing_rules.get("routes", [])
            if isinstance(route, Mapping) and route.get("enabled", True)
        ]
        return (
            "阶段：intent_route。\n"
            "你是 SQLQuery 的受控语义路由器。只判断用户问题属于哪个已配置数据库路由，不生成 SQL。"
            "只能返回 JSON 对象，字段包括：intent、route_id、confidence、crop、entities、variety_name_candidates、entity_type、cross_crop_allowed、clarification_needed、clarifying_question、reason。"
            "entities 是可选数组；每项包含 text、entity_type、field_intent、primary_fields、secondary_fields、confidence。"
            "entity_type 只能是 variety、organization、person、approval_number、other；不要输出 region/year。"
            "field_intent 只能是 variety_name、applicant、breeder、applicant_or_breeder、approval_num、unknown。"
            "route_id 必须是 approval_variety_db、genotype_db 或 unsupported；confidence 是 0 到 1 的数字。"
            "如果不是支持的数据库问题，route_id=unsupported。\n\n"
            f"routes={json.dumps(routes, ensure_ascii=False)}\n\n"
            f"user_question={user_question}"
        )

    def _build_semantic_route_repair_prompt(self, user_question: str, *, raw_output: str, reason: str) -> str:
        return (
            "阶段：intent_route_repair。\n"
            "上一次 SQLQuery query understanding 输出未通过 runtime 校验。请只修正为合法 JSON，不生成 SQL，不添加解释。\n"
            "合法字段：intent、route_id、confidence、crop、entities、variety_name_candidates、entity_type、cross_crop_allowed、clarification_needed、clarifying_question、reason。\n"
            "entities 每项字段只能包含 text、entity_type、field_intent、primary_fields、secondary_fields、confidence；不要输出 region/year 或 transgenic_owner。\n"
            "route_id 只能是 approval_variety_db、genotype_db 或 unsupported；confidence 必须是 0 到 1 的数字。\n"
            f"validation_reason={reason}\n"
            f"user_question={user_question}\n"
            f"previous_output={raw_output}"
        )

    @staticmethod
    def _decoded_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"true", "yes", "1", "是"}
        return bool(value)

    @staticmethod
    def _coerce_confidence(value: Any) -> float | None:
        if value is None or value == "":
            return None
        try:
            confidence = float(value)
        except (TypeError, ValueError):
            return None
        if confidence < 0:
            return 0.0
        if confidence > 1:
            return 1.0
        return confidence

    @staticmethod
    def _clean_string_list(value: Any) -> list[str]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list | tuple):
            return []
        result: list[str] = []
        for item in value:
            text = str(item or "").strip()
            text = text.replace("`", "").replace("'", "").replace('"', "").replace(";", "").replace("\\", "")
            if text and len(text) <= 40:
                result.append(text)
        return list(dict.fromkeys(result))[:3]

    @classmethod
    def _clean_entities(cls, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list | tuple):
            return []
        result: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, Mapping):
                continue
            text = cls._clean_entity_text(item.get("text") or item.get("name") or item.get("value") or item.get("entity"))
            if not text:
                continue
            entity_type = cls._normalize_entity_type(item.get("entity_type"))
            if entity_type is None:
                continue
            field_intent = cls._normalize_field_intent(item.get("field_intent"))
            entity = {
                "text": text,
                "entity_type": entity_type,
                "field_intent": field_intent,
                "primary_fields": cls._clean_field_list(item.get("primary_fields")),
                "secondary_fields": cls._clean_field_list(item.get("secondary_fields")),
            }
            confidence = cls._coerce_confidence(item.get("confidence"))
            if confidence is not None:
                entity["confidence"] = confidence
            if entity not in result:
                result.append(entity)
        return result[:3]

    @staticmethod
    def _clean_entity_text(value: Any) -> str | None:
        text = str(value or "").strip()
        text = text.replace("`", "").replace("'", "").replace('"', "").replace(";", "").replace("\\", "")
        return text if text and len(text) <= 40 else None

    @staticmethod
    def _normalize_entity_type(value: Any) -> str | None:
        text = str(value or "other").strip().lower()
        aliases = {
            "company": "organization",
            "org": "organization",
            "institution": "organization",
            "variety_name": "variety",
            "approval_num": "approval_number",
        }
        text = aliases.get(text, text)
        if text in {"region", "year"}:
            return None
        return text if text in {"variety", "organization", "person", "approval_number", "other"} else "other"

    @staticmethod
    def _normalize_field_intent(value: Any) -> str:
        text = str(value or "unknown").strip().lower()
        aliases = {
            "company": "applicant_or_breeder",
            "organization": "applicant_or_breeder",
            "approval_number": "approval_num",
        }
        text = aliases.get(text, text)
        return text if text in {"variety_name", "applicant", "breeder", "applicant_or_breeder", "approval_num", "unknown"} else "unknown"

    @staticmethod
    def _clean_field_list(value: Any) -> list[str]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list | tuple):
            return []
        allowed = {"variety_name", "applicant", "breeder", "approval_num"}
        result: list[str] = []
        for item in value:
            text = str(item or "").strip().lower()
            if text in allowed and text not in result:
                result.append(text)
        return result

    @staticmethod
    def _extract_variety_candidates(user_question: str) -> list[str]:
        import re

        candidates: list[str] = []
        for match in re.findall(r"[A-Za-z\u4e00-\u9fff]{1,16}\d+[A-Za-z0-9\u4e00-\u9fff]{0,12}", user_question):
            text = match.strip()
            for word in ("查询", "查一下", "帮我", "品种", "审定", "信息", "详细"):
                text = text.replace(word, "")
            text = re.split(r"(?:的|是|为|有没有|是否|多少|哪些|有什么)", text, maxsplit=1)[0].strip()
            if text:
                candidates.append(text)
        return list(dict.fromkeys(candidates))[:3]

    def _make_route_interrupt(
        self,
        request: CapabilityExecutionRequest,
        *,
        question: str,
        reason_code: str,
        required_fields: Mapping[str, Any] | None = None,
    ) -> Interrupt:
        fingerprint = json.dumps(
            {
                "reason_code": reason_code,
                "question": question,
                "required_fields": required_fields or {},
                "input_payload": request.input_payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        digest = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:10]
        enriched_required_fields = self._natural_language_required_fields(
            reason_code=reason_code,
            required_fields=required_fields or {},
        )
        return Interrupt(
            interrupt_id=f"{request.node_id}:interrupt:{reason_code}:{digest}",
            conversation_id=request.conversation_id,
            task_id=request.task_id,
            node_id=request.node_id,
            source_agent=self.capability_id,
            source_message_id=f"{request.node_id}:clarification",
            question=question,
            reason_code=reason_code,
            required_fields=enriched_required_fields,
        )

    def _missing_input_result(
        self,
        request: CapabilityExecutionRequest,
        *,
        question: str,
        reason_code: str,
        required_fields: Mapping[str, Any],
    ) -> CapabilityExecutionResult:
        output_payload = {
            "ok": False,
            "domain_kind": "sql_query",
            "capability_id": SQL_QUERY_PUBLIC_CAPABILITY_ID,
            "status": "missing_input",
            "needs_user_input": True,
            "answer": question,
            "response_text": question,
            "error": {"type": "missing_input", "message": question},
            "missing": [
                str(field)
                for field in required_fields.keys()
                if not str(field).startswith("_")
            ],
            "presentation": "natural_language",
        }
        return CapabilityExecutionResult(
            capability_id=request.capability_id,
            task_id=request.task_id,
            node_id=request.node_id,
            output_payload=output_payload,
            interrupt=self._make_route_interrupt(
                request,
                question=question,
                reason_code=reason_code,
                required_fields=required_fields,
            ),
        )

    @staticmethod
    def _natural_language_required_fields(
        *,
        reason_code: str,
        required_fields: Mapping[str, Any],
    ) -> dict[str, Any]:
        enriched = dict(required_fields)
        enriched["_sql_query_resolution"] = {
            "domain_kind": "sql_query",
            "presentation": "natural_language",
            "reason_code": reason_code,
        }
        return enriched
