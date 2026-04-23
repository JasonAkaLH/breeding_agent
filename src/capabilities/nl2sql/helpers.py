from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import yaml

from src.core.contracts import CapabilityExecutionRequest
from src.core.enums import ArtifactType
from src.core.models import Artifact


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def load_yaml(path: str | Path) -> Mapping[str, Any]:
    file_path = Path(path)
    data = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, Mapping):
        raise ValueError(f"YAML file {file_path} must decode to a mapping.")
    return data


def find_dependency_output(request: CapabilityExecutionRequest, required_keys: tuple[str, ...]) -> dict[str, Any]:
    for output in request.dependency_outputs.values():
        if all(key in output for key in required_keys):
            return dict(output)
    raise ValueError(f"Missing dependency output with keys: {required_keys}")


def make_artifact(
    *,
    name: str,
    task_id: str,
    node_id: str,
    payload: Any,
    summary: str,
    artifact_type: ArtifactType = ArtifactType.JSON,
) -> Artifact:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(f"{node_id}:{name}:{serialized}".encode("utf-8")).hexdigest()[:12]
    artifact_id = f"{node_id}:{name}:{digest}"
    return Artifact(
        artifact_id=artifact_id,
        task_id=task_id,
        producer_node_id=node_id,
        artifact_type=artifact_type,
        storage_ref=serialized,
        summary=summary,
        is_complete=True,
    )


def normalize_text(value: Any) -> str:
    return str(value or "").strip().lower()
