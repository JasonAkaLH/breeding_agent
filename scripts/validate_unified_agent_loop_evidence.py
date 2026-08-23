from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


ACTIVE_INVENTORY_RELATIVE = Path(
    "docs/prd/backend/unified-agent-loop/active-prd-inventory.md"
)
UNIFIED_PRD_RELATIVE = Path("docs/prd/backend/unified-agent-loop")
ACTIVE_INVENTORY_SCHEMA = "maf.unified_agent_loop.active_prd_inventory.v1"
CURRENT_EVIDENCE_COMMAND = (
    "conda run -n multi_agent python "
    "scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed"
)

PRD_LEGACY_TERMS = (
    "WorkflowPlan",
    "RuntimeReplanner",
    "main_agent.respond",
    "CompletionPolicy",
    "max_replans",
    "max_dynamic_nodes",
)
TEST_LEGACY_TERMS = PRD_LEGACY_TERMS + (
    "TaskEdge",
    "planner.reasoning_delta",
    "soft_skill.reasoning_delta",
)
DISPOSITIONS = {
    "preserve",
    "rewrite",
    "supersede_at_phase6",
    "historical",
}
TEST_CLASSIFICATIONS = {
    "migrate_behavior",
    "migrate_then_delete_dag_shape",
    "delete_dag_shape",
    "retain_migration_history",
}
ROW_STATUSES = {"registered", "rewritten", "superseded", "historical", "removed"}
REQUIRED_ENTRY_IDS = {
    "ordinary_submit",
    "explicit_skill_submit",
    "explicit_mcp_submit",
    "skill_missing_input_answer",
    "mcp_approval_answer",
    "mcp_mrtr_answer",
    "mcp_remote_completion",
    "task_cancel",
    "crash_startup_recovery",
}
FUTURE_HANDOFFS = {
    "cutover-readiness.md": (5, ("commit", "start", "resume", "cancel", "recovery", "blocker", "schema")),
    "dag-runtime-deletion-report.md": (6, ("deleted", "replacement", "zero", "Phase 7", "rollback")),
    "destructive-migration-evidence.md": (
        7,
        ("backup", "restore", "SQLite", "PostgreSQL", "Sidecar", "Frontend", "Rust", "MCP"),
    ),
}


class EvidenceContractError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class InventoryRow:
    values: dict[str, str]


def collect_active_prd_matches(repo_root: Path) -> dict[str, tuple[str, ...]]:
    prd_root = repo_root / "docs" / "prd"
    if not prd_root.is_dir():
        return {}
    matches: dict[str, tuple[str, ...]] = {}
    for path in sorted(prd_root.rglob("*.md")):
        relative = path.relative_to(repo_root)
        if _is_within(relative, UNIFIED_PRD_RELATIVE):
            continue
        found = _matched_terms(path.read_text(encoding="utf-8"), PRD_LEGACY_TERMS)
        if found:
            matches[relative.as_posix()] = found
    return matches


def collect_legacy_test_matches(repo_root: Path) -> dict[str, tuple[str, ...]]:
    tests_root = repo_root / "tests"
    if not tests_root.is_dir():
        return {}
    matches: dict[str, tuple[str, ...]] = {}
    for path in sorted(tests_root.rglob("test_*.py")):
        if _is_agent_replacement_test(path):
            continue
        found = _matched_terms(path.read_text(encoding="utf-8"), TEST_LEGACY_TERMS)
        if found:
            matches[path.relative_to(repo_root).as_posix()] = found
    return matches


def validate_handoff_schedule(
    repo_root: Path,
    *,
    phase: int,
    require_closed: bool,
) -> dict[str, str]:
    _validate_phase(phase)
    handoff_root = repo_root / UNIFIED_PRD_RELATIVE
    results: dict[str, str] = {}
    for basename, (owner_phase, required_terms) in FUTURE_HANDOFFS.items():
        path = handoff_root / basename
        if not path.exists():
            if phase >= owner_phase:
                raise EvidenceContractError(
                    f"{basename} is required in phase {owner_phase}"
                )
            results[basename] = "not_due"
            continue
        text = path.read_text(encoding="utf-8")
        status = _metadata_value(text, "证据状态")
        if require_closed and phase >= owner_phase and status != "closed":
            raise EvidenceContractError(f"{basename} evidence status must be closed")
        if phase >= owner_phase:
            missing = [term for term in required_terms if term not in text]
            if missing:
                raise EvidenceContractError(
                    f"{basename} is missing required fields: {','.join(missing)}"
                )
        results[basename] = status or "present"
    return results


def validate_phase_evidence(
    repo_root: Path,
    *,
    phase: int,
    require_closed: bool,
) -> dict[str, object]:
    _validate_phase(phase)
    inventory_path = repo_root / ACTIVE_INVENTORY_RELATIVE
    if not inventory_path.is_file():
        raise EvidenceContractError("active-prd-inventory.md is required in phase 0")
    text = inventory_path.read_text(encoding="utf-8")
    _validate_inventory_metadata(text)
    status = _metadata_value(text, "证据状态")
    if require_closed and status != "closed":
        raise EvidenceContractError("active-prd-inventory.md evidence status must be closed")

    document_rows = _table_rows(
        text,
        "## 1. Active PRD disposition",
        (
            "document_path",
            "matched_legacy_terms",
            "disposition",
            "replacement_authority",
            "owner_phase",
            "status",
            "evidence_command",
        ),
    )
    test_rows = _table_rows(
        text,
        "## 2. Legacy test disposition",
        (
            "test_path",
            "matched_legacy_terms",
            "classification",
            "replacement_authority",
            "owner_phase",
            "status",
            "evidence_command",
        ),
    )
    entry_rows = _table_rows(
        text,
        "## 3. Execution and recovery entry inventory",
        (
            "entry_id",
            "code_anchor",
            "current_control",
            "replacement_authority",
            "owner_phase",
            "status",
            "evidence_command",
        ),
    )

    _validate_document_rows(repo_root, document_rows, phase=phase)
    _validate_test_rows(repo_root, test_rows, phase=phase)
    _validate_entry_rows(repo_root, entry_rows)

    handoffs = {"active-prd-inventory.md": status or "present"}
    handoffs.update(
        validate_handoff_schedule(
            repo_root,
            phase=phase,
            require_closed=require_closed,
        )
    )
    return {
        "status": "closed" if status == "closed" else "pending",
        "phase": phase,
        "active_prd_count": len(document_rows),
        "legacy_test_count": len(test_rows),
        "execution_entry_count": len(entry_rows),
        "handoffs": handoffs,
    }


def _validate_document_rows(
    repo_root: Path,
    rows: list[InventoryRow],
    *,
    phase: int,
) -> None:
    indexed = _unique_rows(rows, "document_path")
    discovered = collect_active_prd_matches(repo_root)
    if phase < 6:
        if set(indexed) != set(discovered):
            raise EvidenceContractError(
                _set_mismatch("active PRD inventory", set(discovered), set(indexed))
            )
    else:
        unregistered = set(discovered) - set(indexed)
        if unregistered:
            raise EvidenceContractError(
                f"active PRD inventory has new unregistered matches: {sorted(unregistered)}"
            )
        pending = sorted(
            path for path, row in indexed.items() if row.values["status"] == "registered"
        )
        if pending:
            raise EvidenceContractError(
                f"active PRD dispositions remain registered after phase 6: {pending}"
            )
    for path, row in indexed.items():
        values = row.values
        _require_value(values, "replacement_authority", path)
        _validate_owner_phase(values["owner_phase"], path)
        _require_evidence_command(values["evidence_command"], path)
        if values["disposition"] not in DISPOSITIONS:
            raise EvidenceContractError(f"{path}: invalid disposition")
        _validate_replacement_authority(values["replacement_authority"], path)
        if values["status"] not in ROW_STATUSES:
            raise EvidenceContractError(f"{path}: invalid status")
        if path in discovered:
            recorded = _term_tuple(values["matched_legacy_terms"])
            if recorded != discovered[path]:
                raise EvidenceContractError(f"{path}: matched legacy terms drifted")
        elif phase < 6:
            raise EvidenceContractError(f"{path}: document disappeared before phase 6")


def _validate_test_rows(
    repo_root: Path,
    rows: list[InventoryRow],
    *,
    phase: int,
) -> None:
    indexed = _unique_rows(rows, "test_path")
    discovered = collect_legacy_test_matches(repo_root)
    if phase < 6:
        if set(indexed) != set(discovered):
            raise EvidenceContractError(
                _set_mismatch("legacy test inventory", set(discovered), set(indexed))
            )
    else:
        unregistered = set(discovered) - set(indexed)
        if unregistered:
            raise EvidenceContractError(
                f"legacy test inventory has new unregistered matches: {sorted(unregistered)}"
            )
    for path, row in indexed.items():
        values = row.values
        _require_value(values, "replacement_authority", path)
        _validate_owner_phase(values["owner_phase"], path)
        _require_evidence_command(values["evidence_command"], path)
        if values["classification"] not in TEST_CLASSIFICATIONS:
            raise EvidenceContractError(f"{path}: invalid test classification")
        _validate_replacement_authority(values["replacement_authority"], path)
        if values["status"] not in ROW_STATUSES:
            raise EvidenceContractError(f"{path}: invalid status")
        if path in discovered:
            recorded = _term_tuple(values["matched_legacy_terms"])
            if recorded != discovered[path]:
                raise EvidenceContractError(f"{path}: matched legacy terms drifted")
        elif phase < 6:
            raise EvidenceContractError(f"{path}: test disappeared before phase 6")


def _validate_entry_rows(repo_root: Path, rows: list[InventoryRow]) -> None:
    indexed = _unique_rows(rows, "entry_id")
    if set(indexed) != REQUIRED_ENTRY_IDS:
        raise EvidenceContractError(
            _set_mismatch("execution entry inventory", REQUIRED_ENTRY_IDS, set(indexed))
        )
    for entry_id, row in indexed.items():
        values = row.values
        for key in ("current_control", "replacement_authority"):
            _require_value(values, key, entry_id)
        _validate_owner_phase(values["owner_phase"], entry_id)
        _require_evidence_command(values["evidence_command"], entry_id)
        if values["status"] not in ROW_STATUSES:
            raise EvidenceContractError(f"{entry_id}: invalid status")
        anchor = values["code_anchor"]
        path_text, separator, symbol = anchor.partition("::")
        if not separator or not symbol:
            raise EvidenceContractError(f"{entry_id}: invalid code anchor")
        source_path = repo_root / path_text
        if not source_path.is_file():
            raise EvidenceContractError(f"{entry_id}: code anchor file is missing")
        if symbol not in source_path.read_text(encoding="utf-8"):
            raise EvidenceContractError(f"{entry_id}: code anchor symbol is missing")


def _table_rows(
    text: str,
    heading: str,
    expected_headers: tuple[str, ...],
) -> list[InventoryRow]:
    lines = text.splitlines()
    try:
        start = lines.index(heading) + 1
    except ValueError as exc:
        raise EvidenceContractError(f"missing inventory section: {heading}") from exc
    table_lines: list[str] = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        if line.strip().startswith("|"):
            table_lines.append(line.strip())
    if len(table_lines) < 2:
        raise EvidenceContractError(f"{heading}: table is missing")
    headers = tuple(_table_cells(table_lines[0]))
    if headers != expected_headers:
        raise EvidenceContractError(f"{heading}: table headers do not match contract")
    rows: list[InventoryRow] = []
    for line in table_lines[2:]:
        cells = _table_cells(line)
        if len(cells) != len(headers):
            raise EvidenceContractError(f"{heading}: row width does not match headers")
        rows.append(InventoryRow(dict(zip(headers, cells, strict=True))))
    return rows


def _table_cells(line: str) -> list[str]:
    return [_strip_inline_code(cell.strip()) for cell in line.strip("|").split("|")]


def _strip_inline_code(value: str) -> str:
    if len(value) >= 2 and value.startswith("`") and value.endswith("`"):
        return value[1:-1]
    return value


def _metadata_value(text: str, label: str) -> str:
    match = re.search(rf"^- \*\*{re.escape(label)}\*\*：([^\n]+)$", text, re.MULTILINE)
    return _strip_inline_code(match.group(1).strip()) if match else ""


def _validate_inventory_metadata(text: str) -> None:
    if _metadata_value(text, "证据Schema") != ACTIVE_INVENTORY_SCHEMA:
        raise EvidenceContractError("active-prd-inventory.md schema is invalid")
    if _metadata_value(text, "适用分支") != "main":
        raise EvidenceContractError("active-prd-inventory.md branch must be main")
    if not re.fullmatch(r"[0-9a-f]{7,40}", _metadata_value(text, "基线Commit")):
        raise EvidenceContractError("active-prd-inventory.md baseline commit is invalid")
    if not re.fullmatch(r"[0-9a-f]{40}", _metadata_value(text, "基线Tree")):
        raise EvidenceContractError("active-prd-inventory.md baseline tree is invalid")
    if _metadata_value(text, "验证命令") != CURRENT_EVIDENCE_COMMAND:
        raise EvidenceContractError("active-prd-inventory.md validation command drifted")


def _unique_rows(rows: Iterable[InventoryRow], key: str) -> dict[str, InventoryRow]:
    result: dict[str, InventoryRow] = {}
    for row in rows:
        value = row.values[key]
        if not value or value in result:
            raise EvidenceContractError(f"duplicate or empty {key}: {value}")
        result[value] = row
    return result


def _matched_terms(text: str, terms: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(term for term in terms if term in text))


def _term_tuple(value: str) -> tuple[str, ...]:
    return tuple(sorted(item.strip() for item in value.split(",") if item.strip()))


def _is_agent_replacement_test(path: Path) -> bool:
    name = path.name
    return name.startswith("test_agent_") or name in {
        "test_unified_agent_loop_evidence_contract.py",
        "test_migrate_unified_agent_loop_schema.py",
    }


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _require_value(values: dict[str, str], key: str, identity: str) -> None:
    if not values[key]:
        raise EvidenceContractError(f"{identity}: {key} is required")


def _require_evidence_command(value: str, identity: str) -> None:
    if value != CURRENT_EVIDENCE_COMMAND:
        raise EvidenceContractError(f"{identity}: evidence command is invalid")


def _validate_replacement_authority(value: str, identity: str) -> None:
    aliases = [item.strip() for item in value.split(",") if item.strip()]
    if not aliases or any(not re.fullmatch(r"UA-P[0-7]", item) for item in aliases):
        raise EvidenceContractError(f"{identity}: replacement authority is invalid")


def _validate_owner_phase(value: str, identity: str) -> None:
    if not re.fullmatch(r"Phase [0-7]", value):
        raise EvidenceContractError(f"{identity}: owner_phase is invalid")


def _validate_phase(phase: int) -> None:
    if phase < 0 or phase > 7:
        raise EvidenceContractError("phase must be between 0 and 7")


def _set_mismatch(label: str, expected: set[str], actual: set[str]) -> str:
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    return f"{label} mismatch: missing={missing}, extra={extra}"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate unified Agent Loop phased handoff evidence."
    )
    parser.add_argument("--phase", type=int, required=True)
    parser.add_argument("--require-closed", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    try:
        result = validate_phase_evidence(
            args.repo_root.resolve(),
            phase=args.phase,
            require_closed=args.require_closed,
        )
    except EvidenceContractError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"unified_agent_loop_phase_{args.phase}_evidence_{result['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
