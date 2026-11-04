from datetime import UTC, datetime

from grove.ids import new_id
from grove.ingest.material_files import parse_file
from grove.ingest.materials import prepare


def test_title_and_body_use_real_contact_screening():
    world, material = new_id(), new_id()
    value = prepare(
        world,
        material,
        1,
        "Contact mira@cedar.example",
        "Email mira@cedar.example for details.",
        datetime(2026, 11, 4, tzinfo=UTC),
    )
    assert "mira@cedar.example" not in value.title.content()
    assert all("mira@cedar.example" not in s.content() for s in value.bodies)


def test_file_facts_survive_screening_without_invented_time():
    value = prepare(
        new_id(),
        new_id(),
        1,
        "Notes",
        parse_file(b"A future plan.", "text/plain"),
        datetime(2026, 11, 4, tzinfo=UTC),
    )
    message = value.bodies[0].messages[0]
    assert message.at is None and message.speaker_kind is None
    assert message.source_facts.time.status == "absent"


def test_title_and_body_use_real_credential_screening():
    planted = "sk-" + "Ab3dE5gH7jK9mN2pQ4sT6vW8yZ1aC3eF5hJ7kL9m"
    value = prepare(
        new_id(),
        new_id(),
        1,
        "Token " + planted,
        "Use token " + planted,
        datetime(2026, 11, 4, tzinfo=UTC),
    )
    assert planted not in value.title.content()
    assert all(planted not in s.content() for s in value.bodies)
    assert value.title.counts.get("credential", 0) > 0
    assert any(s.counts.get("credential", 0) > 0 for s in value.bodies)
