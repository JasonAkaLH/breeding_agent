from __future__ import annotations

import ast
from collections import Counter
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
REPOSITORIES = "src/storage/sqlite/repositories.py"
RUNTIME = "src/api/runtime.py"
SITE_OWNERS = {
    (
        "src/storage/sqlite/agent_repository.py",
        "SQLiteAgentRepository",
        "_commit_final",
    ): "sql_agent_final",
    (
        REPOSITORIES,
        "SQLiteStateRepository",
        "admit_submission_sql",
    ): "submission_sql_admit",
    (
        REPOSITORIES,
        "SQLiteStateRepository",
        "project_submission_admission",
    ): "submission_projection",
    (REPOSITORIES, "SQLiteStateRepository", "_save_message"): "save_message",
    (
        REPOSITORIES,
        "SQLiteStateRepository",
        "_upsert_file_upload_message",
    ): "file_upload_upsert",
}
RAW_INSERT = re.compile(
    r"\binsert\s+(?:or\s+\w+\s+)?into\s+"
    r"(?:(?:[\w\"`\[\]]+)\s*\.\s*)?[\"`\[]?messages\b",
    re.IGNORECASE,
)
FORBIDDEN_ORM_WRITES = {
    "bulk_insert_mappings",
    "bulk_save_objects",
    "bulk_update_mappings",
    "merge",
}


def _leaf(node: ast.AST) -> str | None:
    return (
        node.id
        if isinstance(node, ast.Name)
        else node.attr
        if isinstance(node, ast.Attribute)
        else None
    )


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    return sorted(
        (
            item
            for item in ast.walk(node)
            if isinstance(item, ast.Call) and _leaf(item.func) == name
        ),
        key=lambda item: (item.lineno, item.col_offset),
    )


def _methods(tree: ast.Module, class_name: str) -> dict[str, ast.AST]:
    owner = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return {
        node.name: node
        for node in owner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _encloses(definition: ast.AST, node: ast.AST) -> bool:
    return definition.lineno <= node.lineno <= definition.end_lineno  # type: ignore[attr-defined]


def _constructor_sites(trees: dict[str, ast.Module]) -> Counter[tuple[str, str, str]]:
    sites: list[tuple[str, str, str]] = []
    for path, tree in trees.items():
        definitions = list(ast.walk(tree))
        for call in _calls(tree, "MessageRow"):
            classes = [
                node
                for node in definitions
                if isinstance(node, ast.ClassDef) and _encloses(node, call)
            ]
            functions = [
                node
                for node in definitions
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and _encloses(node, call)
            ]
            sites.append(
                (
                    path,
                    max(classes, key=lambda node: node.lineno).name
                    if classes
                    else "<module>",
                    max(functions, key=lambda node: node.lineno).name
                    if functions
                    else "<module>",
                )
            )
    return Counter(sites)


def _references(node: ast.AST, names: set[str]) -> bool:
    return any(
        isinstance(item, (ast.Name, ast.Attribute)) and _leaf(item) in names
        for item in ast.walk(node)
    )


def _hidden_writes(trees: dict[str, ast.Module]) -> list[tuple[str, int, str]]:
    violations: list[tuple[str, int, str]] = []
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and RAW_INSERT.search(node.value)
            ):
                violations.append((path, node.lineno, "raw insert"))
            if isinstance(node, ast.Call):
                name = _leaf(node.func)
                if name in {
                    "insert",
                    "postgresql_insert",
                    "sqlite_insert",
                } and _references(node, {"MessageRow"}):
                    violations.append((path, node.lineno, "core insert"))
                if name in FORBIDDEN_ORM_WRITES and _references(node, {"MessageRow"}):
                    violations.append((path, node.lineno, name or "write"))

        functions = (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and _calls(node, "MessageRow")
        )
        for function in functions:
            for call in (
                item for item in ast.walk(function) if isinstance(item, ast.Call)
            ):
                if _leaf(call.func) in FORBIDDEN_ORM_WRITES:
                    violations.append((path, call.lineno, _leaf(call.func) or "write"))
    return sorted(set(violations))


class MessageIdentityInventoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.trees = {
            path.relative_to(ROOT).as_posix(): ast.parse(
                path.read_text(encoding="utf-8"),
                filename=path.relative_to(ROOT).as_posix(),
            )
            for path in sorted((ROOT / "src").rglob("*.py"))
        }
        cls.storage = _methods(cls.trees[REPOSITORIES], "SQLiteStorage")

    def test_production_message_row_constructor_inventory_is_closed(self) -> None:
        self.assertEqual(_constructor_sites(self.trees), Counter(SITE_OWNERS.keys()))

    def test_no_hidden_raw_core_bulk_or_merge_message_write(self) -> None:
        self.assertEqual(_hidden_writes(self.trees), [])

    def test_enforce_reserved_paths_reserve_before_their_approved_insert(self) -> None:
        reserve = ast.unparse(self.storage["_reserve_message_insert"])
        self.assertEqual(
            reserve.count("await self.reserve_message_identity(request)"), 1
        )

        save = ast.unparse(self.storage["save_message"])
        save_reserve = "result = await self._reserve_message_insert(reservation)"
        save_insert = "allow_insert=True"
        self.assertEqual((save.count(save_reserve), save.count(save_insert)), (1, 1))
        self.assertLess(save.index(save_reserve), save.index(save_insert))

        prepare = ast.unparse(self.storage["_prepare_file_upload_message_insert"])
        upload = ast.unparse(self.storage["upsert_file_upload_message"])
        upload_prepare = "await self._prepare_file_upload_message_insert("
        upload_insert = "state._upsert_file_upload_message("
        self.assertEqual(prepare.count("await self._reserve_message_insert("), 1)
        self.assertEqual(
            (upload.count(upload_prepare), upload.count(upload_insert)), (1, 1)
        )
        self.assertLess(upload.index(upload_prepare), upload.index(upload_insert))

    def test_a6_capability_gate_is_default_off_and_enforce_only(self) -> None:
        constructor = ast.unparse(self.storage["__init__"])
        active = ast.unparse(self.storage["_message_identity_authority_active"])
        disabled_guard = "if not self._message_identity_authority_active():"

        self.assertIn("message_identity_authority_enabled: bool=False", constructor)
        self.assertIn(
            "self._message_identity_authority_enabled = "
            "message_identity_authority_enabled",
            constructor,
        )
        self.assertIn("self._message_identity_authority_enabled", active)
        self.assertIn("self._task_authority_mode() == 'enforce'", active)
        for method_name in (
            "reserve_message_identity",
            "save_message",
            "_prepare_file_upload_message_insert",
        ):
            self.assertIn(disabled_guard, ast.unparse(self.storage[method_name]))

        build = next(
            node
            for node in self.trees[RUNTIME].body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "build_api_runtime"
        )
        self.assertIn(
            "message_identity_authority_enabled=canonical_task_authority_mode == "
            "'enforce'",
            ast.unparse(build),
        )

    def test_enforce_first_insert_owner_allowlist_is_closed(self) -> None:
        admission = self.storage["admit_submission"]
        sql_admission = _calls(admission, "_run_submission_admission")
        non_enforce = [
            node
            for node in ast.walk(admission)
            if isinstance(node, ast.If)
            and ast.unparse(node.test) == "self._task_authority_mode() != 'enforce'"
            and sql_admission
            and _encloses(node, sql_admission[0])
        ]
        self.assertEqual((len(sql_admission), len(non_enforce)), (1, 1))
        self.assertIn(sql_admission[0], list(ast.walk(non_enforce[0])))

        projection_callers = {
            name
            for name, method in self.storage.items()
            if _calls(method, "_run_submission_projection")
        }
        self.assertEqual(projection_callers, {"acknowledge_submission_projection"})
        self.assertEqual(
            len(
                _calls(
                    self.storage["_run_submission_projection"],
                    "project_submission_admission",
                )
            ),
            1,
        )

        build = next(
            node
            for node in self.trees[RUNTIME].body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "build_api_runtime"
        )
        rebind = "agent_repository = RuntimeSidecarAgentRepository("
        enforce = [
            guard
            for guard in ast.walk(build)
            if isinstance(guard, ast.If)
            and ast.unparse(guard.test) == "canonical_task_authority_mode == 'enforce'"
            if rebind in ast.unparse(guard)
        ]
        self.assertEqual(len(enforce), 1)
        enforce_source = ast.unparse(enforce[0])
        self.assertEqual(enforce_source.count(rebind), 1)
        build_source = ast.unparse(build)
        publisher = "final_output_publisher = AgentFinalOutputPublisher("
        self.assertEqual(build_source.count(publisher), 1)
        between = build_source[
            build_source.index(rebind) : build_source.index(publisher)
        ]
        self.assertEqual(between.count("agent_repository ="), 1)
        publisher_source = build_source[build_source.index(publisher) :]
        self.assertIn("runs=agent_repository", publisher_source)
        self.assertIn("writer=agent_repository", publisher_source)

        owners = {SITE_OWNERS[site] for site in _constructor_sites(self.trees)}
        owners -= {"submission_sql_admit", "sql_agent_final"}
        self.assertEqual(
            owners, {"submission_projection", "save_message", "file_upload_upsert"}
        )


if __name__ == "__main__":
    unittest.main()
