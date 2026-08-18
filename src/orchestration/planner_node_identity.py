from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import replace

from .models import WorkflowNodePlan, WorkflowPlan


PLANNER_NODE_IDENTITY_VERSION = "v1"
INITIAL_PLANNING_EPOCH = "p0"
MAX_PLANNER_NODE_KEY_BYTES = 256
MAX_SAFE_PLANNER_NODE_KEY_CHARS = 48

_PLANNING_EPOCH_RE = re.compile(r"(?:p0|r[1-9][0-9]*)\Z")
_SAFE_KEY_DISALLOWED_RE = re.compile(r"[^a-z0-9._-]+")
_SAFE_KEY_SEPARATOR_RE = re.compile(r"[-._]{2,}")
_DIGEST_RE = re.compile(r"[0-9a-f]{20}\Z")


class PlannerNodeIdentityError(ValueError):
    """Raised when model-authored planner identities cannot be canonicalized safely."""


def validate_canonical_model_node_identity(
    node: WorkflowNodePlan,
    *,
    task_id: str,
) -> None:
    """Fail closed when a model-origin node reaches persistence with a forged ID."""

    if node.metadata.get("identity_origin") != "model":
        return
    version = node.metadata.get("identity_version")
    planning_epoch = node.metadata.get("planning_epoch")
    safe_key = node.metadata.get("planner_node_key")
    if version != PLANNER_NODE_IDENTITY_VERSION:
        raise PlannerNodeIdentityError("model node identity_version must be v1")
    if not isinstance(planning_epoch, str) or _PLANNING_EPOCH_RE.fullmatch(planning_epoch) is None:
        raise PlannerNodeIdentityError("model node planning_epoch is invalid")
    if not isinstance(safe_key, str) or not safe_key or safe_key != PlannerNodeIdentityMap._safe_key(safe_key):
        raise PlannerNodeIdentityError("model node planner_node_key is not a canonical safe key")
    expected_prefix = (
        f"{task_id}:plan:{PLANNER_NODE_IDENTITY_VERSION}:"
        f"{planning_epoch}:{safe_key}:"
    )
    if not node.node_id.startswith(expected_prefix):
        raise PlannerNodeIdentityError("model node canonical identity does not match task or metadata")
    digest = node.node_id[len(expected_prefix) :]
    if _DIGEST_RE.fullmatch(digest) is None:
        raise PlannerNodeIdentityError("model node canonical identity digest is invalid")


DigestFactory = Callable[[bytes], str]


class PlannerNodeIdentityMap:
    """Convert task-local model node keys into deterministic global node IDs."""

    def __init__(
        self,
        *,
        task_id: str,
        planning_epoch: str,
        digest_factory: DigestFactory | None = None,
    ) -> None:
        resolved_task_id = str(task_id or "").strip()
        if not resolved_task_id:
            raise PlannerNodeIdentityError("task_id must not be empty")
        if _PLANNING_EPOCH_RE.fullmatch(planning_epoch) is None:
            raise PlannerNodeIdentityError("planning_epoch must be p0 or a positive rN epoch")
        self._task_id = resolved_task_id
        self._planning_epoch = planning_epoch
        self._digest_factory = digest_factory or self._sha256_digest20

    def canonicalize(self, plan: WorkflowPlan) -> WorkflowPlan:
        if plan.task_id != self._task_id:
            raise PlannerNodeIdentityError("task_id mismatch while canonicalizing planner nodes")

        local_keys: list[str] = []
        seen_local_keys: set[str] = set()
        for node in plan.nodes:
            local_key = self._validate_local_key(node.node_id)
            if local_key in seen_local_keys:
                raise PlannerNodeIdentityError(f"duplicate planner node key: {self._safe_key(local_key)}")
            seen_local_keys.add(local_key)
            local_keys.append(local_key)

        identity_by_local_key: dict[str, str] = {}
        local_key_by_identity: dict[str, str] = {}
        for local_key in local_keys:
            canonical_id = self._canonical_node_id(local_key)
            previous_key = local_key_by_identity.get(canonical_id)
            if previous_key is not None and previous_key != local_key:
                raise PlannerNodeIdentityError("canonical node identity collision")
            identity_by_local_key[local_key] = canonical_id
            local_key_by_identity[canonical_id] = local_key

        canonical_nodes: list[WorkflowNodePlan] = []
        for node, local_key in zip(plan.nodes, local_keys, strict=True):
            rewritten_dependencies: list[str] = []
            for dependency in node.depends_on:
                canonical_dependency = identity_by_local_key.get(dependency)
                if canonical_dependency is None:
                    raise PlannerNodeIdentityError(
                        f"unknown planner node dependency: {self._safe_key(str(dependency))}"
                    )
                rewritten_dependencies.append(canonical_dependency)
            metadata = {
                **dict(node.metadata),
                "identity_origin": "model",
                "identity_version": PLANNER_NODE_IDENTITY_VERSION,
                "planning_epoch": self._planning_epoch,
                "planner_node_key": self._safe_key(local_key),
            }
            canonical_nodes.append(
                replace(
                    node,
                    node_id=identity_by_local_key[local_key],
                    depends_on=tuple(rewritten_dependencies),
                    metadata=metadata,
                )
            )

        return replace(
            plan,
            nodes=tuple(canonical_nodes),
            metadata={
                **dict(plan.metadata),
                "planner_identity_origin": "model",
                "planner_identity_version": PLANNER_NODE_IDENTITY_VERSION,
                "planning_epoch": self._planning_epoch,
            },
        )

    def canonicalize_runtime_replan(
        self,
        plan: WorkflowPlan,
        *,
        existing_node_ids: frozenset[str] | set[str],
    ) -> WorkflowPlan:
        """Canonicalize only new model nodes in a closed mixed replan DAG."""

        if plan.task_id != self._task_id:
            raise PlannerNodeIdentityError("task_id mismatch while canonicalizing planner nodes")
        preserved_ids = frozenset(existing_node_ids)
        plan_ids = [node.node_id for node in plan.nodes]
        if len(set(plan_ids)) != len(plan_ids):
            raise PlannerNodeIdentityError("duplicate node identity in runtime replan")
        unknown_preserved = preserved_ids - set(plan_ids)
        if unknown_preserved:
            raise PlannerNodeIdentityError("runtime replan existing node set is not present in the revised plan")

        new_local_keys = [
            self._validate_local_key(node.node_id)
            for node in plan.nodes
            if node.node_id not in preserved_ids
        ]
        if len(set(new_local_keys)) != len(new_local_keys):
            raise PlannerNodeIdentityError("duplicate planner node key in runtime replan")
        if any(local_key in preserved_ids for local_key in new_local_keys):
            raise PlannerNodeIdentityError("runtime replan node key conflicts with an existing node identity")

        identity_by_local_key: dict[str, str] = {}
        local_key_by_identity: dict[str, str] = {}
        for local_key in new_local_keys:
            canonical_id = self._canonical_node_id(local_key)
            if canonical_id in preserved_ids:
                raise PlannerNodeIdentityError("canonical node identity collision")
            previous_key = local_key_by_identity.get(canonical_id)
            if previous_key is not None and previous_key != local_key:
                raise PlannerNodeIdentityError("canonical node identity collision")
            identity_by_local_key[local_key] = canonical_id
            local_key_by_identity[canonical_id] = local_key

        known_references = preserved_ids | set(identity_by_local_key)
        canonical_nodes: list[WorkflowNodePlan] = []
        for node in plan.nodes:
            if node.node_id in preserved_ids:
                if any(dependency not in known_references for dependency in node.depends_on):
                    raise PlannerNodeIdentityError("unknown existing node dependency in runtime replan")
                canonical_nodes.append(node)
                continue
            local_key = node.node_id
            rewritten_dependencies: list[str] = []
            for dependency in node.depends_on:
                if dependency in preserved_ids:
                    rewritten_dependencies.append(dependency)
                    continue
                canonical_dependency = identity_by_local_key.get(dependency)
                if canonical_dependency is None:
                    raise PlannerNodeIdentityError("unknown planner node dependency in runtime replan")
                rewritten_dependencies.append(canonical_dependency)
            metadata = {
                **dict(node.metadata),
                "identity_origin": "model",
                "identity_version": PLANNER_NODE_IDENTITY_VERSION,
                "planning_epoch": self._planning_epoch,
                "planner_node_key": self._safe_key(local_key),
            }
            canonical_nodes.append(
                replace(
                    node,
                    node_id=identity_by_local_key[local_key],
                    depends_on=tuple(rewritten_dependencies),
                    metadata=metadata,
                )
            )

        return replace(
            plan,
            nodes=tuple(canonical_nodes),
            metadata={
                **dict(plan.metadata),
                "planner_identity_origin": "mixed_runtime_replan",
                "planner_identity_version": PLANNER_NODE_IDENTITY_VERSION,
                "planning_epoch": self._planning_epoch,
            },
        )

    @staticmethod
    def _validate_local_key(value: object) -> str:
        if not isinstance(value, str):
            raise PlannerNodeIdentityError("planner node key must be a string")
        local_key = value.strip()
        if not local_key:
            raise PlannerNodeIdentityError("planner node key must not be empty")
        if len(local_key.encode("utf-8")) > MAX_PLANNER_NODE_KEY_BYTES:
            raise PlannerNodeIdentityError("planner node key must not exceed 256 UTF-8 bytes")
        if any(unicodedata.category(character) == "Cc" for character in local_key):
            raise PlannerNodeIdentityError("planner node key must not contain a control character")
        return local_key

    @staticmethod
    def _safe_key(local_key: str) -> str:
        normalized = unicodedata.normalize("NFKC", local_key).lower()
        safe_key = _SAFE_KEY_DISALLOWED_RE.sub("-", normalized)
        safe_key = _SAFE_KEY_SEPARATOR_RE.sub("-", safe_key).strip("-._")
        return (safe_key or "node")[:MAX_SAFE_PLANNER_NODE_KEY_CHARS].rstrip("-._") or "node"

    def _canonical_node_id(self, local_key: str) -> str:
        payload = json.dumps(
            {
                "epoch": self._planning_epoch,
                "planner_node_key": local_key,
                "task_id": self._task_id,
                "version": PLANNER_NODE_IDENTITY_VERSION,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        digest = str(self._digest_factory(payload)).lower()
        if _DIGEST_RE.fullmatch(digest) is None:
            raise PlannerNodeIdentityError("planner node digest factory must return 20 lowercase hex characters")
        return (
            f"{self._task_id}:plan:{PLANNER_NODE_IDENTITY_VERSION}:"
            f"{self._planning_epoch}:{self._safe_key(local_key)}:{digest}"
        )

    @staticmethod
    def _sha256_digest20(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()[:20]
