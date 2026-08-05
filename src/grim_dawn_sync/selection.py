"""Pure selection policy and short-lived, fail-closed selection plans.

This module deliberately does not acquire locks, alter a live save, or invoke
Git mutation.  Workflow integration is responsible for executing a validated
plan later.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from time import monotonic
from typing import Callable, Literal
import re
import math
import uuid

from grim_dawn_sync.errors import EXIT_CONFLICT, EXIT_VALIDATION, SyncError
from grim_dawn_sync.session_start import SESSION_START_ID_PATTERN
from grim_dawn_sync.version_catalog import SaveCandidate, VersionCatalog


SelectionMode = Literal["launch", "promote-only"]


class ReconcileCase(str, Enum):
    EQUAL = "equal"
    REMOTE_AHEAD = "remote_ahead"
    LIVE_AHEAD = "live_ahead"
    DIVERGED = "diverged"
    BASELINE_MISSING = "baseline_missing"


@dataclass(frozen=True)
class SelectionDirective:
    show_selector: bool
    initial_selection: Literal["live", "remote_head"] | None
    allowed_operations: tuple[str, ...]
    bookmark_displaced_remote: bool


_POLICY: dict[ReconcileCase, SelectionDirective] = {
    ReconcileCase.EQUAL: SelectionDirective(False, "remote_head", ("launch", "cancel", "reload"), False),
    ReconcileCase.REMOTE_AHEAD: SelectionDirective(True, "remote_head", ("launch", "promote", "bookmark", "cancel", "reload"), False),
    ReconcileCase.LIVE_AHEAD: SelectionDirective(True, "live", ("launch", "promote", "bookmark", "cancel", "reload"), True),
    ReconcileCase.DIVERGED: SelectionDirective(True, None, ("launch", "promote", "bookmark", "cancel", "reload"), True),
    ReconcileCase.BASELINE_MISSING: SelectionDirective(False, None, ("cancel",), False),
}


def classify_reconciliation(*, live_root_hash: str | None, remote_root_hash: str | None, baseline_root_hash: str | None) -> ReconcileCase:
    """Classify three roots without performing I/O or selecting a winner."""
    if not baseline_root_hash or not live_root_hash or not remote_root_hash:
        return ReconcileCase.BASELINE_MISSING
    if live_root_hash == remote_root_hash:
        return ReconcileCase.EQUAL
    if live_root_hash == baseline_root_hash:
        return ReconcileCase.REMOTE_AHEAD
    if remote_root_hash == baseline_root_hash:
        return ReconcileCase.LIVE_AHEAD
    return ReconcileCase.DIVERGED


SelectionPolicyName = Literal["on_diff", "always"]
_SELECTION_POLICY_NAMES = frozenset({"on_diff", "always"})


def selection_policy(case: ReconcileCase, *, policy: str = "on_diff") -> SelectionDirective:
    """Return the immutable policy for a reconciliation case.

    ``policy="always"`` may only widen ``show_selector`` to ``True``.  It never
    changes ``initial_selection``, ``allowed_operations``, or
    ``bookmark_displaced_remote``, and it never overrides the
    ``BASELINE_MISSING`` fail-closed error (which keeps ``show_selector=False``
    regardless of policy).
    """
    if policy not in _SELECTION_POLICY_NAMES:
        raise SyncError("invalid_selection_policy", "Selection policy is invalid.", EXIT_VALIDATION)
    try:
        resolved_case = ReconcileCase(case)
    except ValueError as error:
        raise SyncError("invalid_reconcile_case", "Selection state is invalid.", EXIT_VALIDATION) from error
    try:
        directive = _POLICY[resolved_case]
    except KeyError as error:
        raise SyncError("invalid_reconcile_case", "Selection state is invalid.", EXIT_VALIDATION) from error
    if policy == "always" and resolved_case != ReconcileCase.BASELINE_MISSING and not directive.show_selector:
        directive = replace(directive, show_selector=True)
    return directive


@dataclass(frozen=True)
class SelectionPlan:
    """An immutable pre-mutation record of a specific approved choice."""
    plan_nonce: str
    catalog_token: str
    candidate_id: str
    candidate_kind: str
    selected_commit: str | None
    selected_root_hash: str
    expected_remote_head: str | None
    expected_live_root_hash: str
    mode: SelectionMode
    bookmark_displaced_remote: bool
    display_name: str | None
    note: str | None
    confirmation_required: bool
    reconcile_case: ReconcileCase
    expected_context_digest: str | None
    expected_remote_identity: tuple[str, str] | None
    confirmed: bool = False


@dataclass(frozen=True)
class CancelledSelection:
    status: Literal["cancelled"] = "cancelled"


@dataclass(frozen=True)
class _CatalogRecord:
    catalog: VersionCatalog
    expires_at: float


@dataclass(frozen=True)
class _PlanRecord:
    canonical: SelectionPlan
    case: ReconcileCase


class SelectionRegistry:
    """In-memory capability registry for catalog-bound, short-lived choices."""

    def __init__(self, *, ttl_seconds: float = 300, clock: Callable[[], float] = monotonic, verified_bookmark_target: Callable[[SaveCandidate], bool] | None = None) -> None:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)) or not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl_seconds = float(ttl_seconds)
        self._clock = clock
        # Legacy tags are not an authorization mechanism.  The caller that
        # obtained verified managed-bookmark provenance must opt in explicitly.
        self._verified_bookmark_target = verified_bookmark_target
        self._records: dict[str, _CatalogRecord] = {}
        self._plans: dict[str, _PlanRecord] = {}

    def register(self, catalog: VersionCatalog) -> str:
        token = catalog.token
        if not isinstance(token, str) or len(token) < 16 or token in self._records:
            raise SyncError("invalid_catalog_token", "Catalog token is invalid.", EXIT_VALIDATION)
        self._records[token] = _CatalogRecord(catalog, self._now() + self._ttl_seconds)
        return token

    def catalog(self, token: str) -> VersionCatalog:
        record = self._records.get(token)
        if record is None:
            raise SyncError("invalid_catalog_token", "Catalog token is unknown or has been altered.", EXIT_VALIDATION)
        if self._now() >= record.expires_at:
            del self._records[token]
            raise SyncError("catalog_expired", "Catalog has expired; reload versions before selecting.", EXIT_CONFLICT)
        return record.catalog

    def _now(self) -> float:
        """Read an injected monotonic clock without accepting fail-open values."""
        try:
            value = self._clock()
        except Exception as error:
            raise SyncError("catalog_clock_invalid", "Catalog clock is unavailable.", EXIT_CONFLICT) from error
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise SyncError("catalog_clock_invalid", "Catalog clock is invalid.", EXIT_CONFLICT)
        return float(value)

    def build_plan(self, *, catalog_token: str, candidate_id: str, mode: SelectionMode, case: ReconcileCase, display_name: str | None = None, note: str | None = None,
                   expected_context_digest: str | None = None, expected_remote_identity: tuple[str, str] | None = None) -> SelectionPlan:
        if mode not in ("launch", "promote-only"):
            raise SyncError("invalid_selection_mode", "Selection mode is invalid.", EXIT_VALIDATION)
        directive = selection_policy(case)
        operation = "launch" if mode == "launch" else "promote"
        if operation not in directive.allowed_operations:
            raise SyncError("selection_operation_forbidden", "This reconciliation state does not permit that operation.", EXIT_VALIDATION)
        catalog = self.catalog(catalog_token)
        candidate = catalog.candidate(candidate_id)
        if candidate.kind not in {"live", "remote_head", "history", "bookmark", "legacy", "session_start"}:
            raise SyncError("candidate_not_authorized", "Selected version kind is not authorized.", EXIT_VALIDATION)
        if not isinstance(candidate.root_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", candidate.root_hash):
            raise SyncError("candidate_not_authorized", "Selected version has an invalid root hash.", EXIT_VALIDATION)
        if not isinstance(catalog.live_root_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", catalog.live_root_hash):
            raise SyncError("invalid_catalog_token", "Catalog live root is invalid.", EXIT_VALIDATION)
        if catalog.remote_head is not None and (not isinstance(catalog.remote_head, str) or not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", catalog.remote_head)):
            raise SyncError("invalid_catalog_token", "Catalog remote head is invalid.", EXIT_VALIDATION)
        # session_start candidates have no commit at all; they are a local,
        # tool-owned archive instead.  Every other non-live kind still requires
        # a verified commit target.
        if candidate.kind not in {"live", "session_start"} and (candidate.commit is None or not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", candidate.commit)):
            raise SyncError("candidate_not_authorized", "Selected version has no authorized commit target.", EXIT_VALIDATION)
        if candidate.kind == "session_start" and (
            not isinstance(candidate.source_archive_id, str) or not SESSION_START_ID_PATTERN.fullmatch(candidate.source_archive_id)
        ):
            raise SyncError("candidate_not_authorized", "Selected session-start archive is not authorized.", EXIT_VALIDATION)
        if candidate.kind in {"legacy", "bookmark"} and (
            self._verified_bookmark_target is None or not self._verified_bookmark_target(candidate)
        ):
            raise SyncError("candidate_not_authorized", "Selected bookmark target is not verified.", EXIT_VALIDATION)
        if ReconcileCase(case) == ReconcileCase.EQUAL and mode == "launch":
            displaced = False
        elif candidate.kind == "live" and mode == "promote-only":
            displaced = True
        else:
            displaced = candidate.kind != "remote_head"
        confirmation = candidate.kind in {"history", "bookmark", "legacy", "session_start"}
        plan = SelectionPlan(
            plan_nonce=uuid.uuid4().hex,
            catalog_token=catalog_token,
            candidate_id=candidate.candidate_id,
            candidate_kind=candidate.kind,
            selected_commit=candidate.commit,
            selected_root_hash=candidate.root_hash,
            expected_remote_head=catalog.remote_head,
            expected_live_root_hash=catalog.live_root_hash,
            mode=mode,
            bookmark_displaced_remote=displaced,
            display_name=display_name,
            note=note,
            confirmation_required=confirmation,
            reconcile_case=ReconcileCase(case),
            expected_context_digest=expected_context_digest,
            expected_remote_identity=expected_remote_identity,
        )
        self._plans[plan.plan_nonce] = _PlanRecord(plan, ReconcileCase(case))
        return plan

    def confirm(self, plan: SelectionPlan) -> SelectionPlan:
        canonical = self._canonical_plan(plan)
        if not canonical.confirmation_required:
            return canonical
        approved = replace(canonical, confirmed=True)
        self._plans[approved.plan_nonce] = _PlanRecord(approved, self._plans[approved.plan_nonce].case)
        return approved

    def revalidate(self, plan: SelectionPlan, *, live_manifest: Callable[[], dict], remote_head: Callable[[], str | None]) -> SelectionPlan:
        """Reject stale plans immediately before the workflow's first mutation."""
        canonical = self._canonical_plan(plan)
        if canonical.confirmation_required and not canonical.confirmed:
            raise SyncError("selection_confirmation_required", "This saved version needs a second confirmation.", EXIT_VALIDATION)
        live = live_manifest()
        current_root = live.get("root_hash") if isinstance(live, dict) else None
        current_remote = remote_head()
        if current_root != canonical.expected_live_root_hash or current_remote != canonical.expected_remote_head:
            raise SyncError("selection_stale", "Save data changed while selection was open; reload versions.", EXIT_CONFLICT)
        return canonical

    def resolve(self, plan: SelectionPlan) -> SelectionPlan:
        """Return the canonical server-side plan for a later executor."""
        return self._canonical_plan(plan)

    def _canonical_plan(self, plan: SelectionPlan) -> SelectionPlan:
        if not isinstance(plan, SelectionPlan):
            raise SyncError("selection_plan_tampered", "Selection plan is invalid.", EXIT_VALIDATION)
        record = self._plans.get(plan.plan_nonce)
        if record is None or plan != record.canonical:
            raise SyncError("selection_plan_tampered", "Selection plan does not match its registered authorization.", EXIT_VALIDATION)
        self._validate_bound_plan(record.canonical, record.case)
        return record.canonical

    def _validate_bound_plan(self, plan: SelectionPlan, case: ReconcileCase) -> None:
        catalog = self.catalog(plan.catalog_token)
        candidate = catalog.candidate(plan.candidate_id)
        if (
            candidate.kind != plan.candidate_kind
            or candidate.commit != plan.selected_commit
            or candidate.root_hash != plan.selected_root_hash
            or catalog.remote_head != plan.expected_remote_head
            or catalog.live_root_hash != plan.expected_live_root_hash
        ):
            raise SyncError("selection_plan_tampered", "Selection plan does not match its catalog.", EXIT_VALIDATION)
        directive = selection_policy(case)
        operation = "launch" if plan.mode == "launch" else "promote"
        if operation not in directive.allowed_operations:
            raise SyncError("selection_plan_tampered", "Selection plan operation no longer matches its policy.", EXIT_VALIDATION)


def cancel_selection() -> CancelledSelection:
    """Represent cancellation without reading or writing any external state."""
    return CancelledSelection()
