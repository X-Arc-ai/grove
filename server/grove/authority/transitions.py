"""Pure authority replay. Every result remains an unverified data claim.

Current entities point into validated event bytes. The projection hash uses an
incremental applied-intent prefix, so replay never copies the full effect log.
External principal, ledger, approval, lineage, and database checks are returned
as outstanding requirements; caller booleans cannot satisfy them.
"""

from dataclasses import dataclass, replace
from hashlib import sha256
from types import MappingProxyType

from .capacity import (
    COMPLETE_RESERVE,
    HARD,
    JournalUsage,
    assess_ordinary_append,
    saturation_reasons,
)
from .codec import (
    PROFILE,
    _budget,
    _hash_record,
    _parse,
    _tokens,
    choice_digest,
    complete_event_digest,
    encode_event,
    journal_event,
    record_metrics,
    scan_journal,
    typed_digest,
)
from .records import (
    CREDENTIAL_EFFECTS,
    LINEAGE_EFFECTS,
    OWNER_EFFECTS,
    RESTRICTION_KINDS,
    SCHEMAS,
    TYPES,
    UntrustedJournal,
    UntrustedReduction,
    VerificationRequirement,
    at_microseconds,
    refuse,
)

_RESTRICTIONS = frozenset(RESTRICTION_KINDS)
_OWNER = frozenset(OWNER_EFFECTS)
_CREDENTIAL = frozenset(CREDENTIAL_EFFECTS)
_LINEAGE = frozenset(LINEAGE_EFFECTS)
REQUIREMENT_CODES = frozenset(
    (
        "trusted_genesis",
        "current_principal",
        "owner_approval",
        "credential_flow",
        "source_origin",
        "observed_lineage",
        "service_configuration",
        "account_flow",
        "controller_receipt",
        "database_outcome",
        "ledger_content",
        "effect_references",
        "restore_artifact",
        "renewed_approval",
        "current_projection",
    )
)


@dataclass(frozen=True, slots=True)
class _Entity:
    base: tuple
    latest: tuple
    revision: int = 1
    status: str = "active"
    epoch: int = 0
    attempts: int = 0


@dataclass(frozen=True, slots=True)
class _Retry:
    intent: tuple
    outcome: str
    state_digest: str


class _State:
    __slots__ = (
        "header",
        "budget",
        "reservation",
        "installation_epoch",
        "authority_epoch",
        "erasure_revision",
        "ledger",
        "closed",
        "entities",
        "burned",
        "cancelled",
        "retries",
        "intent_ids",
        "effect_ids",
        "applied_hash",
        "applied_count",
        "requirements",
        "configuration",
    )

    def __init__(self, header, installation_epoch, budget, reservation):
        self.header, self.budget, self.reservation = header, budget, reservation
        self.installation_epoch = installation_epoch
        self.authority_epoch = self.erasure_revision = 0
        self.ledger, self.closed, self.configuration = None, False, None
        self.entities, self.retries = {}, {}
        self.burned, self.intent_ids, self.effect_ids = set(), set(), set()
        self.cancelled, self.requirements = [], []
        self.applied_hash = sha256(b'["grove.authority.projection.v1",{"applied":[')
        self.applied_count = 0

    def fork(self):
        self.budget.tick()
        result = object.__new__(_State)
        for name in self.__slots__:
            setattr(result, name, getattr(self, name))
        # The enclosing bounded scratch reservation covers both pointer tables.
        result.entities = dict(self.entities)
        result.burned = set(self.burned)
        result.cancelled = list(self.cancelled)
        result.applied_hash = self.applied_hash.copy()
        return result

    def requirement(self, code, event, intent=None, digest=None):
        if code not in REQUIREMENT_CODES:
            refuse("transition")
        self.reservation.grow(512)
        self.requirements.append(
            VerificationRequirement(
                code,
                event.event_id,
                None if intent is None else intent.intent_id,
                event.digest if digest is None else digest,
            )
        )

    def put(self, key, value):
        if key not in self.entities:
            self.reservation.grow(1024)
        self.entities[key] = value

    def burn(self, approval):
        if approval is not None:
            if approval in self.burned:
                refuse("transition")
            self.reservation.grow(160)
            self.burned.add(approval)

    def apply_projection(self, intent, renewed=None):
        approval = _approval_id(intent)
        self.burn(approval)
        self.burn(renewed)
        applied = TYPES["AppliedIntent"](
            intent_id=intent.intent_id, approval_id=approval, effects=intent.effects
        )
        if self.applied_count:
            self.applied_hash.update(b",")
        for chunk in _tokens(applied):
            self.budget.tick()
            self.applied_hash.update(chunk)
        self.applied_count += 1

    def digest(self):
        digest = self.applied_hash.copy()
        digest.update(b"]")
        reservation = self.budget.reserve(
            "encoder", 65536 + len(self.burned) * 24 + len(self.cancelled) * 24
        )
        values = (
            ("authority_epoch", self.authority_epoch),
            ("burned_approvals", tuple(sorted(self.burned))),
            ("cancelled", tuple(sorted(self.cancelled, key=lambda r: r.intent_id))),
            ("closed", self.closed),
            ("erasure_revision", self.erasure_revision),
            ("format", "grove.authority-state.v1"),
            ("installation_epoch", self.installation_epoch),
            ("installation_id", self.header.installation_id),
            ("ledger", self.ledger),
            ("owner_id", self.header.owner_id),
            ("profile", self.header.schema_profile),
            ("world_id", self.header.world_id),
        )
        try:
            for key, value in values:
                digest.update((',"' + key + '":').encode("ascii"))
                for chunk in _tokens(value):
                    self.budget.tick()
                    digest.update(chunk)
            digest.update(b"}]")
            return digest.hexdigest()
        finally:
            reservation.release()


def _approval_id(intent):
    auth = intent.authorization
    return auth.approval_id if auth.kind == "owner_approval" else None


def _view(record):
    if record._span is None:
        refuse("input_type")
    return record._span


def _load(view, budget):
    data, start, end, schema = view
    return _parse(data, schema, budget, start=start, end=end)[0]


def _ref_digest(ref, state):
    return typed_digest("grove.authority.reference.v1", (ref,), budget=state.budget)


def _entity_key(effect, state):
    kind = effect.kind
    if kind.startswith("class_"):
        return "class", effect.class_id
    if kind == "policy_replace":
        return "policy", effect.policy_id
    if kind == "world_exclusions_replace":
        return "exclusions", "world"
    if kind == "assignment_set":
        return "assignment", effect.assignment_id
    if kind == "connection_rule_set":
        return "connection", effect.rule_id
    if kind == "grant_set":
        return "grant", effect.grant.grant_id
    if kind == "grant_revoke":
        return "grant", effect.grant_id
    if kind.startswith("credential_"):
        return "credential", effect.credential_id
    if kind == "family_revoke":
        return "family", effect.family_id
    if kind == "run_state":
        return "run", effect.run_id
    if kind == "lineage_derivation":
        return "derivation", effect.derivation_id
    if kind == "lineage_output":
        return "output", effect.output_id
    if kind == "lineage_dependency":
        return "dependency", effect.dependency_id
    if kind == "lineage_invalidate":
        return "lineage_run", effect.run_id
    return kind, "world"


def classify_effects(effects):
    if all(
        e.kind in _RESTRICTIONS or (e.kind == "run_state" and e.state != "active") for e in effects
    ):
        return "restrictive"
    return "ordinary"


def _pure_expansion(effects):
    # Existing class/policy/assignment changes may narrow AND widen. Never cancel
    # those on a guessed semantic interpretation of names or caller flags.
    for e in effects:
        if e.kind in ("class_create", "grant_set", "credential_issue"):
            continue
        if e.kind in ("policy_replace", "connection_rule_set"):
            if e.expected_revision == 0 and e.state == "active":
                continue
        if e.kind == "assignment_set" and e.expected_revision == 0 and e.classes:
            continue
        return False
    return True


def _ordered_effects(state, intent):
    reservation = state.budget.reserve("scratch", 1024 + len(intent.effects) * 256)
    try:
        return _dependency_order(state, intent, reservation)
    finally:
        reservation.release()


def _dependency_order(state, intent, reservation):
    effects = intent.effects
    creators, dependencies, extra_keys = {}, {}, set()
    for index, effect in enumerate(effects):
        state.budget.tick()
        key = _entity_key(effect, state)
        if key in creators or effect.effect_id in state.effect_ids:
            refuse("transition")
        creators[key] = index
        dependencies[index] = set()
        if effect.kind == "assignment_set":
            unique = ("ref", _ref_digest(effect.ref, state))
        elif effect.kind == "policy_replace":
            unique = ("grant_policy", effect.grant.grant_id)
        elif effect.kind == "lineage_dependency":
            unique = ("slot", _hash_record(effect.output_slot), effect.output_version)
        elif effect.kind == "lineage_derivation":
            unique = ("call", effect.run_id, effect.model_call_id)
        else:
            unique = key
        if unique in extra_keys:
            refuse("transition")
        extra_keys.add(unique)
    for index, effect in enumerate(effects):
        refs = []
        if effect.kind in ("policy_replace", "grant_revoke"):
            grant_id = effect.grant.grant_id if effect.kind == "policy_replace" else effect.grant_id
            refs.append(("grant", grant_id))
        if effect.kind == "policy_replace":
            refs.extend(("class", rule.class_id) for rule in effect.rules)
        if effect.kind == "connection_rule_set":
            refs.extend((("class", effect.class_id), ("grant", effect.grant_id)))
        if effect.kind == "assignment_set":
            refs.extend(("class", rule.class_id) for rule in effect.classes)
            if intent.authorization.kind == "prospective_assignment":
                refs.extend(("connection", rule.rule_id) for rule in effect.classes)
        if effect.kind == "credential_issue":
            binding = effect.binding
            if hasattr(binding, "grant"):
                refs.append(("grant", binding.grant.grant_id))
            if binding.kind == "runner_pass":
                refs.append(("run", binding.run_id))
            # Burn/consume the source before issuing a successor, independent of IDs.
            refs.extend(
                ("credential", e.credential_id) for e in effects if e.kind == "credential_consume"
            )
        if effect.kind == "run_state" and effect.state == "active":
            refs.extend(
                ("run", e.run_id) for e in effects if e.kind == "run_state" and e.state != "active"
            )
        if effect.kind in _LINEAGE:
            refs.append(("run", effect.run_id)) if hasattr(effect, "run_id") else None
        if effect.kind == "lineage_derivation" and effect.previous_id is not None:
            refs.append(("derivation", effect.previous_id))
        if effect.kind == "lineage_output":
            refs.append(("derivation", effect.derivation_id))
        if effect.kind == "lineage_dependency":
            refs.extend((("derivation", effect.derivation_id), ("output", effect.output_id)))
        for key in refs:
            if key in creators and creators[key] != index:
                if creators[key] not in dependencies[index]:
                    reservation.grow(64)
                dependencies[index].add(creators[key])
    ordered, remaining = [], set(range(len(effects)))
    while remaining:
        state.budget.tick(len(remaining))
        ready = sorted(i for i in remaining if not (dependencies[i] & remaining))
        if not ready:
            refuse("transition")
        ordered.extend(effects[i] for i in ready)
        remaining.difference_update(ready)
    return ordered


def _authorize(state, intent):
    auth, effects = intent.authorization, intent.effects
    kind = auth.kind
    if kind == "owner_approval":
        if auth.user_id != state.header.owner_id or auth.approval_id in state.burned:
            refuse("identity")
        allowed = _OWNER
    elif kind == "credential_transition":
        if (
            auth.installation_epoch != state.installation_epoch
            or auth.authority_epoch != state.authority_epoch
        ):
            refuse("identity")
        entity = state.entities.get(("credential", auth.credential_id))
        if entity is None:
            refuse("identity")
        old = _load(entity.base, state.budget)
        binding = old.binding
        if (
            binding.kind != auth.credential_kind
            or old.installation_epoch != auth.installation_epoch
            or old.authority_epoch != auth.authority_epoch
        ):
            refuse("identity")
        if hasattr(binding, "grant"):
            if (
                auth.client != binding.grant.client
                or auth.grant_id != binding.grant.grant_id
                or auth.user_id != binding.grant.user_id
                or auth.family_id != getattr(binding, "family_id", None)
            ):
                refuse("identity")
        elif auth.user_id != getattr(binding, "user_id", None):
            refuse("identity")
        if entity.status != "active" and not all(
            e.kind in ("family_revoke", "credential_revoke")
            or (e.kind == "credential_consume" and e.outcome == "reused")
            for e in effects
        ):
            refuse("transition")
        allowed = _CREDENTIAL
    elif kind == "prospective_assignment":
        allowed = frozenset(("assignment_set",))
    elif kind == "observed_lineage":
        allowed = _LINEAGE
    elif kind == "service_transition":
        if state.configuration is not None and auth.configuration_digest != state.configuration:
            refuse("identity")
        allowed = frozenset(("run_state", "credential_issue", "credential_revoke"))
    elif kind == "account_flow":
        allowed = frozenset(("credential_issue", "credential_revoke"))
    elif kind == "controller_operation":
        if auth.genesis_digest != state.header.genesis_digest:
            refuse("identity")
        allowed = {
            "birth": frozenset(("origin",)),
            "adopt": frozenset(("origin",)),
            "restore": frozenset(("restore_begin", "epoch_invalidate")),
            "close": frozenset(("world_close",)),
            "recover": _RESTRICTIONS | frozenset(("configuration_replace", "run_state")),
        }[auth.purpose]
    else:
        refuse("transition")
    for effect in effects:
        if effect.kind not in allowed:
            refuse("transition")
        if kind == "owner_approval" and hasattr(effect, "approval_id"):
            if effect.approval_id != auth.approval_id:
                refuse("identity")
        if kind == "service_transition":
            if effect.kind == "credential_issue" and effect.binding.kind != "runner_pass":
                refuse("transition")
            if effect.kind == "run_state" and (
                effect.agent != auth.agent
                or effect.configuration_digest != auth.configuration_digest
            ):
                refuse("identity")
            if (
                effect.kind == "run_state"
                and effect.state == "active"
                and effect.run_id != auth.run_id
            ):
                refuse("identity")
        if (
            kind == "controller_operation"
            and effect.kind == "run_state"
            and effect.state == "active"
        ):
            refuse("transition")
        if kind == "account_flow" and effect.kind == "credential_issue":
            if effect.binding.kind not in ("session", "email_code", "auth_state"):
                refuse("transition")
        if (
            kind == "credential_transition"
            and effect.kind in ("credential_attempt", "credential_consume", "credential_revoke")
            and effect.credential_id != auth.credential_id
        ):
            refuse("identity")
        if kind == "credential_transition" and effect.kind == "family_revoke":
            if (
                effect.family_id != auth.family_id
                or effect.grant_id != auth.grant_id
                or effect.grant_epoch != binding.grant.grant_epoch
            ):
                refuse("identity")
        if effect.kind == "credential_revoke" and kind in ("service_transition", "account_flow"):
            target = state.entities.get(("credential", effect.credential_id))
            if target is None:
                refuse("identity")
            target_binding = _load(target.base, state.budget).binding
            if kind == "service_transition":
                if (
                    target_binding.kind != "runner_pass"
                    or target_binding.run_id != auth.run_id
                    or target_binding.agent != auth.agent
                    or target_binding.configuration_digest != auth.configuration_digest
                ):
                    refuse("identity")
            elif target_binding.kind not in ("email_code", "auth_state", "session"):
                refuse("transition")


def _active(state, key):
    entity = state.entities.get(key)
    if entity is None or entity.status != "active":
        refuse("transition")
    return entity


def _grant(state, binding):
    entity = _active(state, ("grant", binding.grant_id))
    if entity.epoch != binding.grant_epoch:
        refuse("identity")
    grant = _load(entity.base, state.budget)
    if grant.grant != binding or binding.user_id != state.header.owner_id:
        refuse("identity")
    return grant


def _revoke_family(state, family_id, grant_id, epoch):
    old = state.entities.get(("family", family_id))
    if old is None or old.epoch != epoch:
        refuse("identity")
    base = _load(old.base, state.budget)
    if base.binding.grant.grant_id != grant_id:
        refuse("identity")
    state.put(("family", family_id), replace(old, status="revoked"))
    for key, entity in tuple(state.entities.items()):
        if key[0] == "credential":
            issue = _load(entity.base, state.budget)
            if getattr(issue.binding, "family_id", None) == family_id:
                state.put(key, replace(entity, status="revoked"))


def _apply_effect(state, effect, intent):
    kind, key = effect.kind, _entity_key(effect, state)
    old = state.entities.get(key)
    span = _view(effect)
    if kind == "origin":
        if (
            state.authority_epoch
            or state.ledger is not None
            or effect.owner_id != state.header.owner_id
        ):
            refuse("transition")
        state.authority_epoch, state.ledger = 1, effect.ledger
        return
    if hasattr(effect, "expected_revision"):
        current = state.erasure_revision if kind == "forget_apply" else (old.revision if old else 0)
        if effect.expected_revision != current:
            refuse("transition")
    if kind == "class_create":
        if old is not None or sum(k[0] == "class" for k in state.entities) >= 256:
            refuse("transition")
        state.put(key, _Entity(span, span))
    elif kind in ("class_label", "class_retire"):
        old = _active(state, key)
        state.put(
            key,
            replace(
                old,
                latest=span,
                revision=effect.revision,
                status="retired" if kind == "class_retire" else "active",
            ),
        )
    elif kind == "grant_set":
        if (old.epoch if old else 0) != effect.expected_epoch:
            refuse("transition")
        if effect.grant.user_id != state.header.owner_id:
            refuse("identity")
        if not any(e.kind == "policy_replace" and e.grant == effect.grant for e in intent.effects):
            refuse("transition")
        if not any(
            e.kind == "connection_rule_set"
            and e.grant_id == effect.grant.grant_id
            and e.grant_epoch == effect.grant.grant_epoch
            for e in intent.effects
        ):
            refuse("transition")
        if old is not None:
            _revoke_grant_children(state, effect.grant.grant_id)
        state.put(key, _Entity(span, span, epoch=effect.grant.grant_epoch))
    elif kind == "policy_replace":
        _grant(state, effect.grant)
        if old and _load(old.base, state.budget).grant.grant_id != effect.grant.grant_id:
            refuse("identity")
        for rule in effect.rules:
            _active(state, ("class", rule.class_id))
        for other_key, entity in state.entities.items():
            if other_key[0] == "policy" and other_key != key:
                if _load(entity.latest, state.budget).grant.grant_id == effect.grant.grant_id:
                    refuse("identity")
        state.put(
            key,
            _Entity(
                span if old is None else old.base,
                span,
                effect.revision,
                effect.state,
                effect.grant.grant_epoch,
            ),
        )
    elif kind == "world_exclusions_replace":
        for class_id in effect.class_ids:
            _active(state, ("class", class_id))
        state.put(key, _Entity(span if old is None else old.base, span, effect.revision))
    elif kind == "connection_rule_set":
        _active(state, ("class", effect.class_id))
        grant = _active(state, ("grant", effect.grant_id))
        if grant.epoch != effect.grant_epoch:
            refuse("identity")
        if old:
            prior = _load(old.base, state.budget)
            if (prior.class_id, prior.grant_id, prior.grant_epoch) != (
                effect.class_id,
                effect.grant_id,
                effect.grant_epoch,
            ) or old.status == "retired":
                refuse("identity")
        state.put(
            key,
            _Entity(
                span if old is None else old.base,
                span,
                effect.revision,
                effect.state,
                effect.grant_epoch,
            ),
        )
    elif kind == "assignment_set":
        for rule in effect.classes:
            _active(state, ("class", rule.class_id))
        digest = _ref_digest(effect.ref, state)
        ref_key = ("assigned_ref", digest)
        owner = state.entities.get(ref_key)
        if (
            owner is not None
            and _load(owner.base, state.budget).assignment_id != effect.assignment_id
        ):
            refuse("identity")
        if old:
            prior = _load(old.base, state.budget)
            if prior.ref != effect.ref or prior.origin != effect.origin:
                refuse("identity")
        auth = intent.authorization
        if auth.kind == "prospective_assignment":
            origin = effect.origin
            if (
                origin is None
                or effect.approval_id is not None
                or len(effect.classes) != 1
                or effect.ref.kind != "source"
                or effect.ref.id != auth.source_id
                or effect.ref.selector.kind != "whole"
            ):
                refuse("identity")
            if any(
                getattr(origin, k) != getattr(auth, k)
                for k in ("grant_id", "grant_epoch", "request_id", "source_id", "origin_digest")
            ):
                refuse("identity")
            rule = _load(_active(state, ("connection", auth.rule_id)).latest, state.budget)
            if (
                rule.class_id != effect.classes[0].class_id
                or rule.rule_id != effect.classes[0].rule_id
                or rule.grant_id != auth.grant_id
                or rule.grant_epoch != auth.grant_epoch
            ):
                refuse("identity")
        elif old is None and effect.origin is not None:
            # Explicit assignment cannot invent an authenticated historical origin.
            refuse("identity")
        entity = _Entity(span if old is None else old.base, span, effect.revision)
        state.put(key, entity)
        state.put(ref_key, entity)
    elif kind == "grant_revoke":
        old = _active(state, key)
        if old.epoch != effect.grant_epoch:
            refuse("identity")
        state.put(key, replace(old, latest=span, revision=effect.revision, status="revoked"))
        _revoke_grant_children(state, effect.grant_id)
    elif kind == "credential_issue":
        _issue(state, effect, intent, old, span)
    elif kind == "credential_attempt":
        old = _active(state, key)
        prior = _load(old.base, state.budget)
        if (
            prior.binding.kind != "email_code"
            or old.attempts != effect.expected_attempts
            or effect.attempts > prior.binding.attempt_limit
            or (effect.outcome == "invalid" and effect.attempts >= prior.binding.attempt_limit)
        ):
            refuse("transition")
        state.put(
            key,
            replace(
                old,
                latest=span,
                attempts=effect.attempts,
                status="active" if effect.outcome == "invalid" else "consumed",
            ),
        )
    elif kind == "credential_consume":
        if old is None:
            refuse("identity")
        prior = _load(old.base, state.budget)
        if prior.binding.kind not in ("oauth_code", "refresh_token", "email_code", "auth_state"):
            refuse("transition")
        if effect.outcome == "used":
            _active(state, key)
            state.put(key, replace(old, latest=span, status="consumed"))
        else:
            if old.status != "consumed" or prior.binding.kind != "refresh_token":
                refuse("transition")
            _revoke_family(
                state,
                prior.binding.family_id,
                prior.binding.grant.grant_id,
                prior.binding.grant.grant_epoch,
            )
    elif kind == "credential_revoke":
        old = _active(state, key)
        state.put(key, replace(old, latest=span, status="revoked"))
    elif kind == "family_revoke":
        _revoke_family(state, effect.family_id, effect.grant_id, effect.grant_epoch)
    elif kind == "run_state":
        if effect.expected_state == "absent":
            if old is not None or effect.state != "active":
                refuse("transition")
            if any(k[0] == "run" and e.status == "active" for k, e in state.entities.items()):
                refuse("transition")
            if (
                state.configuration is not None
                and state.configuration != effect.configuration_digest
            ):
                refuse("identity")
            state.configuration = effect.configuration_digest
            state.put(key, _Entity(span, span))
        else:
            old = _active(state, key)
            prior = _load(old.base, state.budget)
            if (
                effect.state == "active"
                or prior.agent != effect.agent
                or prior.configuration_digest != effect.configuration_digest
            ):
                refuse("transition")
            state.put(key, replace(old, latest=span, status=effect.state))
            _end_passes(state, run_id=effect.run_id)
    elif kind == "configuration_replace":
        if (
            state.configuration != effect.expected_configuration_digest
            or state.configuration == effect.configuration_digest
        ):
            refuse("transition")
        state.configuration = effect.configuration_digest
        _end_passes(state)
    elif kind == "epoch_invalidate":
        if effect.old_epoch != state.authority_epoch:
            refuse("identity")
        for entity_key, entity in tuple(state.entities.items()):
            if entity_key[0] in ("credential", "family"):
                state.put(entity_key, replace(entity, status="revoked"))
        state.authority_epoch = effect.new_epoch
    elif kind == "world_close":
        if effect.owner_id != state.header.owner_id:
            refuse("identity")
        state.closed = True
    elif kind == "forget_apply":
        if state.ledger != effect.before_ledger or ("ledger_row", effect.row.id) in state.entities:
            refuse("ledger_mismatch")
        state.ledger, state.erasure_revision = effect.after_ledger, effect.revision
        state.put(("ledger_row", effect.row.id), _Entity(span, span))
    elif kind == "restore_begin":
        if (
            effect.old_epoch != state.authority_epoch
            or effect.live_ledger != state.ledger
            or effect.installation_before != state.installation_epoch
        ):
            refuse("identity")
        state.installation_epoch = effect.installation_after
    elif kind in _LINEAGE:
        _lineage(state, effect, intent, old, span)
    else:
        refuse("transition")


def _revoke_grant_children(state, grant_id):
    for key, entity in tuple(state.entities.items()):
        if key[0] in ("family", "credential"):
            issue = _load(entity.base, state.budget)
            binding = issue.binding
            if hasattr(binding, "grant") and binding.grant.grant_id == grant_id:
                state.put(key, replace(entity, status="revoked"))
        elif key[0] in ("connection", "policy"):
            effect = _load(entity.latest, state.budget)
            identity = effect.grant_id if key[0] == "connection" else effect.grant.grant_id
            if identity == grant_id:
                state.put(
                    key, replace(entity, status="retired" if key[0] == "connection" else "revoked")
                )


def _end_passes(state, run_id=None):
    for key, entity in tuple(state.entities.items()):
        if key[0] == "credential":
            issue = _load(entity.base, state.budget)
            if issue.binding.kind == "runner_pass" and (
                run_id is None or issue.binding.run_id == run_id
            ):
                state.put(key, replace(entity, status="revoked"))


def _issue(state, effect, intent, old, span):
    if (
        old is not None
        or effect.installation_epoch != state.installation_epoch
        or effect.authority_epoch != state.authority_epoch
    ):
        refuse("identity")
    binding, auth = effect.binding, intent.authorization
    if hasattr(binding, "user_id") and binding.user_id not in (None, state.header.owner_id):
        refuse("identity")
    if hasattr(binding, "grant"):
        grant = _grant(state, binding.grant)
        if not set(binding.scopes) <= set(grant.scopes):
            refuse("transition")
        if auth.kind != "credential_transition":
            refuse("transition")
        source = _load(state.entities[("credential", auth.credential_id)].base, state.budget)
        if binding.kind == "oauth_code":
            if (
                source.binding.kind != "session"
                or auth.user_id != state.header.owner_id
                or source.binding.user_id != state.header.owner_id
            ):
                refuse("identity")
            policies = [
                entity
                for key, entity in state.entities.items()
                if key[0] == "policy" and entity.status == "active"
            ]
            if not any(
                _load(entity.latest, state.budget).grant == binding.grant for entity in policies
            ):
                refuse("transition")
            state.put(("credential", effect.credential_id), _Entity(span, span))
            return
        if not hasattr(source.binding, "grant") or source.binding.grant != binding.grant:
            refuse("identity")
        if binding.kind not in ("access_token", "refresh_token"):
            refuse("transition")
        if not any(
            e.kind == "credential_consume"
            and e.credential_id == auth.credential_id
            and e.outcome == "used"
            for e in intent.effects
        ):
            refuse("transition")
        if not set(binding.scopes) <= set(source.binding.scopes):
            refuse("transition")
        if source.binding.kind == "refresh_token":
            if binding.family_id != source.binding.family_id:
                refuse("identity")
            if (
                binding.kind == "refresh_token"
                and binding.parent_credential_id != auth.credential_id
            ):
                refuse("identity")
        elif source.binding.kind != "oauth_code":
            refuse("transition")
        elif binding.kind == "refresh_token" and binding.parent_credential_id is not None:
            refuse("identity")
        issues = [e for e in intent.effects if e.kind == "credential_issue"]
        if (
            len(issues) != 2
            or {e.binding.kind for e in issues} != {"access_token", "refresh_token"}
            or any(
                e.binding.family_id != binding.family_id
                or e.binding.grant != binding.grant
                or e.binding.scopes != binding.scopes
                for e in issues
            )
        ):
            refuse("transition")
        family_key = ("family", binding.family_id)
        family = state.entities.get(family_key)
        if family is None:
            if source.binding.kind != "oauth_code":
                refuse("identity")
            state.put(family_key, _Entity(span, span, epoch=binding.grant.grant_epoch))
        elif family.status != "active" or family.epoch != binding.grant.grant_epoch:
            refuse("transition")
        else:
            family_origin = _load(family.base, state.budget)
            if family_origin.binding.grant != binding.grant:
                refuse("identity")
            if source.binding.kind == "oauth_code" and family_origin.credential_id not in {
                e.credential_id for e in issues
            }:
                refuse("identity")
    elif binding.kind == "runner_pass":
        run = _load(_active(state, ("run", binding.run_id)).base, state.budget)
        if (
            auth.kind != "service_transition"
            or auth.run_id != binding.run_id
            or run.agent != binding.agent
            or auth.agent != binding.agent
            or run.configuration_digest != binding.configuration_digest
            or auth.configuration_digest != binding.configuration_digest
            or state.configuration != binding.configuration_digest
        ):
            refuse("identity")
    elif auth.kind == "credential_transition":
        if binding.kind != "session" or auth.credential_kind not in ("email_code", "auth_state"):
            refuse("transition")
        if not any(
            e.kind in ("credential_attempt", "credential_consume")
            and e.credential_id == auth.credential_id
            and getattr(e, "outcome", None) in ("accepted", "used")
            for e in intent.effects
        ):
            refuse("transition")
    elif auth.kind != "account_flow":
        refuse("transition")
    if binding.kind in ("email_code", "auth_state"):
        # Account adapter identifies actual prior challenges. It must supply exact
        # replacement effects; no email-derived identity is inferred here.
        if binding.kind == "auth_state" and binding.flow_id != auth.flow_id:
            refuse("identity")
    state.put(("credential", effect.credential_id), _Entity(span, span))


def _lineage(state, effect, intent, old, span):
    kind, auth = effect.kind, intent.authorization
    if auth.kind not in ("observed_lineage", "controller_operation"):
        refuse("transition")
    if kind == "lineage_invalidate":
        if auth.kind == "observed_lineage" and effect.run_id != auth.run_id:
            refuse("identity")
        state.put(("lineage_run", effect.run_id), _Entity(span, span, status="unknown"))
        return
    run_id = auth.run_id
    run = _load(_active(state, ("run", run_id)).base, state.budget)
    if (
        run.configuration_digest != auth.configuration_digest
        or state.configuration != auth.configuration_digest
    ):
        refuse("identity")
    if hasattr(effect, "run_id") and effect.run_id != run_id:
        refuse("identity")
    if hasattr(effect, "authority_epoch") and effect.authority_epoch != state.authority_epoch:
        refuse("identity")
    if (
        kind in ("lineage_derivation", "lineage_output")
        and effect.model_call_id != auth.model_call_id
    ):
        refuse("identity")
    unknown = state.entities.get(("lineage_run", run_id))
    if (
        unknown is not None
        and unknown.status == "unknown"
        and getattr(effect, "state", "complete") == "complete"
    ):
        refuse("transition")
    if kind == "lineage_derivation":
        if old is not None:
            refuse("identity")
        previous = state.entities.get(("lineage_head", run_id))
        if previous is None:
            if effect.ordinal != 1:
                refuse("sequence")
        else:
            prior = _load(previous.latest, state.budget)
            if (
                effect.ordinal != prior.ordinal + 1
                or effect.previous_id != prior.derivation_id
                or effect.input_count < prior.input_count
                or effect.seed_count < prior.seed_count
            ):
                refuse("sequence")
        call_key = ("model_call", effect.model_call_id)
        if call_key in state.entities:
            refuse("identity")
        entity = _Entity(span, span, status=effect.state)
        state.put(("derivation", effect.derivation_id), entity)
        state.put(("lineage_head", run_id), entity)
        state.put(call_key, entity)
        if effect.state == "unknown":
            state.put(("lineage_run", run_id), replace(entity, status="unknown"))
    elif kind == "lineage_output":
        derivation = state.entities.get(("derivation", effect.derivation_id))
        if derivation is None:
            refuse("identity")
        source = _load(derivation.base, state.budget)
        if source.run_id != run_id or source.model_call_id != effect.model_call_id:
            refuse("identity")
        if effect.state == "complete" and derivation.status != "complete":
            refuse("transition")
        if old:
            prior = _load(old.latest, state.budget)
            if old.status != "incomplete" or effect.state not in ("complete", "unknown"):
                refuse("transition")
            for field_name, _ in SCHEMAS["LineageOutput"]:
                if field_name not in ("effect_id", "state", "canonical_digest"):
                    if getattr(prior, field_name) != getattr(effect, field_name):
                        refuse("identity")
        state.put(
            ("output", effect.output_id),
            _Entity(span if old is None else old.base, span, status=effect.state),
        )
    elif kind == "lineage_dependency":
        if old is not None:
            refuse("identity")
        output_entity = state.entities.get(("output", effect.output_id))
        if output_entity is None or output_entity.status != "complete":
            refuse("transition")
        output = _load(output_entity.latest, state.budget)
        if (
            output.derivation_id != effect.derivation_id
            or output.run_id != run_id
            or output.model_call_id != auth.model_call_id
            or output.authority_epoch != state.authority_epoch
        ):
            refuse("identity")
        slot_key = ("slot", _hash_record(effect.output_slot), effect.output_version)
        if slot_key in state.entities:
            refuse("identity")
        entity = _Entity(span, span)
        state.put(("dependency", effect.dependency_id), entity)
        state.put(slot_key, entity)


def _apply(state, intent, renewed=None):
    _authorize(state, intent)
    effects = _ordered_effects(state, intent)
    # Restore's epoch invalidation must observe the old epoch, and follows the
    # matching restore_begin regardless of lexical effect IDs.
    if intent.mode == "restore":
        effects.sort(key=lambda e: 0 if e.kind == "restore_begin" else 1)
    for effect in effects:
        _apply_effect(state, effect, intent)
    state.apply_projection(intent, renewed)


def _intent_checks(state, event, previous, *, manifest):
    intent, kind = event.payload.intent, event.kind
    if (
        intent.base_sequence != event.sequence - 1
        or intent.base_head_digest != previous
        or intent.base_state_digest != state.digest()
        or intent.expected_installation_epoch != state.installation_epoch
        or intent.expected_authority_epoch != state.authority_epoch
        or intent.expected_erasure_revision != state.erasure_revision
    ):
        refuse("projection_mismatch")
    if intent.intent_id in state.intent_ids or intent.idempotency_key in state.retries:
        refuse("identity")
    if len({e.effect_id for e in intent.effects}) != len(intent.effects):
        refuse("identity")
    if kind in ("world_birth", "adopt_world"):
        payload = event.payload
        mode = "birth" if kind == "world_birth" else "adopt"
        if (
            intent.mode != "origin"
            or len(intent.effects) != 1
            or intent.effects[0].kind != "origin"
            or intent.effects[0].mode != mode
            or intent.authorization.kind != "controller_operation"
            or intent.authorization.purpose != mode
            or payload.owner_id != state.header.owner_id
            or payload.installation_id != state.header.installation_id
            or payload.activation_id != manifest.activation_id
            or payload.genesis_digest != state.header.genesis_digest
        ):
            refuse("identity")
        ledger = payload.empty_ledger if mode == "birth" else payload.ledger
        if (
            intent.effects[0].ledger != ledger
            or intent.effects[0].inventory_digest != manifest.inventory_digest
        ):
            refuse("ledger_mismatch")
        if mode == "birth" and ledger.count != 0:
            refuse("ledger_mismatch")
        if mode == "adopt" and payload.inventory_digest != manifest.inventory_digest:
            refuse("identity")
    elif kind == "restore_epoch":
        payload = event.payload
        by_kind = {e.kind: e for e in intent.effects}
        if (
            intent.mode != "restore"
            or len(intent.effects) != 2
            or set(by_kind) != {"restore_begin", "epoch_invalidate"}
            or intent.authorization.kind != "controller_operation"
            or intent.authorization.purpose != "restore"
            or intent.authorization.operation_id != payload.operation_id
            or intent.authorization.controller_receipt_id != payload.controller_receipt_id
            or payload.live_authority_head != previous
        ):
            refuse("transition")
        begin, epoch = by_kind["restore_begin"], by_kind["epoch_invalidate"]
        for key in (
            "operation_id",
            "source_kind",
            "artifact_digest",
            "inventory_digest",
            "d3_fingerprint",
            "old_epoch",
            "new_epoch",
            "scope",
            "installation_before",
            "installation_after",
        ):
            if getattr(begin, key) != getattr(payload, key):
                refuse("identity")
        if (
            begin.live_ledger != payload.ledger
            or epoch.old_epoch != begin.old_epoch
            or epoch.new_epoch != begin.new_epoch
            or payload.inventory_digest != manifest.inventory_digest
        ):
            refuse("identity")
        if (
            payload.scope == "installation"
            and payload.installation_after != manifest.installation_epoch
        ):
            refuse("identity")
    else:
        if any(e.kind in ("origin", "restore_begin", "epoch_invalidate") for e in intent.effects):
            refuse("transition")
        expected = (
            "close"
            if any(e.kind == "world_close" for e in intent.effects)
            else classify_effects(intent.effects)
        )
        if intent.mode != expected or (
            expected == "close" and classify_effects(intent.effects) != "restrictive"
        ):
            refuse("transition")


def _release_state(state, pending, sealed):
    if state.closed:
        return "closed"
    if sealed:
        return "sealed"
    if pending is not None:
        return "restoring" if pending.mode == "restore" else "pending"
    if any(key[0] == "policy" and e.status == "active" for key, e in state.entities.items()):
        return "ready"
    return "no_host_policy"


def reduce_journal(journal, *, manifest, budget):
    budget = _budget(budget)
    if type(journal) is not UntrustedJournal or journal._budget is not budget:
        refuse("input_type")
    if type(manifest) is not TYPES["Manifest"]:
        refuse("input_type")
    record_metrics(manifest, budget=budget)
    from .codec import manifest_checksum

    if manifest.checksum != manifest_checksum(manifest):
        refuse("digest")
    # A manually constructed public dataclass cannot smuggle an unvalidated tape.
    checked = scan_journal(journal._data, budget=budget)
    if (
        checked.header != journal.header
        or checked.metrics != journal.metrics
        or checked._offsets != journal._offsets
    ):
        refuse("identity")
    del checked
    header = journal.header
    if (
        header.installation_id != manifest.installation_id
        or header.genesis_digest != manifest.genesis_digest
        or header.schema_profile != manifest.schema_profile
    ):
        refuse("identity")
    heads = [head for head in manifest.worlds if head.world_id == header.world_id]
    if len(heads) != 1 or heads[0].owner_id != header.owner_id:
        refuse("identity")
    head = heads[0]
    first = journal_event(journal, 0)
    reservation = budget.reserve("reducer", 16384)
    state = _State(header, first.payload.intent.expected_installation_epoch, budget, reservation)
    origin_id, origin_digest = first.event_id, complete_event_digest(first)
    del first
    pending, pending_event, previous = None, None, None
    sealed = False
    offsets = memoryview(journal._offsets).cast("I")
    event_bytes = sum(offsets[i + 1] - offsets[i] for i in range(0, len(offsets), 4))
    event_nodes = sum(offsets[i + 2] for i in range(0, len(offsets), 4))
    prefix = JournalUsage(
        journal.metrics.bytes - event_bytes - journal.metrics.events + 1,
        0,
        journal.metrics.nodes - event_nodes,
    )
    try:
        for index in range(journal.metrics.events):
            event = journal_event(journal, index)
            after = JournalUsage(
                prefix.bytes + offsets[index * 4 + 1] - offsets[index * 4] + (1 if index else 0),
                prefix.events + 1,
                prefix.nodes + offsets[index * 4 + 2],
            )
            if state.closed:
                refuse("closed")
            if event.kind in ("world_birth", "adopt_world", "prepare", "restore_epoch"):
                if sealed:
                    refuse("sealed")
                if pending is not None:
                    refuse("pending")
                _intent_checks(state, event, previous, manifest=manifest)
                intent = event.payload.intent
                ordinary = intent.mode not in ("restrictive", "close")
                if ordinary and (saturation_reasons(prefix) or saturation_reasons(after)):
                    refuse("saturated")
                scratch = budget.reserve(
                    "reducer",
                    4096
                    + len(state.entities) * 128
                    + len(state.burned) * 64
                    + len(state.cancelled) * 16,
                )
                try:
                    candidate = state.fork()
                    candidate.reservation = scratch
                    _apply(candidate, intent)
                    if candidate.digest() != intent.result_state_digest:
                        refuse("projection_mismatch")
                    del candidate
                finally:
                    scratch.release()
                reservation.grow(768 + len(intent.effects) * 160)
                state.intent_ids.add(intent.intent_id)
                state.effect_ids.update(e.effect_id for e in intent.effects)
                state.retries[intent.idempotency_key] = _Retry(
                    _view(intent), "pending", state.digest()
                )
                pending, pending_event = _view(intent), (event.actor_id, event.approved_at)
                code = {
                    "owner_approval": "owner_approval",
                    "credential_transition": "credential_flow",
                    "prospective_assignment": "source_origin",
                    "observed_lineage": "observed_lineage",
                    "service_transition": "service_configuration",
                    "account_flow": "account_flow",
                    "controller_operation": "controller_receipt",
                }[intent.authorization.kind]
                state.requirement(code, event, intent)
                state.requirement("effect_references", event, intent)
                if event.kind in ("world_birth", "adopt_world"):
                    state.requirement("trusted_genesis", event, intent)
                if event.kind == "restore_epoch":
                    state.requirement("restore_artifact", event, intent)
            elif event.kind == "seal":
                if sealed or state.authority_epoch == 0:
                    refuse("transition")
                if (
                    pending is not None
                    and classify_effects(_load(pending, budget).effects) != "restrictive"
                ):
                    refuse("transition")
                sealed = True
            elif event.kind in ("commit", "recover", "world_closed"):
                if pending is None:
                    refuse("transition")
                intent = _load(pending, budget)
                if intent.mode not in ("restrictive", "close"):
                    if any(
                        getattr(HARD, d) - getattr(after, d) < getattr(COMPLETE_RESERVE, d)
                        for d in ("bytes", "events", "nodes")
                    ):
                        refuse("reserve_shortfall")
                if sealed and classify_effects(intent.effects) != "restrictive":
                    refuse("sealed")
                completion = event.payload.completion
                if (
                    completion.intent_id != intent.intent_id
                    or completion.intent_digest != intent.intent_digest
                ):
                    refuse("identity")
                is_close = intent.mode == "close"
                if is_close != (event.kind == "world_closed"):
                    refuse("transition")
                if is_close:
                    close_effect = next(e for e in intent.effects if e.kind == "world_close")
                    if event.payload.closure_id != close_effect.closure_id:
                        refuse("identity")
                renewed, cancel = None, False
                actor, approved_at = pending_event
                if event.kind == "recover":
                    renewed, cancel = _recovery(state, event, intent)
                    if renewed is not None:
                        actor, approved_at = renewed.user_id, renewed.approved_at
                if (event.actor_id, event.approved_at) != (actor, approved_at):
                    refuse("identity")
                if approved_at is not None and at_microseconds(
                    completion.completed_at
                ) < at_microseconds(approved_at):
                    refuse("transition")
                # Pending IDs were reserved at prepare; the same exact effects are
                # now applied once. They are not a second new-intent collision.
                state.effect_ids.difference_update(e.effect_id for e in intent.effects)
                if cancel:
                    state.burn(_approval_id(intent))
                    state.cancelled.append(
                        TYPES["CancelledIntent"](
                            intent_id=intent.intent_id,
                            intent_digest=intent.intent_digest,
                            approval_id=_approval_id(intent),
                        )
                    )
                else:
                    _apply(state, intent, None if renewed is None else renewed.approval_id)
                state.effect_ids.update(e.effect_id for e in intent.effects)
                expected = state.digest()
                if (
                    completion.applied_state_digest != expected
                    or completion.ledger != state.ledger
                    or completion.authority_epoch != state.authority_epoch
                    or completion.erasure_revision != state.erasure_revision
                ):
                    refuse("projection_mismatch")
                if not cancel and renewed is None and expected != intent.result_state_digest:
                    refuse("projection_mismatch")
                state.retries[intent.idempotency_key] = _Retry(
                    pending, "cancelled" if cancel else "committed", expected
                )
                state.requirement("database_outcome", event, intent, expected)
                state.requirement("ledger_content", event, intent, state.ledger.file_digest)
                pending, pending_event = None, None
            else:
                refuse("transition")
            previous = complete_event_digest(event)
            prefix = after
            del event
        pending_record = None if pending is None else _load(pending, budget)
        release_state = _release_state(state, pending_record, sealed)
        ledger = state.ledger
        if (
            state.authority_epoch == 0
            and pending_record is not None
            and pending_record.mode == "origin"
        ):
            ledger = pending_record.effects[0].ledger
        if (
            head.sequence != journal.metrics.events
            or head.head_digest != previous
            or head.origin_event_id != origin_id
            or head.origin_digest != origin_digest
            or head.authority_epoch != state.authority_epoch
            or head.erasure_revision != state.erasure_revision
            or head.ledger != ledger
            or head.release_state != release_state
            or head.pending_id != (None if pending_record is None else pending_record.intent_id)
        ):
            refuse("projection_mismatch")
        expected_installation = state.installation_epoch
        if pending_record is not None and pending_record.mode == "restore":
            begin = next(e for e in pending_record.effects if e.kind == "restore_begin")
            if begin.scope == "installation":
                expected_installation = begin.installation_after
                if manifest.state != "restoring":
                    refuse("identity")
        if expected_installation != manifest.installation_epoch:
            refuse("identity")
        final = journal_event(journal, journal.metrics.events - 1)
        state.requirement("current_principal", final)
        state.requirement("current_projection", final, digest=state.digest())
        usage = JournalUsage(journal.metrics.bytes, journal.metrics.events, journal.metrics.nodes)
        if saturation_reasons(usage) and release_state not in ("closed", "sealed"):
            release_state = "saturated"
        if manifest.state != "ready" and release_state in ("ready", "no_host_policy"):
            release_state = "restoring" if manifest.state == "restoring" else "pending"
        reservation.grow(len(state.entities) * 64 + len(state.burned) * 64)
        frozen = MappingProxyType(
            {
                "entities": MappingProxyType(state.entities),
                "retries": MappingProxyType(state.retries),
                "burned": frozenset(state.burned),
                "head_digest": previous,
            }
        )
        return UntrustedReduction(
            journal,
            state.digest(),
            state.installation_epoch,
            state.authority_epoch,
            state.erasure_revision,
            ledger,
            release_state,
            None if pending_record is None else pending_record.intent_id,
            sealed,
            state.closed,
            tuple(state.requirements),
            frozen,
            reservation,
        )
    except BaseException:
        reservation.release()
        raise


def _recovery(state, event, intent):
    payload, approval = event.payload, intent.authorization
    resolution = payload.resolution
    original = approval.payload_digest if approval.kind == "owner_approval" else None
    if payload.original_approval_digest != original:
        refuse("identity")
    restrictive = classify_effects(intent.effects) == "restrictive"
    if resolution == "complete_restriction":
        if not restrictive or payload.observed_db_digest != intent.result_state_digest:
            refuse("transition")
    elif resolution == "confirm_committed":
        if payload.observed_db_digest != intent.result_state_digest:
            refuse("projection_mismatch")
        if intent.mode == "ordinary" and approval.kind != "owner_approval":
            # Non-owner operational flows still require their exact source flow.
            state.requirement("current_principal", event, intent)
    elif resolution == "cancel_unapplied_expansion":
        if (
            approval.kind != "owner_approval"
            or not _pure_expansion(intent.effects)
            or payload.observed_db_digest != intent.base_state_digest
        ):
            refuse("transition")
        return None, True
    elif resolution == "complete_reapproved":
        new = payload.renewed_approval
        if (
            approval.kind != "owner_approval"
            or restrictive
            or new is None
            or new.user_id != state.header.owner_id
            or new.approval_id == approval.approval_id
            or new.approval_id in state.burned
            or new.payload_digest != choice_digest(state.header.world_id, intent)
            or new.expected_revision != approval.expected_revision
            or at_microseconds(new.approved_at) <= at_microseconds(approval.approved_at)
        ):
            refuse("identity")
        # Compare against the exact new two-approval result, never an arbitrary
        # observed digest that happens to agree with a caller-supplied completion.
        scratch = state.budget.reserve("reducer", 4096 + len(state.entities) * 128)
        try:
            candidate = state.fork()
            candidate.reservation = scratch
            candidate.effect_ids = set(state.effect_ids)
            candidate.effect_ids.difference_update(e.effect_id for e in intent.effects)
            _apply(candidate, intent, new.approval_id)
            if payload.observed_db_digest != candidate.digest():
                refuse("projection_mismatch")
            del candidate
        finally:
            scratch.release()
        state.requirement("renewed_approval", event, intent, new.payload_digest)
        return new, False
    else:
        refuse("transition")
    return None, False


@dataclass(frozen=True, slots=True)
class ProjectionAgreement:
    state_digest: str
    requirements: tuple


def compare_projection(reduction, projection):
    if (
        type(reduction) is not UntrustedReduction
        or type(projection) is not TYPES["ProjectionBinding"]
    ):
        refuse("input_type")
    budget = _budget(reduction.journal._budget)
    record_metrics(projection, budget=budget)
    if reduction.release_state not in ("ready", "no_host_policy"):
        code = reduction.release_state
        refuse(code if code in ("pending", "sealed", "closed", "saturated") else "pending")
    expected = TYPES["ProjectionBinding"](
        installation_id=reduction.journal.header.installation_id,
        installation_epoch=reduction.installation_epoch,
        authority_epoch=reduction.authority_epoch,
        sequence=reduction.journal.metrics.events,
        head_digest=reduction._state["head_digest"],
        schema_profile=PROFILE,
        state_digest=reduction.state_digest,
        ledger=reduction.ledger,
        ready=True,
    )
    if projection != expected:
        refuse("projection_mismatch")
    return ProjectionAgreement(reduction.state_digest, reduction.requirements)


@dataclass(frozen=True, slots=True)
class RetryOutcome:
    outcome: str
    state_digest: str
    requirements: tuple


def lookup_retry(reduction, intent):
    if type(reduction) is not UntrustedReduction or type(intent) is not TYPES["Intent"]:
        refuse("input_type")
    record_metrics(intent, budget=_budget(reduction.journal._budget))
    prior = reduction._state["retries"].get(intent.idempotency_key)
    if prior is None:
        return None
    if _load(prior.intent, reduction.journal._budget) != intent:
        refuse("identity")
    return RetryOutcome(prior.outcome, prior.state_digest, reduction.requirements)


@dataclass(frozen=True, slots=True)
class AppendAssessment:
    decision: str
    current: JournalUsage
    projected: JournalUsage
    requirements: tuple


def assess_append(reduction, events, *, budget):
    budget = _budget(budget)
    if type(reduction) is not UntrustedReduction or reduction.journal._budget is not budget:
        refuse("input_type")
    if type(events) is not tuple or not 1 <= len(events) <= 3:
        refuse("input_type")
    current = JournalUsage(
        reduction.journal.metrics.bytes,
        reduction.journal.metrics.events,
        reduction.journal.metrics.nodes,
    )
    projected, measures = current, []
    previous = reduction._state["head_digest"]
    for index, event in enumerate(events):
        measured = encode_event(event, budget=budget).metrics
        if (
            event.sequence != current.events + index + 1
            or event.previous_digest != previous
            or event.world_id != reduction.journal.header.world_id
        ):
            refuse("sequence")
        previous = complete_event_digest(event)
        projected = JournalUsage(
            projected.bytes + measured.bytes + 1,
            projected.events + 1,
            projected.nodes + measured.nodes,
        )
        measures.append(projected)
    if reduction.closed:
        refuse("closed")
    if reduction.sealed and any(
        e.kind not in ("commit", "recover", "world_closed") for e in events
    ):
        refuse("sealed")
    if any(getattr(projected, d) > getattr(HARD, d) for d in ("bytes", "events", "nodes")):
        refuse("journal_limit")
    first = events[0]
    if first.kind in ("prepare", "restore_epoch"):
        if reduction.pending_id is not None:
            refuse("pending")
        intent = first.payload.intent
        if (
            intent.base_sequence != current.events
            or intent.base_head_digest != reduction._state["head_digest"]
            or intent.base_state_digest != reduction.state_digest
            or intent.expected_installation_epoch != reduction.installation_epoch
            or intent.expected_authority_epoch != reduction.authority_epoch
            or intent.expected_erasure_revision != reduction.erasure_revision
        ):
            refuse("projection_mismatch")
        completions = [e for e in events[1:] if e.kind in ("commit", "recover", "world_closed")]
        if len(completions) != 1:
            refuse("transition")
        completion = completions[0].payload.completion
        if (
            completion.intent_id != intent.intent_id
            or completion.intent_digest != intent.intent_digest
        ):
            refuse("identity")
        if (
            any(e.kind == "seal" for e in events)
            and classify_effects(intent.effects) != "restrictive"
        ):
            refuse("transition")
    ordinary = first.kind in ("prepare", "restore_epoch") and (
        first.kind == "restore_epoch"
        or classify_effects(first.payload.intent.effects) == "ordinary"
    )
    if ordinary:
        if len(events) != 2 or events[1].kind not in ("commit", "recover"):
            refuse("transition")
        decision = assess_ordinary_append(current, projected)
        if decision != "arithmetic_fits":
            refuse("saturated" if decision == "current_saturated" else "reserve_shortfall")
        if saturation_reasons(measures[0]):
            refuse("saturated")
        # Complete reserve remains after the exact pending completion, in every dimension.
        if any(
            getattr(HARD, d) - getattr(projected, d) < getattr(COMPLETE_RESERVE, d)
            for d in ("bytes", "events", "nodes")
        ):
            refuse("reserve_shortfall")
    elif first.kind == "prepare":
        if len(events) < 2 or not any(
            e.kind in ("commit", "world_closed", "recover") for e in events[1:]
        ):
            refuse("transition")
    elif first.kind == "seal":
        if len(events) != 1:
            refuse("transition")
        if reduction.pending_id is not None:
            pending = next(
                r for r in reduction._state["retries"].values() if r.outcome == "pending"
            )
            if classify_effects(_load(pending.intent, budget).effects) != "restrictive":
                refuse("transition")
    elif first.kind in ("commit", "recover", "world_closed"):
        if reduction.pending_id is None or len(events) != 1:
            refuse("transition")
    else:
        refuse("transition")
    return AppendAssessment("arithmetic_fits_only", current, projected, reduction.requirements)
