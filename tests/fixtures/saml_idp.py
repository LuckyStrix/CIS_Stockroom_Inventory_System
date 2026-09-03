"""A fake Shibboleth identity provider, in process.

Real SAML tests need a real signature, and a real signature needs a private
key. Rather than check one into the repository -- where it would look exactly
like a leaked credential to every scanner that ever reads this tree -- the
keys are generated with `openssl` when the test session starts and thrown away
when it ends. openssl is not an extra dependency: deploy/setup-pi.sh already
requires it to make the Pi's TLS certificate.

The metadata this produces is shaped like RIT's real rit-metadata.xml, which
matters in one specific way: **it declares no SingleLogoutService**, because
RIT's does not. Testing against a fake IdP that offers SLO would let a change
that depends on SLO pass here and fail in the stockroom.
"""

from __future__ import annotations

import base64
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

IDP_ENTITY_ID = "https://test-idp.invalid/idp/shibboleth"
IDP_SSO_URL = "https://test-idp.invalid/idp/profile/SAML2/Redirect/SSO"


def _stamp(offset_seconds: int = 0) -> str:
    when = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Keypair:
    cert_path: Path
    key_path: Path

    @property
    def cert_pem(self) -> str:
        return self.cert_path.read_text()

    @property
    def key_pem(self) -> str:
        return self.key_path.read_text()

    @property
    def cert_body(self) -> str:
        """The base64 between the PEM banners, which is what SAML metadata wants."""
        lines = [
            line for line in self.cert_pem.splitlines()
            if line and not line.startswith("-----")
        ]
        return "".join(lines)


def make_keypair(directory: Path, common_name: str) -> Keypair:
    directory.mkdir(parents=True, exist_ok=True)
    cert = directory / f"{common_name}.crt"
    key = directory / f"{common_name}.key"
    if not cert.exists():
        subprocess.run(
            ["openssl", "req", "-x509", "-nodes", "-newkey", "rsa:2048",
             "-days", "2", "-keyout", str(key), "-out", str(cert),
             "-subj", f"/CN={common_name}/O=stockroom test suite"],
            check=True, capture_output=True,
        )
    return Keypair(cert_path=cert, key_path=key)


def idp_metadata(keypair: Keypair) -> str:
    """Metadata in RIT's shape: several SSO bindings, and no SLO."""
    return f"""<?xml version="1.0"?>
<EntityDescriptor xmlns="urn:oasis:names:tc:SAML:2.0:metadata"
                  entityID="{IDP_ENTITY_ID}">
  <IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
    <KeyDescriptor use="signing">
      <ds:KeyInfo xmlns:ds="http://www.w3.org/2000/09/xmldsig#">
        <ds:X509Data><ds:X509Certificate>{keypair.cert_body}</ds:X509Certificate></ds:X509Data>
      </ds:KeyInfo>
    </KeyDescriptor>
    <SingleSignOnService
        Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
        Location="{IDP_SSO_URL}"/>
    <SingleSignOnService
        Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
        Location="https://test-idp.invalid/idp/profile/SAML2/POST/SSO"/>
  </IDPSSODescriptor>
</EntityDescriptor>
"""


_DEFAULT_ATTRIBUTES = {
    "uid": ["abc1234"],
    "mail": ["abc1234@rit.edu"],
    "givenName": ["Ada"],
    "sn": ["Byron"],
    "ritEduAffiliation": ["Student"],
}


def _attribute_xml(attributes: dict[str, list[str]]) -> str:
    blocks = []
    for name, values in attributes.items():
        rendered = "".join(
            '<saml:AttributeValue xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
            ' xsi:type="xs:string" xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            f"{value}</saml:AttributeValue>"
            for value in values
        )
        blocks.append(
            f'<saml:Attribute Name="{name}" '
            'NameFormat="urn:oasis:names:tc:SAML:2.0:attrname-format:basic">'
            f"{rendered}</saml:Attribute>"
        )
    return "".join(blocks)


def signed_response(
    keypair: Keypair,
    *,
    sp_entity_id: str,
    acs_url: str,
    in_response_to: str,
    attributes: dict[str, list[str]] | None = None,
    lifetime_seconds: int = 300,
    sign: bool = True,
    issuer: str | None = None,
    digest: str = "sha256",
) -> str:
    """A base64 SAMLResponse, signed the way a real Shibboleth IdP signs one.

    The Assertion carries the signature, not the Response, because the
    settings this project uses set ``wantAssertionsSigned``. Signing the
    envelope instead would leave the assertion itself unprotected, which is
    the mistake the setting exists to prevent.

    ``digest="sha1"`` signs with RSA-SHA1 and a SHA-1 digest, which is how a
    Shibboleth identity provider of the vintage RIT's metadata suggests would
    sign. It exists so that `config.SSO_REJECT_SHA1` can be tested in both
    positions -- a switch nothing ever flips is a switch nobody knows works.
    """
    from onelogin.saml2.constants import OneLogin_Saml2_Constants
    from onelogin.saml2.utils import OneLogin_Saml2_Utils

    attributes = _DEFAULT_ATTRIBUTES if attributes is None else attributes
    issuer = issuer or IDP_ENTITY_ID
    assertion_id = f"_{uuid.uuid4().hex}"
    response_id = f"_{uuid.uuid4().hex}"
    issued = _stamp()
    expires = _stamp(lifetime_seconds)

    assertion = f"""<saml:Assertion
    xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion"
    ID="{assertion_id}" Version="2.0" IssueInstant="{issued}">
  <saml:Issuer>{issuer}</saml:Issuer>
  <saml:Subject>
    <saml:NameID Format="urn:oasis:names:tc:SAML:2.0:nameid-format:transient"
                 >_{uuid.uuid4().hex}</saml:NameID>
    <saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">
      <saml:SubjectConfirmationData NotOnOrAfter="{expires}"
          Recipient="{acs_url}" InResponseTo="{in_response_to}"/>
    </saml:SubjectConfirmation>
  </saml:Subject>
  <saml:Conditions NotBefore="{_stamp(-30)}" NotOnOrAfter="{expires}">
    <saml:AudienceRestriction><saml:Audience>{sp_entity_id}</saml:Audience></saml:AudienceRestriction>
  </saml:Conditions>
  <saml:AuthnStatement AuthnInstant="{issued}" SessionIndex="{assertion_id}">
    <saml:AuthnContext><saml:AuthnContextClassRef
      >urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport</saml:AuthnContextClassRef></saml:AuthnContext>
  </saml:AuthnStatement>
  <saml:AttributeStatement>{_attribute_xml(attributes)}</saml:AttributeStatement>
</saml:Assertion>"""

    if sign:
        algorithms = {
            "sha256": (OneLogin_Saml2_Constants.RSA_SHA256,
                       OneLogin_Saml2_Constants.SHA256),
            "sha1": (OneLogin_Saml2_Constants.RSA_SHA1,
                     OneLogin_Saml2_Constants.SHA1),
        }
        sign_algorithm, digest_algorithm = algorithms[digest]
        signed = OneLogin_Saml2_Utils.add_sign(
            assertion, keypair.key_pem, keypair.cert_pem,
            sign_algorithm=sign_algorithm,
            digest_algorithm=digest_algorithm,
        )
        assertion = signed.decode("utf-8") if isinstance(signed, bytes) else signed
        # add_sign returns a whole document; drop its XML declaration so the
        # assertion can be embedded in the Response below.
        if assertion.startswith("<?xml"):
            assertion = assertion.split("?>", 1)[1].lstrip()

    response = f"""<samlp:Response
    xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"
    xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion"
    ID="{response_id}" Version="2.0" IssueInstant="{issued}"
    Destination="{acs_url}" InResponseTo="{in_response_to}">
  <saml:Issuer>{issuer}</saml:Issuer>
  <samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>
  {assertion}
</samlp:Response>"""

    return base64.b64encode(response.encode("utf-8")).decode("ascii")
