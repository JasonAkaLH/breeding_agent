from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .catalog import SkillCatalog
from .contract import SkillContract, SkillContractDiagnostic
from .skill_capabilities import SkillCapabilityRegistry, build_skill_capability_registry


_LEGACY_REVISION_RE = re.compile(r"^skillrev-[0-9]{6,}-[0-9a-f]{12}$")
_V2_REVISION_RE = re.compile(r"^skillrev-v2-[0-9a-f]{64}$")


class SkillBundleRevisionError(LookupError):
    def __init__(self, safe_error_code: str) -> None:
        super().__init__(safe_error_code)
        self.safe_error_code = safe_error_code


def classify_skill_bundle_revision(revision: object) -> str:
    normalized = str(revision or "").strip()
    if not normalized or _LEGACY_REVISION_RE.fullmatch(normalized):
        return "retired"
    if _V2_REVISION_RE.fullmatch(normalized):
        return "v2"
    return "invalid"


@dataclass(slots=True, frozen=True)
class SkillRuntimeBundle:
    revision: str
    created_at: datetime
    skill_roots: tuple[Path, ...]
    public_skill_roots: tuple[Path, ...]
    fingerprint: tuple[tuple[str, str], ...]
    catalog: SkillCatalog
    skill_capabilities: SkillCapabilityRegistry
    contracts_by_skill_name: dict[str, SkillContract]
    contract_by_capability_id: dict[str, SkillContract]
    contract_diagnostics: tuple[SkillContractDiagnostic, ...] = ()
    script_package_snapshot: bool = False


@dataclass(slots=True, frozen=True)
class SkillRuntimeRefreshResult:
    status: str
    reason: str
    previous_revision: str
    active_revision: str
    registered_count: int
    skipped_count: int
    duration_ms: int
    script_package_snapshot: bool
    error_type: str = ""


class SkillRuntimeState:
    """Process-local Skill bundle state used by planning, macro expansion and execution.

    The state keeps immutable bundle snapshots by revision.  New tasks use the
    active bundle; running tasks can retain their planning revision so later
    refreshes do not change forced-skill manifest resolution mid-task.
    """

    def __init__(
        self,
        *,
        skill_roots: Iterable[str | Path],
        public_skill_roots: Iterable[str | Path],
        reserved_capability_ids: Iterable[str],
        initial_catalog: SkillCatalog | None = None,
        refresh_enabled: bool = True,
    ) -> None:
        self._skill_roots = tuple(Path(root).expanduser() for root in skill_roots)
        self._public_skill_roots = tuple(Path(root).expanduser() for root in public_skill_roots)
        self._reserved_capability_ids = tuple(reserved_capability_ids)
        self._refresh_enabled = refresh_enabled
        self._retained_counts: dict[str, int] = {}
        self._bundles: dict[str, SkillRuntimeBundle] = {}
        fingerprint = self._fingerprint_roots(self._skill_roots)
        catalog = initial_catalog or SkillCatalog.from_roots(self._skill_roots)
        bundle = self._make_bundle(catalog=catalog, fingerprint=fingerprint)
        self._active_revision = bundle.revision
        self._bundles[bundle.revision] = bundle

    @classmethod
    def from_roots(
        cls,
        *,
        skill_roots: Iterable[str | Path],
        public_skill_roots: Iterable[str | Path],
        reserved_capability_ids: Iterable[str],
    ) -> "SkillRuntimeState":
        return cls(
            skill_roots=skill_roots,
            public_skill_roots=public_skill_roots,
            reserved_capability_ids=reserved_capability_ids,
        )

    @property
    def active_revision(self) -> str:
        return self._active_revision

    @property
    def active_bundle(self) -> SkillRuntimeBundle:
        return self._bundles[self._active_revision]

    def bundle_for_revision(self, revision: str | None = None) -> SkillRuntimeBundle:
        normalized = str(revision or "").strip()
        classification = classify_skill_bundle_revision(normalized)
        if classification == "retired":
            raise SkillBundleRevisionError("agent_skill_bundle_revision_retired")
        if classification == "invalid":
            raise SkillBundleRevisionError("agent_skill_bundle_revision_invalid")
        try:
            return self._bundles[normalized]
        except KeyError as exc:
            raise SkillBundleRevisionError(
                "agent_skill_bundle_revision_unavailable"
            ) from exc

    def activate_revision(self, revision: str) -> None:
        self.bundle_for_revision(revision)
        self._active_revision = revision

    def catalog_for_revision(self, revision: str | None = None) -> SkillCatalog:
        return self.bundle_for_revision(revision).catalog

    def skill_name_for_capability(self, capability_id: str, revision: str | None = None) -> str | None:
        bundle = self.bundle_for_revision(revision)
        return bundle.skill_capabilities.skill_name_by_capability_id.get(capability_id)

    def active_skill_capability_ids(self) -> tuple[str, ...]:
        return tuple(self.active_bundle.skill_capabilities.skill_name_by_capability_id)

    def known_skill_capability_ids(self) -> tuple[str, ...]:
        known: set[str] = set()
        for bundle in self._bundles.values():
            known.update(bundle.skill_capabilities.skill_name_by_capability_id)
        return tuple(sorted(known))

    def retain_revision(self, revision: str | None) -> None:
        bundle = self.bundle_for_revision(revision)
        self._retained_counts[bundle.revision] = (
            self._retained_counts.get(bundle.revision, 0) + 1
        )

    def release_revision(self, revision: str | None) -> None:
        if not revision:
            return
        count = self._retained_counts.get(revision, 0)
        if count <= 1:
            self._retained_counts.pop(revision, None)
        else:
            self._retained_counts[revision] = count - 1
        self._evict_unretained_inactive_bundles()

    def refresh_if_changed(self, *, reason: str, force: bool = False) -> SkillRuntimeRefreshResult:
        started = time.monotonic()
        previous = self.active_bundle
        if not self._refresh_enabled:
            return self._refresh_result(
                status="skipped",
                reason="disabled",
                previous_revision=previous.revision,
                active_revision=previous.revision,
                started=started,
            )

        fingerprint = self._fingerprint_roots(self._skill_roots)
        if not force and fingerprint == previous.fingerprint:
            return self._refresh_result(
                status="skipped",
                reason="fingerprint_unchanged",
                previous_revision=previous.revision,
                active_revision=previous.revision,
                started=started,
            )

        try:
            catalog = SkillCatalog.from_roots(self._skill_roots)
            bundle = self._make_bundle(catalog=catalog, fingerprint=fingerprint)
        except Exception as exc:  # Defensive: callers should be able to keep the previous bundle.
            return self._refresh_result(
                status="failed",
                reason=reason,
                previous_revision=previous.revision,
                active_revision=previous.revision,
                started=started,
                error_type=type(exc).__name__,
            )

        self._bundles[bundle.revision] = bundle
        self._active_revision = bundle.revision
        self._evict_unretained_inactive_bundles()
        return self._refresh_result(
            status="completed",
            reason=reason,
            previous_revision=previous.revision,
            active_revision=bundle.revision,
            started=started,
            registered_count=len(bundle.skill_capabilities.descriptors),
            skipped_count=len(bundle.skill_capabilities.diagnostics),
            script_package_snapshot=bundle.script_package_snapshot,
        )

    def _make_bundle(
        self,
        *,
        catalog: SkillCatalog,
        fingerprint: tuple[tuple[str, str], ...],
    ) -> SkillRuntimeBundle:
        revision = f"skillrev-v2-{self._fingerprint_digest(fingerprint)}"
        capabilities = build_skill_capability_registry(
            catalog,
            public_skill_roots=self._public_skill_roots,
            reserved_capability_ids=self._reserved_capability_ids,
        )
        contracts_by_skill_name = {
            skill.name: skill.contract
            for skill in catalog.skills
            if skill.contract is not None
        }
        contract_by_capability_id = {
            capability_id: contracts_by_skill_name[skill_name]
            for capability_id, skill_name in capabilities.skill_name_by_capability_id.items()
            if skill_name in contracts_by_skill_name
        }
        contract_diagnostics = tuple(
            diagnostic
            for skill in catalog.skills
            for diagnostic in skill.contract_diagnostics
        )
        return SkillRuntimeBundle(
            revision=revision,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            skill_roots=self._skill_roots,
            public_skill_roots=self._public_skill_roots,
            fingerprint=fingerprint,
            catalog=catalog,
            skill_capabilities=capabilities,
            contracts_by_skill_name=contracts_by_skill_name,
            contract_by_capability_id=contract_by_capability_id,
            contract_diagnostics=contract_diagnostics,
            script_package_snapshot=False,
        )

    def _evict_unretained_inactive_bundles(self) -> None:
        for revision in list(self._bundles):
            if revision == self._active_revision:
                continue
            if self._retained_counts.get(revision, 0) > 0:
                continue
            self._bundles.pop(revision, None)

    def _refresh_result(
        self,
        *,
        status: str,
        reason: str,
        previous_revision: str,
        active_revision: str,
        started: float,
        registered_count: int | None = None,
        skipped_count: int | None = None,
        script_package_snapshot: bool | None = None,
        error_type: str = "",
    ) -> SkillRuntimeRefreshResult:
        active = self._bundles[active_revision]
        return SkillRuntimeRefreshResult(
            status=status,
            reason=reason,
            previous_revision=previous_revision,
            active_revision=active_revision,
            registered_count=len(active.skill_capabilities.descriptors) if registered_count is None else registered_count,
            skipped_count=len(active.skill_capabilities.diagnostics) if skipped_count is None else skipped_count,
            duration_ms=int((time.monotonic() - started) * 1000),
            script_package_snapshot=active.script_package_snapshot if script_package_snapshot is None else script_package_snapshot,
            error_type=error_type,
        )

    @staticmethod
    def _fingerprint_roots(roots: tuple[Path, ...]) -> tuple[tuple[str, str], ...]:
        entries: list[tuple[str, str]] = []
        for root in roots:
            root_path = root.expanduser()
            if not root_path.exists():
                continue
            for skill_file in sorted(root_path.rglob("SKILL.md")):
                skill_dir = skill_file.parent
                for file_path in sorted(path for path in skill_dir.rglob("*") if path.is_file()):
                    if "__pycache__" in file_path.parts or file_path.suffix == ".pyc":
                        continue
                    try:
                        stat = file_path.stat()
                        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
                    except OSError:
                        continue
                    entries.append((str(file_path.resolve()), f"{stat.st_size}:{digest}"))
        return tuple(entries)

    @staticmethod
    def _fingerprint_digest(fingerprint: tuple[tuple[str, str], ...]) -> str:
        raw = "\n".join(f"{path}\t{digest}" for path, digest in fingerprint)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
