"""The material parser refuses data loss and preserves unknown provenance."""

import pytest

from tests.views import test_materials_api

material_world = test_materials_api.material_world
material_client = test_materials_api.material_client


def test_text_material_retains_exact_unknown_provenance():
    from grove.ingest.material_files import parse_file

    parsed = parse_file(b"A cedar plan.\n\nA second paragraph.", "text/plain", "plan.txt")
    assert parsed.format == "text"
    assert [part.text for part in parsed.parts] == ["A cedar plan.", "A second paragraph."]
    assert all(part.source_facts.time.status == "absent" for part in parsed.parts)


def test_material_parser_rejects_unsupported_file():
    from grove.ingest.material_files import MaterialInvalid, parse_file

    with pytest.raises(MaterialInvalid):
        parse_file(b"not an image", "image/png", "plan.png")


def test_oversize_block_refuses_instead_of_truncating():
    from grove.ingest.material_files import MaterialInvalid, parse_file

    with pytest.raises(MaterialInvalid):
        parse_file(b"x" * 100001, "text/plain")


def office(entries):
    import io
    import zipfile

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return out.getvalue()


def test_docx_preserves_ordered_paragraph_and_table_text():
    from grove.ingest.material_files import parse_file

    data = office(
        [
            (
                "word/document.xml",
                '<w:document xmlns:w="urn:word"><w:body><w:p><w:r><w:t>First</w:t></w:r'
                "></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Table</w:t></w:r></w:p></w:tc"
                "></w:tr></w:tbl></w:body></w:document>",
            )
        ]
    )
    result = parse_file(
        data, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert [p.text for p in result.parts] == ["First", "Table"]
    assert [p.source_facts.locator.block for p in result.parts] == [1, 2]
    assert all(p.source_facts.time.status == "absent" for p in result.parts)


@pytest.mark.parametrize(
    "name,content",
    [
        ("../evil", "text"),
        ("word/media/a.png", "data"),
        ("word/document.xml", '<!DOCTYPE x [<!ENTITY x "boom">]><x>&x;</x>'),
    ],
)
def test_unsafe_office_refuses(name, content):
    from grove.ingest.material_files import MaterialInvalid, parse_file

    with pytest.raises(MaterialInvalid):
        parse_file(
            office([(name, content)]),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )


def test_pptx_uses_presentation_order():
    from grove.ingest.material_files import parse_file

    data = office(
        [
            (
                "ppt/presentation.xml",
                '<p:presentation xmlns:p="urn:p" xmlns:r="http://schemas.openxmlformats'
                '.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId r:id="b"/'
                '><p:sldId r:id="a"/></p:sldIdLst></p:presentation>',
            ),
            (
                "ppt/_rels/presentation.xml.rels",
                '<Relationships><Relationship Id="a" Target="slides/slide1.xml"/><Relat'
                'ionship Id="b" Target="slides/slide2.xml"/></Relationships>',
            ),
            ("ppt/slides/slide1.xml", "<slide><p><t>Second</t></p></slide>"),
            ("ppt/slides/slide2.xml", "<slide><p><t>First</t></p></slide>"),
        ]
    )
    result = parse_file(
        data, "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )
    assert [p.text for p in result.parts] == ["First", "Second"]
    assert [p.source_facts.locator.slide for p in result.parts] == [1, 2]


def test_pdf_uses_real_text_and_ignores_document_creation_time():
    from grove.ingest.material_files import parse_file
    from tests.ingest.test_documents import pdf_bytes

    parsed = parse_file(pdf_bytes("Cedar plan", created="D:20200101120000Z"), "application/pdf")
    assert parsed.parts[0].text == "Cedar plan"
    assert parsed.parts[0].source_facts.locator.page == 1
    assert parsed.parts[0].source_facts.time.status == "absent"


def test_pdf_with_unresolved_image_page_refuses_complete_import():
    from grove.ingest.material_files import MaterialInvalid, parse_file
    from tests.ingest.test_documents import pdf_bytes

    with pytest.raises(MaterialInvalid):
        parse_file(pdf_bytes("Good page", None), "application/pdf")


def test_cancelled_parse_has_no_partial_result():
    import threading

    from grove.ingest.material_files import MaterialInvalid, parse_file

    cancel = threading.Event()
    cancel.set()
    with pytest.raises(MaterialInvalid):
        parse_file(b"A complete note.", "text/plain", cancel=cancel)


def test_office_external_relationship_refuses_without_network():
    from grove.ingest.material_files import MaterialInvalid, parse_file

    data = office(
        [
            ("word/document.xml", "<document><p><t>Safe text</t></p></document>"),
            (
                "word/_rels/document.xml.rels",
                '<Relationships><Relationship Id="a" TargetMode="External" Target="https://example.com"/></Relationships>',
            ),
        ]
    )
    with pytest.raises(MaterialInvalid):
        parse_file(data, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")


def test_office_xml_depth_is_bounded():
    from grove.ingest.material_files import MaterialInvalid, parse_file

    data = office([("word/document.xml", "<x>" * 65 + "<p><t>Text</t></p>" + "</x>" * 65)])
    with pytest.raises(MaterialInvalid):
        parse_file(data, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")


def template_presentation(layout="", master="", *, placeholder=False):
    """A slide linked to a layout and master, with distinct visible text."""
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

    def shape(text):
        marker = "<p:ph type='title'/>" if placeholder else ""
        return (
            "<p:sp><p:nvSpPr><p:cNvPr id='2' name='Text'/><p:cNvSpPr/>"
            f"<p:nvPr>{marker}</p:nvPr></p:nvSpPr><p:spPr/>"
            f"<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>{text}</a:t>"
            "</a:r></a:p></p:txBody></p:sp>"
        )

    def part(tag, text):
        return (
            f"<p:{tag} xmlns:p='http://schemas.openxmlformats.org/presentationml/2006/main' "
            "xmlns:a='http://schemas.openxmlformats.org/drawingml/2006/main'>"
            f"<p:cSld><p:spTree>{shape(text)}</p:spTree></p:cSld></p:{tag}>"
        )

    return office(
        [
            (
                "ppt/presentation.xml",
                f"<p:presentation xmlns:p='urn:p' xmlns:r='{rel}'>"
                "<p:sldIdLst><p:sldId r:id='slide'/></p:sldIdLst></p:presentation>",
            ),
            (
                "ppt/_rels/presentation.xml.rels",
                "<Relationships><Relationship Id='slide' "
                "Target='slides/slide1.xml'/></Relationships>",
            ),
            ("ppt/slides/slide1.xml", part("sld", "Quarterly plan")),
            (
                "ppt/slides/_rels/slide1.xml.rels",
                "<Relationships><Relationship Id='layout' "
                "Target='../slideLayouts/slideLayout1.xml'/></Relationships>",
            ),
            ("ppt/slideLayouts/slideLayout1.xml", part("sldLayout", layout)),
            (
                "ppt/slideLayouts/_rels/slideLayout1.xml.rels",
                "<Relationships><Relationship "
                "Id='master' Target='../slideMasters/slideMaster1.xml'/></Relationships>",
            ),
            ("ppt/slideMasters/slideMaster1.xml", part("sldMaster", master)),
        ]
    )


@pytest.mark.parametrize(
    "layout,master",
    [
        ("Gate opens at 09:00.", ""),
        ("", "Meeting ends at 17:00."),
        ("Gate opens at 09:00.", "Meeting ends at 17:00."),
    ],
)
def test_pptx_static_template_text_refuses(layout, master):
    from grove.ingest.material_files import MaterialInvalid, parse_file

    with pytest.raises(MaterialInvalid, match="unsupported or unreadable"):
        parse_file(
            template_presentation(layout, master),
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        )


@pytest.mark.parametrize("placeholder", [False, True])
def test_pptx_empty_templates_and_placeholder_defaults_preserve_slide(placeholder):
    from grove.ingest.material_files import parse_file

    default = "Click to edit title" if placeholder else ""
    parsed = parse_file(
        template_presentation(default, default, placeholder=placeholder),
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )
    assert [part.text for part in parsed.parts] == ["Quarterly plan"]


def test_pptx_static_template_http_refuses_before_retention(
    driver, material_world, material_client
):
    from grove.store.query import WorldQuery

    response = material_client.post(
        "/v1/materials/files",
        content=template_presentation("Gate opens at 09:00.", "Meeting ends at 17:00."),
        headers={
            "Content-Type": (
                "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            ),
            "X-Grove-Filename": "plan.pptx",
        },
    )
    assert response.status_code == 422
    for label in ("MaterialRevision", "Source", "Request", "Episode"):
        assert (
            WorldQuery(driver, material_world[0].world_id).read(
                f"MATCH (n:{label}) RETURN count(n) AS n"
            )[0]["n"]
            == 0
        )
