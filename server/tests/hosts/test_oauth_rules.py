"""The rules of the authorization server that need no store: scopes, PKCE, redirect URIs, the
registration metadata, and the consent page's fragment and csrf value."""

from pathlib import Path

import pytest

from grove.oauth.consent import (
    CONSENT_MARK,
    TITLE_MARK,
    ConsentView,
    TemplateBroken,
    consent_fragment,
    csrf_token,
    error_fragment,
    render_page,
    same_csrf,
)
from grove.oauth.pkce import s256, valid_pkce_value, verifier_matches
from grove.oauth.redirects import (
    redirect_matches,
    redirect_uri_problem,
    return_place,
    with_params,
)
from grove.oauth.registration import (
    INVALID_CLIENT_METADATA,
    INVALID_REDIRECT_URI,
    InvalidMetadata,
    clean_client_name,
    parse_registration,
)
from grove.oauth.scopes import DEFAULT_SCOPES, granted_scopes, known_scopes
from tests.hosts.conftest import LOOPBACK_REDIRECT

REPO = Path(__file__).resolve().parents[3]

# Scopes ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [None, "", "   ", "openid profile email offline_access", "grove.admin", "GROVE.READ", 7],
)
def test_no_known_scope_gives_read_and_write(raw):
    assert granted_scopes(raw) == ("grove.read", "grove.write") == DEFAULT_SCOPES


def test_foreign_scopes_drop_and_known_ones_keep_their_order():
    assert granted_scopes("openid grove.write offline_access grove.read") == (
        "grove.read",
        "grove.write",
    )
    assert granted_scopes("grove.read") == ("grove.read",)


def test_sensitive_write_comes_only_when_named():
    assert "grove.sensitive_write" not in granted_scopes(None)
    assert granted_scopes("grove.sensitive_write grove.read") == (
        "grove.read",
        "grove.sensitive_write",
    )
    assert known_scopes("profile") == ()


# PKCE ------------------------------------------------------------------------------------------


def test_the_s256_of_the_rfc_7636_example():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert s256(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    assert verifier_matches(verifier, "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")
    assert not verifier_matches(verifier + "x", "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("a" * 43, True),
        ("A-._~" * 25 + "abc", True),
        ("a" * 42, False),
        ("a" * 129, False),
        ("a" * 42 + "+", False),
        ("a" * 42 + "=", False),
        ("a" * 43 + "\n", False),
        (None, False),
    ],
)
def test_a_pkce_value_is_43_to_128_unreserved_characters(value, valid):
    assert valid_pkce_value(value) is valid


def test_a_verifier_that_is_not_well_formed_never_matches():
    challenge = s256("b" * 43)
    assert verifier_matches("b" * 43, challenge)
    assert not verifier_matches("b" * 42, s256("b" * 42))


# Redirect URIs ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "uri",
    [
        "https://app.example/callback",
        "https://app.example:8443/cb?tab=1",
        "http://127.0.0.1:43117/callback",
        "http://[::1]:5000/cb",
        "http://localhost/cb",
        "com.lumendesk.app:/oauth/callback",
        "lumen-desk://callback",
    ],
)
def test_these_redirect_uris_may_be_registered(uri):
    assert redirect_uri_problem(uri) is None


@pytest.mark.parametrize(
    "uri",
    [
        "",
        None,
        "http://app.example/callback",
        "http://127.0.0.1.app.example/cb",
        "http://localhost@app.example/cb",
        "https://user:pw@app.example/cb",
        "https:///nohost",
        "https://app.example/cb#frag",
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "data:text/html,hi",
        "file:///etc/passwd",
        "vbscript:msgbox",
        "about:blank",
        "blob:https://app.example/1",
        "https://app.example/a b",
        'https://app.example/"cb"',
        "https://app.example/<cb>",
        "https://app.example/c\\b",
        "https://app.example/{cb}",
        "https://app.example/c|b",
        "https://app.example/c^b",
        "https://app.example/c`b",
        "https://app.example/é",
        "https://app.example/\tcb",
        "/relative/path",
        "https://app.example:99999/cb",
        "https://app.example/" + "x" * 2000,
    ],
)
def test_these_redirect_uris_are_refused(uri):
    problem = redirect_uri_problem(uri)
    assert problem is not None
    if isinstance(uri, str) and len(uri) > 4:
        assert uri[4:] not in problem


def test_a_redirect_must_match_exactly_outside_loopback():
    registered = ["https://app.example/cb"]
    assert redirect_matches("https://app.example/cb", registered)
    for other in (
        "https://app.example/cb/",
        "https://app.example:8443/cb",
        "https://APP.example/cb",
        "https://app.example/cb?x=1",
        "https://app.example/c",
        "http://app.example/cb",
    ):
        assert not redirect_matches(other, registered), other


def test_a_loopback_redirect_may_differ_in_port_only():
    registered = [LOOPBACK_REDIRECT]
    assert redirect_matches("http://127.0.0.1:50001/callback", registered)
    assert redirect_matches("http://127.0.0.1/callback", registered)
    for other in (
        "http://127.0.0.1:50001/other",
        "http://127.0.0.1:50001/callback/",
        "http://127.0.0.1:50001/callback?x=1",
        "http://localhost:43117/callback",
        "https://127.0.0.1:43117/callback",
        "http://127.0.0.1:50001/callback#x",
        "http://[::1]:43117/callback",
    ):
        assert not redirect_matches(other, registered), other
    assert redirect_matches("http://localhost:9/cb", ["http://localhost:43117/cb"])
    assert not redirect_matches("http://127.0.0.1:9/cb", ["http://localhost:43117/cb"])
    assert redirect_matches("http://[::1]:9/cb?a=1", ["http://[::1]:7/cb?a=1"])


def test_the_redirect_takes_its_parameters_with_the_right_separator():
    assert with_params("http://127.0.0.1:9/cb", {"code": "c", "state": "s"}) == (
        "http://127.0.0.1:9/cb?code=c&state=s"
    )
    assert with_params("https://app.example/cb?tab=1", {"code": "c"}) == (
        "https://app.example/cb?tab=1&code=c"
    )
    assert with_params("https://app.example/cb?", {"iss": "https://grove.example"}) == (
        "https://app.example/cb?iss=https%3A%2F%2Fgrove.example"
    )


def test_the_consent_page_names_where_the_redirect_goes():
    assert return_place(LOOPBACK_REDIRECT) == "this computer"
    assert return_place("https://app.example/cb") == "app.example"
    assert return_place("com.lumendesk.app:/cb") == "an app on this device (com.lumendesk.app:)"


# Registration metadata -------------------------------------------------------------------------


def test_a_registration_with_only_redirect_uris_is_confidential_by_default():
    metadata = parse_registration({"redirect_uris": [LOOPBACK_REDIRECT, LOOPBACK_REDIRECT]})
    assert metadata.redirect_uris == (LOOPBACK_REDIRECT,)
    assert metadata.auth_method == "client_secret_basic" and metadata.confidential
    assert metadata.grant_types == ("authorization_code", "refresh_token")
    assert metadata.response_types == ("code",)
    assert metadata.client_name is None


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ([], INVALID_CLIENT_METADATA),
        ({}, INVALID_REDIRECT_URI),
        ({"redirect_uris": []}, INVALID_REDIRECT_URI),
        ({"redirect_uris": LOOPBACK_REDIRECT}, INVALID_REDIRECT_URI),
        ({"redirect_uris": ["http://app.example/cb"]}, INVALID_REDIRECT_URI),
        ({"redirect_uris": [f"https://app.example/{n}" for n in range(11)]}, INVALID_REDIRECT_URI),
        ({"redirect_uris": [LOOPBACK_REDIRECT], "token_endpoint_auth_method": "private_key_jwt"},
         INVALID_CLIENT_METADATA),
        ({"redirect_uris": [LOOPBACK_REDIRECT], "grant_types": ["password"]},
         INVALID_CLIENT_METADATA),
        ({"redirect_uris": [LOOPBACK_REDIRECT], "grant_types": []}, INVALID_CLIENT_METADATA),
        ({"redirect_uris": [LOOPBACK_REDIRECT], "response_types": ["token"]},
         INVALID_CLIENT_METADATA),
        ({"redirect_uris": [LOOPBACK_REDIRECT], "client_name": 12}, INVALID_CLIENT_METADATA),
    ],
)  # fmt: skip
def test_a_registration_that_breaks_a_rule_is_refused_with_its_code(body, error):
    with pytest.raises(InvalidMetadata) as refused:
        parse_registration(body)
    assert refused.value.error == error


def test_a_client_name_loses_control_and_direction_characters_and_is_cut_to_100():
    assert clean_client_name("Lumen‮ Desk\x00\n") == "Lumen Desk"
    assert clean_client_name("  ​ ") is None
    assert clean_client_name("L" * 300) == "L" * 100
    assert clean_client_name(None) is None


# The consent page ------------------------------------------------------------------------------

FIELDS = {
    "client_id": "c-1",
    "redirect_uri": LOOPBACK_REDIRECT,
    "redirect_given": "true",
    "scope": "grove.read grove.write",
    "code_challenge": "a" * 43,
    "state": "st-1",
    "resource": "https://grove.example/mcp",
}


def _view(**changes) -> ConsentView:
    values = {
        "client_name": "Lumen Desk",
        "document_host": None,
        "return_place": "this computer",
        "scopes": ("grove.read", "grove.write"),
        "person_title": "Ines Duarte",
        **changes,
    }
    return ConsentView(**values)


def test_the_fragment_says_who_asks_for_what_and_offers_deny_first():
    page = consent_fragment(_view(), {"client_id": "c-1", "state": "st-1"}, "f" * 64)
    assert "An app that says it is <strong><q>Lumen Desk</q></strong>" in page
    assert "Signed in as <strong>Ines Duarte</strong>" in page
    assert "Grove sends you back to this computer" in page
    assert "Read what Grove knows: recall, what changed, what it breaks, questions" in page
    assert "Add to Grove: remember, answer questions, correct" in page
    assert "Forget things" not in page
    assert '<input type="hidden" name="client_id" value="c-1">' in page
    assert '<input type="hidden" name="csrf" value="' + "f" * 64 + '">' in page
    assert page.index('value="deny">Deny</button>') < page.index('value="approve">Approve</button>')
    assert 'action="/authorize/decision"' in page


def test_the_fragment_escapes_every_value_it_shows():
    hostile = '<script>alert(1)</script>"&'
    page = consent_fragment(
        _view(client_name=hostile, person_title=hostile, return_place=hostile),
        {"state": hostile},
        "0" * 64,
    )
    assert "<script>" not in page
    assert page.count("&lt;script&gt;alert(1)&lt;/script&gt;&quot;&amp;") == 4


def test_a_document_client_shows_its_host_and_its_name_as_its_own_words():
    page = consent_fragment(
        _view(document_host="lumen.example", client_name="Lumen Desk"), {}, "0" * 64
    )
    assert "<strong>lumen.example</strong> asks to use your Grove" in page
    assert "It calls itself <q>Lumen Desk</q>" in page
    assert "says it is" not in page
    assert "An app with no name" in consent_fragment(_view(client_name=None), {}, "0" * 64)


def test_sensitive_write_is_shown_in_words_when_it_is_asked():
    view = _view(scopes=("grove.read", "grove.sensitive_write"))
    assert "Forget things in Grove (each forget still asks you)" in consent_fragment(view, {}, "")


def test_the_csrf_value_is_bound_to_the_session_and_to_every_field():
    first = csrf_token("session-a", FIELDS)
    assert first == csrf_token("session-a", dict(reversed(FIELDS.items())))
    assert len(first) == 64 and int(first, 16) >= 0
    assert csrf_token("session-b", FIELDS) != first
    for name in FIELDS:
        assert csrf_token("session-a", {**FIELDS, name: FIELDS[name] + "x"}) != first, name
    # A value cannot forge the line of another field.
    forged = {**FIELDS, "state": "st-1\nresource=", "resource": ""}
    assert csrf_token("session-a", forged) != csrf_token("session-a", FIELDS)
    assert same_csrf(first, first) and not same_csrf(first[:-1] + "0", first)
    assert not same_csrf(None, first)


def test_the_page_takes_the_title_and_the_fragment_once_each():
    template = f"<title>{TITLE_MARK}</title><main>{CONSENT_MARK}</main>"
    page = render_page(template, title="A <b> title", fragment=error_fragment("Gone", "x < y"))
    assert page.startswith("<title>A &lt;b&gt; title</title>")
    assert '<h1 id="consent-title">Gone</h1>' in page and "x &lt; y" in page
    for broken in ("<main></main>", f"{TITLE_MARK}{TITLE_MARK}{CONSENT_MARK}", TITLE_MARK):
        with pytest.raises(TemplateBroken):
            render_page(broken, title="t", fragment="f")


def test_the_source_of_the_consent_page_holds_each_mark_once():
    source = (REPO / "web" / "consent.html").read_text(encoding="utf-8")
    assert source.count(TITLE_MARK) == 1 and source.count(CONSENT_MARK) == 1
    page = render_page(source, title="Connect an app: Grove", fragment="<h1>ok</h1>")
    assert "<title>Connect an app: Grove</title>" in page
    # The sheet of the public pages: the sim's tokens, light and dark.
    assert 'href="./site.css"' in source
    sheet = (REPO / "web" / "site.css").read_text(encoding="utf-8")
    assert "@media (prefers-color-scheme:dark)" in sheet and ":root[data-theme=dark]" in sheet
