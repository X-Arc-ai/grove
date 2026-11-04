"""The material parser refuses data loss and preserves unknown provenance."""

import pytest


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
