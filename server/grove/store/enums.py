"""The closed enumerations, as data.

Code fixes these lists (spec lines 189, 1557). No ontology operation extends them. Each comment
gives the spec line of the list. "No list" marks a list this build fixes because the spec names the
column and gives no values.
"""

ENUMS: dict[str, tuple[str, ...]] = {
    # Line 639.
    "lifecycle_class": ("lasting_thing", "work_with_state", "record", "bounded_period"),
    # Lines 322, 1237. done, cancelled and ended are terminal; reported_done is not.
    "status_role": ("open", "active", "blocked", "reported_done", "done", "cancelled", "ended"),
    # Line 341.
    "end_class": ("ended_in_the_world", "refuted", "superseded", "retired", "spent"),
    # Lines 1056 to 1059.
    "source_type": ("planned", "said", "reported_done", "confirmed"),
    # Lines 1129, 2060, 2061. none is the wire value that line 1129 leaves out.
    "statement_class": (
        "intention",
        "assignment_or_request",
        "decision",
        "promise",
        "preference_or_opinion",
        "report_happened",
        "report_state",
        "none",
        # S51: the evidence is itself the record of an act (a receipt, a ticket, a booking
        # confirmation, a boarding pass, a signed document), so code may type it confirmed for
        # what it shows (spec lines 1060, 1086, 1170).
        "artifact",
    ),
    # Line 1124.
    "derivation": ("observed", "inferred", "rolled_up"),
    "speaker_kind": ("person", "ai_host", "automated", "forwarded", "third_party"),
    "modality": ("text", "voice", "image"),
    # Line 3105.
    "polarity": ("supports", "contradicts"),
    # Line 942 for the verify judge. The other codes serve the other roles of line 986: lines 1594
    # and 1667 (same or different, field fit), line 1841 (contradicts or complements), line 1613
    # (a split's per-link classification, with no list, so the two sides are named here).
    # S33: "not supported" (lines 996, 2157, 2627) is a verdict on the claim, not on a row. A row
    # that speaks against the claim is contradicts, a row that does not speak to it is
    # not_about_claim, and the claim is not supported when no row supports it.
    "verdict": (
        "supports",
        "contradicts",
        "not_about_claim",
        "complements",
        "same",
        "different",
        "fits",
        "does_not_fit",
        "first_word",
        "second_word",
    ),
    # Line 945.
    "disposition": ("confirmed", "moved", "retired", "deferred"),
    # S52, line 957: the bands of the agreement score, which line 957 states as conditions only.
    "verification_band": ("confirm", "contradiction", "against", "zero_signal", "stale_support"),
    # S53, B.3: what a relayed request carries from the AI host or the person's views.
    "relay": ("remember", "answer", "correction"),
    # Line 1575.
    "temporal_class": ("state", "permanent", "event"),
    # No list: S3 fixes this one. Line 1140 shows only before.
    "precision": (
        "instant",
        "minute",
        "hour",
        "day",
        "week",
        "month",
        "quarter",
        "year",
        "before",
        "after",
        "unknown",
    ),
    # Line 1127.
    "capability": ("confirms", "shows_said", "shows_planned", "none"),
    "capability_origin": ("default", "minted_with_a_word", "the_person"),
    # Line 650.
    "cardinality": ("many", "one_per_source", "one_per_target"),
    # Line 1235.
    "downstream_end": ("from", "to"),
    # O13, line 3241: every meaning is directed from its first endpoint to its second; a reversed
    # phrasing is a reversed alias, never a second direction.
    "direction": ("from_to",),
    # O13: no list in the spec for property_names.value_type (line 3091).
    "value_type": ("text", "number", "boolean", "date", "instant"),
    # Lines 1572, 1573.
    "registry_status": ("provisional", "canonical", "split", "retired"),
    "registry_origin": ("starter", "minted", "merged_from", "split_from"),
    # Line 3092.
    "alias_target_type": ("kind", "meaning", "item", "property_name"),
    # S25, lines 832, 2067.
    "scheduled_role": ("start", "due", "end"),
    # Line 3181.
    "ontology_operation": (
        "resolve",
        "propose",
        "promote",
        "merge",
        "split",
        "rename",
        "retire",
        "undo",
    ),
    # No list for area status (line 3095). The merged into column needs merged; D8 retires words.
    "area_status": ("active", "merged", "retired"),
    # Lines 3048 to 3059, 3101. scripted runs only in CI and the evaluation world (A.6).
    "connector_type": (
        "calendar",
        "whatsapp",
        "past_ai_conversations",
        "voice_notes",
        "notes_files_releases",
        "screenshots",
        "git",
        "web_pages",
        "ai_host",
        "person_in_views",
        "scripted",
        # S51: the D5 table's reminders and to-dos (line 1083) and identity sources (a contact
        # card, a profile, a sign-in, line 1099), which A.4 does not list.
        "reminders",
        "identity",
    ),
    # Lines 2391, 3101. took_in is a read that found items. denied and failed are the two cases of
    # "could not read" (line 2391), so the row keeps the reason as its code.
    "connector_outcome": ("took_in", "nothing_new", "denied", "failed", "kept_out"),
    # No list for forget state (line 3103). Line 2525 deletes a span or a whole source.
    "forget_state": ("kept", "span_forgotten", "forgotten"),
    # Line 3105.
    "evidence_target_type": ("item", "link", "belief"),
    # S15, line 3166.
    "evidence_end_reason": ("correction", "forget", "merge"),
    # Line 2104.
    "hold_state": ("open", "promoted", "expired"),
    # Line 2105.
    "mention_state": ("open", "resolved", "question", "expired"),
    # S27: B.1 also names runner; decision 6 leaves server as the one reachable value.
    "screened_where": ("server",),
    # D14 lines 2505 to 2520 and slice row 4: the classes a screening receipt counts.
    "screening_class": (
        "credential",
        "payment_card",
        "one_time_code",
        "reset_link",
        "contact_email",
        "contact_phone",
        "contact_handle",
        "pay_amount",
        "denylisted",
        "renamed",
    ),
    # Line 3114.
    "entity_type": ("item", "link", "evidence", "registry_row", "belief"),
    # Lines 750, 3116.
    "request_status": ("queued", "applying", "saved", "declined", "failed"),
    # No list for job state (line 3118). A claim moves a job to claimed, and it checks the state
    # only after it holds the node's write lock (S1).
    "job_state": ("queued", "claimed", "done", "failed"),
    # Line 3118. B.1 is the reference over the two lanes of lines 2029 and 2327.
    "job_lane": ("interactive", "maintenance", "backfill"),
    # Line 368.
    "effort": ("minimal", "low", "medium", "high", "max"),
    # Lines 986, 3121.
    "judgment_role": (
        "first_judge",
        "second_judge",
        "contradicts_or_complements",
        "same_belief",
        "same_word",
        "field_fit",
        "split_classification",
    ),
    # No list for budget state (line 3122). Line 2659 says a budget is reached.
    "budget_state": ("open", "reached"),
    # Line 1242.
    "cascade_rule": ("R1", "R2", "R3", "R4", "R5"),
    # Line 1252.
    "stakes": ("low", "high"),
    # Line 1270.
    "impact_state": (
        "open",
        "applied",
        "proposed",
        "born_resolved",
        "resolved_still_holding",
        "resolved_revised",
        "dismissed",
        "undone",
    ),
    # S25, line 1250.
    "impact_flag": ("date_conflict",),
    # Line 1272.
    "proposal_state": ("open", "accepted", "rejected", "stale"),
    # S25, lines 1252, 1264, 1265.
    "proposal_type": ("cascade", "cascade_failed", "wide_impact", "reopen"),
    # Line 1747.
    "belief_retest_outcome": (
        "supports",
        "contradicts",
        "complements",
        "not_about_claim",
        "disagreement",
    ),
    "belief_resolution": ("standing_holds", "fresh_wins", "complement", "to_question"),
    "support_disposition": ("still_holding", "revised", "dismissed"),
    "belief_state": ("tentative", "established", "weakening", "superseded"),
    # Line 1748. Line 1811's origin "the person" for a restatement is persons_correction.
    "belief_origin": ("earned_from_episodes", "persons_generalisation", "persons_correction"),
    # S25, line 1752.
    "belief_end_class": ("world_changed", "wrong", "replaced", "person_retired"),
    # Line 1787.
    "contradiction_status": ("open", "resolved", "with_the_person"),
    # Line 2172.
    "question_class": ("decision", "fact_only_the_person_holds", "go_on_outward_action"),
    "question_state": ("open", "answered", "withdrawn"),
    # Line 2198.
    "growth_test": ("coverage", "fidelity", "parsimony"),
    # Line 2424. The gap of short_of_target is checks.state_gap.
    "check_state": ("on_target", "short_of_target", "broken", "not_measured"),
    # Line 2425. An invalid run is audit_runs.invalid (S25).
    "audit_run_status": ("completed", "failed", "partial"),
    # Lines 2367, 2372, 2379. Line 2426 gives no list of its own.
    "audit_grade": (
        "supported",
        "supported_but_stale",
        "contradicted",
        "unverifiable",
        "covered",
        "absent",
        "out_of_scope",
        "useful",
        "not_useful",
    ),
    # Line 2427.
    "finding_status": ("open", "fixed", "closed_by_recheck", "declined"),
    # Line 2518.
    "forget_target_type": ("item", "field_value", "source_span", "named_value"),
    # Line 3148.
    "registration_type": ("dynamic", "metadata_document"),
}
