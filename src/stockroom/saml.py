"""SAML 2.0, spoken to RIT's Shibboleth identity provider.

    ======================================================================
    THE ONLY MODULE THAT IMPORTS python3-saml. Everything it needs arrives
    as an argument and everything it returns is a plain dataclass -- no
    sqlite3, no fastapi, no Request. That boundary is the whole point: the
    toolkit is swappable without touching a route, and the routes are
    testable without the toolkit installed.
    ======================================================================

RIT documents three ways to be a service provider (see docs/sso-integration.md
for the links): the Shibboleth SP binary, which is Apache and IIS only; a
"native" SAML implementation; and the OneLogin toolkits, of which
``python3-saml`` is the Python one. This is that third option. It keeps the
single nginx deployment the Pi already has, and it means the identity code is
covered by this project's own test suite rather than by a second web server's
configuration file.

Two facts about RIT's IdP shape everything here:

* **It publishes no SingleLogoutService.** There is no SLO endpoint in
  rit-metadata.xml, so a logout cannot be propagated. See
  `web/routes_auth.logout` for what is done instead.
* **Attributes are ``uid``, ``givenName``, ``sn``, ``mail``,
  ``ritEduAffiliation`` and ``ritEduMemberOfUid``** -- not ``displayName``,
  not ``eduPersonPrincipalName``, not ``eduPersonAffiliation``, whatever a
  generic Shibboleth guide says. A Shibboleth IdP may send those under their
  friendly names or as OID URNs depending on how the release policy is
  written, so :func:`_attr` accepts either.

The library is imported lazily, inside the functions that need it. The
application must start, serve and be tested with it absent -- which is the
state of a development checkout, and of the Pi until somebody turns single
sign-on on.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from . import config


class SamlError(Exception):
    """An assertion could not be trusted, or a request could not be built."""


class SamlNotConfigured(SamlError):
    """Single sign-on was asked for before it was set up."""


@dataclass(frozen=True, slots=True)
class AuthnRequest:
    request_id: str
    redirect_url: str


@dataclass(frozen=True, slots=True)
class Assertion:
    """What RIT told us about the person who just signed in."""

    sso_uid: str
    email: str
    first_name: str
    last_name: str
    affiliation: str
    groups: tuple[str, ...]
    name_id: str
    session_index: str


# Friendly name first, then the OID URN a Shibboleth IdP sends when the
# release policy does not set a friendly name. Order matters only in that the
# first one present wins.
_ATTRIBUTES = {
    "uid": ("uid", "urn:oid:0.9.2342.19200300.100.1.1"),
    "mail": ("mail", "urn:oid:0.9.2342.19200300.100.1.3"),
    "givenName": ("givenName", "urn:oid:2.5.4.42"),
    "sn": ("sn", "urn:oid:2.5.4.4"),
    # RIT's own attributes have no registered OID; the eduPerson equivalents
    # are listed as a fallback in case ITS release those instead.
    "ritEduAffiliation": (
        "ritEduAffiliation",
        "eduPersonAffiliation",
        "urn:oid:1.3.6.1.4.1.5923.1.1.1.1",
    ),
    "ritEduMemberOfUid": (
        "ritEduMemberOfUid",
        "isMemberOf",
        "urn:oid:1.3.6.1.4.1.5923.1.5.1.1",
    ),
}


def _toolkit():
    """Import python3-saml, or explain what is missing.

    Deliberately not a module-level import. `stockroom` runs in password mode
    with this library absent and must not fail to start because of an optional
    dependency -- and the test suite must be runnable on a machine that has
    never built xmlsec.
    """
    try:
        from onelogin.saml2.auth import OneLogin_Saml2_Auth
        from onelogin.saml2.idp_metadata_parser import OneLogin_Saml2_IdPMetadataParser
        from onelogin.saml2.settings import OneLogin_Saml2_Settings
    except ImportError as exc:  # pragma: no cover - exercised by the mode tests
        raise SamlNotConfigured(
            "python3-saml is not installed. Install the SSO extra:\n"
            "    .venv/bin/pip install -e '.[sso]'"
        ) from exc
    return OneLogin_Saml2_Auth, OneLogin_Saml2_Settings, OneLogin_Saml2_IdPMetadataParser


def is_available() -> bool:
    """Whether the toolkit can be imported at all."""
    try:
        _toolkit()
    except SamlNotConfigured:
        return False
    return True


def acs_url() -> str:
    """Where RIT posts the assertion back to.

    Built from configuration, never from the Host header. The application
    builds no absolute URL from Host (see CLAUDE.md), and this is the one
    place where the temptation arises -- the toolkit needs a current URL to
    validate Destination and Recipient against, and taking that from a header
    the caller controls is how a service provider is talked into accepting an
    assertion minted for somewhere else.
    """
    if not config.SSO_BASE_URL:
        raise SamlNotConfigured("STOCKROOM_SSO_BASE_URL is not set.")
    return f"{config.SSO_BASE_URL}/sso/acs"


def entity_id() -> str:
    if not config.SSO_ENTITY_ID:
        raise SamlNotConfigured("STOCKROOM_SSO_BASE_URL is not set.")
    return config.SSO_ENTITY_ID


def missing_pieces() -> list[str]:
    """Everything that still has to be in place, in plain English.

    Used by `stockroom doctor` and by the sign-in route, so that a
    half-configured server says which half.
    """
    problems: list[str] = []
    if not is_available():
        problems.append("python3-saml is not installed (pip install -e '.[sso]')")
    if not config.SSO_BASE_URL:
        problems.append("STOCKROOM_SSO_BASE_URL is not set")
    elif not config.SSO_BASE_URL.startswith("https://"):
        problems.append(
            f"STOCKROOM_SSO_BASE_URL is {config.SSO_BASE_URL!r}, which is not https://"
        )
    for label, path in (
        ("the IdP metadata", config.SSO_IDP_METADATA),
        ("the SP certificate", config.SSO_SP_CERT),
        ("the SP private key", config.SSO_SP_KEY),
    ):
        if not path.exists():
            problems.append(f"{label} is missing ({path})")
    return problems


def is_configured() -> bool:
    return not missing_pieces()


def _require_configured() -> None:
    problems = missing_pieces()
    if problems:
        raise SamlNotConfigured("Single sign-on is not set up: " + "; ".join(problems))


def _read(path) -> str:
    try:
        return path.read_text()
    except OSError as exc:
        raise SamlNotConfigured(f"Cannot read {path}: {exc}") from exc


# The parsed IdP metadata and the keypair, cached until one of the files
# changes. Everything else in settings_dict() is string assembly; this is the
# part that costs -- lxml over RIT's metadata plus three file reads -- and it
# used to happen on every /sso/login, twice per sign-in, and on every hit of
# the public, unauthenticated /sso/metadata.
#
# Keyed on each file's path, size and mtime, which covers both the thing that
# changes it in production -- `stockroom sso init --refresh` rewriting the
# metadata -- and the thing that changes it in the suite, a test pointing
# config.SSO_* at its own tmp_path.
_MATERIAL: tuple | None = None


def _stat_key(path) -> tuple:
    try:
        info = path.stat()
    except OSError:
        return (str(path), None, None)
    return (str(path), info.st_mtime_ns, info.st_size)


def _material_key() -> tuple:
    return (
        _stat_key(config.SSO_IDP_METADATA),
        _stat_key(config.SSO_SP_CERT),
        _stat_key(config.SSO_SP_KEY),
    )


def _material() -> tuple[dict, str, str]:
    """(idp settings, SP certificate, SP private key), parsed at most once."""
    global _MATERIAL

    key = _material_key()
    if _MATERIAL is not None:
        cached_key, idp, cert, private_key = _MATERIAL
        if cached_key == key:
            return idp, cert, private_key

    _, _, IdPMetadataParser = _toolkit()
    try:
        parsed = IdPMetadataParser.parse(_read(config.SSO_IDP_METADATA))
    except Exception as exc:  # the parser raises a variety of lxml errors
        raise SamlNotConfigured(
            f"{config.SSO_IDP_METADATA} is not usable SAML metadata: {exc}"
        ) from exc

    idp = parsed.get("idp") or {}
    if not idp.get("entityId") or not idp.get("singleSignOnService"):
        raise SamlNotConfigured(
            f"{config.SSO_IDP_METADATA} names no identity provider. "
            "Refresh it with `stockroom sso init --refresh`."
        )

    cert = _read(config.SSO_SP_CERT)
    private_key = _read(config.SSO_SP_KEY)
    _MATERIAL = (key, idp, cert, private_key)
    return idp, cert, private_key


def settings_dict() -> dict:
    """The toolkit's settings, assembled from config and the cached metadata.

    A fresh dict every call, deliberately: the toolkit's `_add_default_values`
    mutates what it is handed, so a shared one would accumulate defaults from
    every previous use. Only the expensive parsing is cached -- see
    :func:`_material`.
    """
    _require_configured()
    idp, cert, private_key = _material()

    return {
        # strict means every validation the specification calls for is
        # actually enforced. Turning it off is how service providers end up
        # accepting unsigned assertions.
        "strict": True,
        "debug": False,
        "sp": {
            "entityId": entity_id(),
            "assertionConsumerService": {
                "url": acs_url(),
                # POST, because that is what RIT's metadata offers and what
                # the browser can deliver. The AuthnRequest goes out over
                # Redirect -- see build_authn_request.
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
            },
            "NameIDFormat":
                "urn:oasis:names:tc:SAML:2.0:nameid-format:transient",
            "x509cert": cert,
            "privateKey": private_key,
        },
        # Copied, for the same reason the outer dict is rebuilt: the toolkit
        # writes defaults into the nested settings it is given.
        "idp": dict(idp),
        "security": {
            "authnRequestsSigned": config.SSO_SIGN_REQUESTS,
            "wantAssertionsSigned": True,
            "wantAssertionsEncrypted": config.SSO_ENCRYPTED_ASSERTIONS,
            "wantMessagesSigned": False,
            "wantNameId": True,
            # Left to the identity provider. ITS decide who is prompted for
            # multi-factor authentication and when; a service provider that
            # demands a particular authentication context is overriding a
            # policy that is not its to set.
            "requestedAuthnContext": False,
            "signatureAlgorithm":
                "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256",
            "digestAlgorithm": "http://www.w3.org/2001/04/xmlenc#sha256",
            # Refuses RSA-SHA1 and SHA-1 digests on the way in. A switch
            # rather than a constant because RIT's IdP is old enough that it
            # might still sign that way -- see config.SSO_REJECT_SHA1, which
            # explains why the escape hatch has to exist and why using it is
            # a weakening.
            "rejectDeprecatedAlgorithm": config.SSO_REJECT_SHA1,
        },
    }


def _request_data(post_data: dict | None = None) -> dict:
    """The synthetic request the toolkit wants, built from configuration."""
    parts = urlparse(config.SSO_BASE_URL)
    data = {
        "https": "on" if parts.scheme == "https" else "off",
        "http_host": parts.hostname or "",
        "script_name": "/sso/acs",
        "get_data": {},
        "post_data": post_data or {},
    }
    if parts.port:
        data["server_port"] = str(parts.port)
    return data


def sp_metadata() -> str:
    """Our own metadata -- the document RIT ITS need in order to register us.

    The published document always offers an **encryption** key as well as a
    signing one, whatever ``config.SSO_ENCRYPTED_ASSERTIONS`` says. Those are
    two different questions and the toolkit conflates them:
    ``get_sp_metadata`` emits the encryption ``KeyDescriptor`` only when
    ``wantAssertionsEncrypted`` is set, and that flag *also* makes an
    unencrypted assertion a hard error. So the honest combination -- "you may
    encrypt to us, and we will not refuse you if you do not" -- is
    unreachable through the settings alone, and the default produced metadata
    that told RIT we could not decrypt anything.

    That mattered here. RIT's service provider page configures
    ``encryption="true"``, the metadata template on the ITS request form
    carries both key descriptors, and RIT's own Python cookbook has an
    ``encryption_keypairs`` entry. A Shibboleth IdP encrypts to whichever SPs
    advertise a key, so publishing one is what lets ITS turn encryption on
    without us re-registering. Decryption needs nothing else switched on:
    python3-saml decrypts an ``EncryptedAssertion`` whenever it finds one.

    So the override below is scoped to this document and does not reach
    :func:`parse_response`, where ``wantAssertionsEncrypted`` keeps meaning
    what the operator set it to.
    """
    _, Settings, _ = _toolkit()
    settings = settings_dict()
    settings["security"] = {
        **settings["security"], "wantAssertionsEncrypted": True
    }
    built = Settings(settings, sp_validation_only=True)
    metadata = built.get_sp_metadata()
    errors = built.validate_metadata(metadata)
    if errors:
        raise SamlError(f"The generated metadata is invalid: {errors}")
    return metadata.decode("utf-8") if isinstance(metadata, bytes) else metadata


def build_authn_request(*, relay_state: str) -> AuthnRequest:
    """Start a sign-in: where to send the browser, and what to expect back.

    HTTP-Redirect binding for the request. Not merely convention -- the
    content security policy is ``form-action 'self'``, so a POST-binding
    AuthnRequest would be an auto-submitting cross-origin form that our own
    policy blocks.
    """
    Auth, _, _ = _toolkit()
    auth = Auth(_request_data(), old_settings=settings_dict())
    try:
        url = auth.login(return_to=relay_state)
    except Exception as exc:
        raise SamlError(f"Could not build an authentication request: {exc}") from exc
    return AuthnRequest(request_id=auth.get_last_request_id(), redirect_url=url)


def _attr(attributes: dict, key: str) -> list[str]:
    for name in _ATTRIBUTES[key]:
        values = attributes.get(name)
        if values:
            return [str(v) for v in values if v is not None]
    return []


def _one(attributes: dict, key: str) -> str:
    values = _attr(attributes, key)
    return values[0].strip() if values else ""


def parse_response(*, saml_response: str, request_id: str) -> Assertion:
    """Validate an assertion and return what it says. Raise otherwise.

    ``request_id`` is passed to the toolkit so that InResponseTo is checked
    against the request we actually sent, rather than merely being present.
    That is the belt; the single-use handshake row in
    `security.consume_saml_handshake` is the braces, and it is the braces that
    stop login CSRF -- see the comment above the table in schema.sql.
    """
    Auth, _, _ = _toolkit()
    auth = Auth(
        _request_data({"SAMLResponse": saml_response}),
        old_settings=settings_dict(),
    )
    try:
        auth.process_response(request_id=request_id)
    except Exception as exc:
        raise SamlError(f"The response could not be processed: {exc}") from exc

    errors = auth.get_errors()
    if errors:
        raise SamlError(
            f"{', '.join(errors)}: {auth.get_last_error_reason() or 'no detail'}"
        )
    if not auth.is_authenticated():
        raise SamlError("The identity provider did not authenticate this person.")

    attributes = auth.get_attributes() or {}
    uid = _one(attributes, "uid")
    email = _one(attributes, "mail")
    if not uid or not email:
        # A signed, valid assertion that does not say who it is about. This is
        # a release-policy problem at the IdP, not a user error, so it names
        # what was actually received -- an administrator reading the log needs
        # to be able to take this to ITS.
        raise SamlError(
            "The assertion carried no uid or mail attribute. RIT ITS must "
            f"release both. Received: {sorted(attributes)}"
        )

    return Assertion(
        sso_uid=uid,
        email=email,
        first_name=_one(attributes, "givenName"),
        last_name=_one(attributes, "sn"),
        # Multi-valued: someone can be a Student and a StudentWorker at once.
        affiliation=",".join(_attr(attributes, "ritEduAffiliation")),
        groups=tuple(_attr(attributes, "ritEduMemberOfUid")),
        name_id=auth.get_nameid() or "",
        session_index=auth.get_session_index() or "",
    )
