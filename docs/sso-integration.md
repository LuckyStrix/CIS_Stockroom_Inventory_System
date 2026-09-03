# RIT single sign-on

**Status: implemented, and off by default.** The code is here and tested. It
does nothing until `STOCKROOM_AUTH_MODE` is changed, which must not happen
until RIT ITS have registered this service provider — see
[its-registration.md](its-registration.md), which is the ticket.

Sources, all of which need RIT credentials to read:

- [SSO — Deploying][deploy] · [Single Sign-On (SSO)][sso] ·
  [SSO — Shibboleth Service Provider][sp] ·
  [SSO — OneLogin Python SAML Toolkit][python]
- [SAML Cookbook][cookbook] — RIT's worked examples. Its
  [Python chapter][pysaml2] uses **PySAML2**, not the OneLogin toolkit this
  application uses; both are listed as acceptable on the Deploying page. It is
  also where RIT state that attributes "may be mapped to an alias such as
  `mail`, `uid`, `givenName` or may use an oid", which is why
  `saml._ATTRIBUTES` accepts both spellings of every attribute.
- [The ITS request form][form] — the ticket itself, and the source of the
  metadata template our document is shaped to match.
- RIT's IdP metadata: <https://shibboleth.main.ad.rit.edu/rit-metadata.xml>
- [RIT Security Standard: Web][webstd] — the standard this deployment is bound by

[deploy]: https://shibboleth.main.ad.rit.edu/ITSOperations/SSO---Deploying_22252854.html
[sso]: https://shibboleth.main.ad.rit.edu/ITSOperations/Single-Sign-On---SSO_22252855.html
[sp]: https://shibboleth.main.ad.rit.edu/ITSOperations/SSO---Shibboleth-Service-Provider_22252900.html
[python]: https://shibboleth.main.ad.rit.edu/ITSOperations/SSO---OneLogin-Python-SAML-Toolkit_22252902.html
[webstd]: https://www.rit.edu/security/sites/rit.edu.security/files/Web2017r1.pdf
[cookbook]: https://shibboleth.main.ad.rit.edu/docs/saml-cookbook/
[pysaml2]: https://shibboleth.main.ad.rit.edu/docs/saml-cookbook/code_examples/python.html
[form]: https://help.rit.edu/sp?id=sc_cat_item&sys_id=ab6aeaf31be2c0505d6afeeccd4bcb2a&sysparm_category=4d715cbb1b0ac0d07cc34377cc4bcba3

## What an earlier draft of this document got wrong

It was written from general Shibboleth knowledge, said so, and asked to be
checked against RIT's own pages before anyone built from it. That was the
right instinct. Four things were wrong:

| It said | RIT actually |
|---|---|
| request `displayName` | releases `givenName` + `sn`, which happen to match `account.first_name`/`last_name` exactly |
| request `eduPersonPrincipalName` | releases `uid`, scoped `@rit.edu` |
| request `eduPersonAffiliation` | releases `ritEduAffiliation`, plus `ritEduMemberOfUid` for groups |
| "point Sign out at the Shibboleth logout URL" | **publishes no `SingleLogoutService` at all** |

It also suggested `shibboleth-sp-utils` for nginx. There is no supported
Shibboleth module for nginx; RIT document mod_shib for Apache and IIS only.
And it marked in-app SAML "not recommended" — but that is one of the three
options RIT document, and it is the one built here.

## Why in-app SAML

The alternatives were Apache + mod_shib replacing nginx, or nginx chaining to
Apache on loopback. Both mean a second web server on a Raspberry Pi and a
rewrite of `deploy/nginx-stockroom.conf`, which is carefully commented and
pinned by tests. `python3-saml` is documented by RIT, keeps the single-server
deployment, and puts the identity code under this project's own test suite.

The cost is honest: `python3-saml`'s last release was v1.16.0 in **October
2023**. RIT's Web Security Standard §6.3 says applications lacking
developer-provided security patches shall be remediated or removed, so this
is disclosed in the ITS ticket rather than left to be discovered. The
mitigation is `stockroom/saml.py` — the *only* module that imports the
toolkit, with everything crossing its boundary as plain dataclasses, so
swapping it for `pysaml2` is one file.

The objections the earlier draft raised against in-app SAML were fair, and
each has an answer:

- *certificate rotation* — RIT's metadata is cached on disk and refreshed with
  `stockroom sso init --refresh`; `stockroom doctor` warns once the cache is
  six months old, because a stale copy fails every sign-in with a signature
  error that nobody guesses the cause of.
- *signature validation* — the toolkit's, with `strict` on and
  `wantAssertionsSigned` set. `tests/test_sso.py` proves an unsigned, wrongly
  signed, tampered, expired, misaddressed or misaudienced assertion is refused.
- *replay protection* — single-use handshake rows; see below.

## The shape of it

```
browser ──TLS──> nginx ──> uvicorn (stockroom)
                             │
                             ├─ GET  /sso/login       -> AuthnRequest, 303 to RIT
                             ├─ POST /sso/acs         <- signed SAMLResponse
                             ├─ GET  /sso/metadata    -> our metadata, for ITS
                             └─ GET  /sso/signed-out
```

**Single sign-on does not replace the identity seam, it feeds it.** `/sso/acs`
finishes by calling `accounts.sso_login`, which writes an ordinary row in the
`session` table and sets an ordinary cookie. `deps.current_account` is
unchanged, and so is every route, all five service modules and the audit log.
That is why this branch adds behaviour without touching any of them.

`deploy/nginx-stockroom.conf` needs **no change**, and its
`proxy_set_header X-Shib-Mail ""` lines stay. The application never reads an
identity header — `test_the_application_never_reads_an_identity_header`
enforces that in the source — so the impersonation risk the earlier draft
warned about never arises. **Do not "simplify" this by trusting a header.** On
a campus-reachable host that is impersonation-as-a-service for anyone on the
network.

## The one CSRF exemption, and what replaces it

`/sso/acs` is the only POST in the application that does not check a CSRF
token. It cannot: it is a top-level cross-site form POST from an identity
provider that has never seen our token. The exemption is
`deps.CSRF_EXEMPT_PATHS`, it has exactly one member, and
`test_the_csrf_exemption_is_exactly_one_path` fails if it grows.

Three things must line up before an assertion is accepted, and it is worth
being precise about which does which job:

1. **The signature**, checked against the IdP key in the cached metadata,
   along with audience, recipient, destination and expiry.
2. **`InResponseTo`**, which must name a sign-in *this server* started, and
   which is covered by the signature so it cannot be edited.
3. **A state cookie**, whose SHA-256 is stored on the handshake row, proving
   the response came back to the *same browser* that asked.

The third is not decoration, and the reasoning matters because the intuitive
answer is wrong. The threat here is **login CSRF** — the attack `deps.py`
already describes for `/login`: an attacker makes your browser complete *their*
sign-in, you end up in their account, and they read what you do in it.

- The signature does not stop it. The assertion is genuinely signed by RIT.
- `InResponseTo` alone does not stop it either. The attacker starts their own
  sign-in, has RIT sign an assertion naming themselves, and simply never
  completes it — so the row is unspent and the ID matches.
- **The cookie stops it**, because the victim's browser does not have it.

`test_an_assertion_bound_to_another_browser_is_refused` is that test, and it
has been checked against a deliberately weakened implementation: remove the
browser binding and it fails.

The handshake row is spent *before* the assertion is checked, so a signature
failure burns it. That costs a legitimate user one extra click on a path that
is already failing, and denies an attacker repeated attempts against a live
handshake. Consumption is an `UPDATE ... WHERE consumed_at IS NULL` inside
`db.transaction()`, so a replay loses a race rather than being merely unlikely
to win one — the same `BEGIN IMMEDIATE` property that stops two people
checking out the same last unit.

### The state cookie is `SameSite=None`, and has to be

A `Lax` cookie is sent on a cross-site request only when it is a *navigation
with a safe method*. RIT's reply is a cross-site **POST**, so `Lax` would mean
the cookie never arrives and every sign-in fails. `None` requires `Secure`,
which is why **single sign-on requires TLS**; plain HTTP falls back to `Lax`
and an unprefixed name, which is enough for the test client and nothing else.

What bounds the risk is that the cookie is worth almost nothing: it is not the
session, it is `HttpOnly`, it is single-use, it expires in five minutes, and
the only thing it can do is finish one specific pending sign-in that also
requires a signed assertion from RIT.

### Rejected alternatives

- **HTTP-Artifact binding** would make the ACS a GET and sidestep the question
  entirely. RIT publishes no `ArtifactResolutionService`, and it needs a SOAP
  back channel from the Pi. Not available.
- **Put the CSRF token in `RelayState`.** Works, and leaves the middleware
  untouched. Rejected because `RelayState` is logged by the IdP, and putting
  our CSRF token in another organisation's logs is a needless disclosure.
  `RelayState` stays an opaque nonce.
- **A two-step ACS** (POST, then a one-time code, then a GET) does not remove
  the tokenless POST, so it does not solve the problem, and does nothing
  against login CSRF.

## Attributes

Requested from ITS, and what each is for:

| Attribute | Used for |
|---|---|
| `uid` | `account.sso_uid` — the stable join key. Matched *first*. |
| `mail` | `account.email`; the fallback join key on first sign-in only |
| `givenName` / `sn` | `account.first_name` / `last_name`, straight across |
| `ritEduAffiliation` | stored in `account.affiliation`; **not** used for roles |
| `ritEduMemberOfUid` | parsed, currently unused; requested so group-driven roles stay possible without a second ticket |

A Shibboleth IdP may send these under friendly names or as OID URNs depending
on the release policy, so `saml._ATTRIBUTES` accepts either.

**Attributes only arrive when somebody authenticates.** RIT are explicit that
SAML is not a queryable directory, so there is no way to look up a person who
has not signed in. That is why borrower records are still just a name and an
email — the stockroom lends to visitors with no RIT login at all.

## Signing, encryption and SHA-1

Three settings, and the trap is that two of them do not mean what their names
suggest.

**`STOCKROOM_SSO_SIGN_REQUESTS` (default on) is a metadata setting.** It sets
`AuthnRequestsSigned` in the document ITS register as well as deciding whether
we actually sign. Changing it after registration means sending ITS a new
document, so it is not a restart-and-see knob. On by default because RIT ask
for it twice: `signing="true"` on the service provider page, and
`AuthnRequestsSigned="true"` in the request form's template.

**`STOCKROOM_SSO_ENCRYPTED_ASSERTIONS` (default off) is not about whether RIT
can encrypt.** Our metadata publishes an `encryption` `KeyDescriptor`
unconditionally, and python3-saml decrypts an `EncryptedAssertion` whenever it
finds one. This flag only decides whether an assertion arriving *in the clear*
is refused. It is off because turning it on before knowing ITS encrypt would
refuse every sign-in; turn it on once one has been seen arriving encrypted.

That separation costs an override in `saml.sp_metadata`, because the toolkit
conflates the two: `get_sp_metadata` emits the encryption key only when
`wantAssertionsEncrypted` is set, and that same flag makes cleartext a hard
error. The honest combination — "you may encrypt, and we will not refuse you
if you do not" — is unreachable through the settings alone. It matters because
a Shibboleth IdP encrypts to whichever service providers advertise a key: with
the toolkit's default we would have registered a document saying we cannot
decrypt, and turning encryption on later would have been a second ticket.

**`STOCKROOM_SSO_REJECT_SHA1` (default on) is a stopgap that should never be
used.** It exists because of what `rit-metadata.xml` looks like: a signing
certificate issued in 2008, `urn:mace:shibboleth:1.0` and SAML 1.1 still
advertised. A Shibboleth IdP of that age signed with RSA-SHA1, which we refuse
— correctly, since SHA-1 collisions are practical. If ITS turn out to be one,
the alternative to this switch is nobody signing in until another team changes
something, so the switch exists; `stockroom doctor` reports WARN for as long
as it is off, and question 1 of the ITS ticket is there to make sure it never
has to be. Nothing in the test suite can catch this in production's favour —
`tests/fixtures/saml_idp.py` signs whichever way the test asks, and both
positions are tested, but only RIT can say which one they are.

## Roles

Manual, unchanged. The IdP proves identity; it does not decide who works in
this stockroom. A first sign-in provisions at `requester`, and an admin
promotes from `/accounts` exactly as now. "Employee" is a far larger set than
"works here", so driving `staff` off `ritEduAffiliation` would be wrong;
driving it off a `ritEduMemberOfUid` group would need ITS to create and
maintain one, which is a decision the stockroom can make later without any
code change beyond reading a field that is already parsed.

## Logout

**RIT's IdP publishes no `SingleLogoutService`.** Signing out therefore cannot
end the RIT session, and no amount of code changes that. Do not invent a
logout URL; one that 404s is worse than none.

`POST /logout` revokes the session row and clears the cookie as it always has.
Under `sso` mode it then lands on `/sso/signed-out` rather than `/login` —
because `/login` forwards to RIT, RIT still recognises the browser, and the
person would be signed straight back in, so "Sign out" would visibly do
nothing. That page says plainly that the RIT session is still open and that
closing the browser is what finishes the job.

This is a real residual risk on the counter machine, and the honest mitigations
are a short `STOCKROOM_SESSION_IDLE_HOURS` and the habit of closing the
browser — not a URL that does not exist.

## Migrating

Accounts are keyed by RIT email, and so is `mail`, so **existing accounts light
up on first SSO sign-in** with their roles, history and person link intact.
Nobody re-registers. The account keeps its password too, so it can use either
door until the stockroom closes one.

Run `both` for a term. Then `sso`. Deleting the password machinery — `login`,
`register`, `change_password`, the lockout table, `security.hash_password` —
is a separate change with its own migration, and should not happen until SSO
has been working for long enough that nobody wants it back.

**The escape hatch.** If single sign-on ever breaks — an expired certificate,
an IdP outage, a metadata rotation nobody caught — set
`STOCKROOM_AUTH_MODE="password"` in `/etc/stockroom.env` and
`systemctl restart stockroom`. Everyone who has a password is back in
immediately. That is the single most important operational sentence in this
file, and it is the reason the password code is still here.

## Deliberately out of scope

- **Authorising borrowers.** The person a loan is tagged to stays a name and
  an email. The stockroom lends to visitors and collaborators with no RIT
  login, and SAML cannot tell us about someone who has not signed in anyway.
- **The public page.** `/public/*` stays open to everyone, signed in or not.
