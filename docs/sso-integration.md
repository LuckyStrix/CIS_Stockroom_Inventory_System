# Future work: RIT single sign-on

**Status: planned, not implemented.** Today the system asks operators to type
their name once and stores it in a cookie. This document is the plan for
replacing that with real RIT authentication.

Reference: [RIT ITS — Single Sign-On (SSO)][rit-sso]. That page requires RIT
credentials, so **read it and confirm the details below before starting the
work** — the specifics of RIT's registration process are the one part of this
plan written from general Shibboleth knowledge rather than from the page
itself.

[rit-sso]: https://shibboleth.main.ad.rit.edu/ITSOperations/Single-Sign-On---SSO_22252855.html

## What we have today

Identity is **self-declared and unverified**. On first use the browser is
asked "who are you?", and the answer is stored in the `stockroom_operator`
cookie and recorded on every audit event.

This is deliberate, not an oversight. The system is on the stockroom LAN, the
threat model is "who moved the tripod", and the audit log needs a name against
each change far more than it needs a password. Anyone on the network can
currently claim to be anyone.

## Why the change is small

All of the identity logic is in exactly one function:

```
src/stockroom/web/deps.py :: current_actor()
```

Everything downstream — every route, the whole service layer, the audit log —
receives an `Actor` object and never asks where it came from. `current_actor()`
**already reads Shibboleth attribute headers** and prefers them over the
cookie, so a correctly configured SP in front of the app makes SSO work with
no code change at all. `tests/test_web.py::test_sso_headers_take_precedence_over_the_cookie`
covers that path.

## Recommended approach: Shibboleth SP in front of the app

RIT runs a **Shibboleth SAML 2.0 identity provider**. The lowest-effort and
most standard integration is to put the official Service Provider in front of
the application rather than speaking SAML from Python.

```
browser ──TLS──> nginx/Apache + mod_shib ──plain HTTP──> uvicorn (stockroom)
                       │                                      │
                       │  handles the whole SAML dance        │  reads
                       └─> sets X-Shib-* request headers ─────┘  current_actor()
```

The SP handles metadata exchange, signing, encryption, session cookies and
logout — all the parts that are easy to get subtly and dangerously wrong.

### Steps

1. **Get a stable public hostname and TLS certificate.** The IdP redirects the
   browser back to a fixed Assertion Consumer Service URL, so the Pi needs a
   real DNS name (e.g. `stockroom.cis.rit.edu`) and a valid certificate. This
   almost certainly requires ITS to place the Pi somewhere appropriate on the
   network.

2. **Install and configure the SP.**
   ```bash
   sudo apt install libapache2-mod-shib   # or shibboleth-sp-utils for nginx
   ```
   Set `entityID` in `/etc/shibboleth/shibboleth2.xml`, generate SP keys, and
   register the SP's metadata with RIT ITS.

3. **Register with RIT ITS.** Supply the entityID, ACS URL and metadata, and
   request the attributes below. **This is the long pole** — it is a request to
   another team, not a code change, so start it first.

4. **Request these attributes** (standard eduPerson; confirm exact names with
   ITS):

   | Attribute | Used for |
   |---|---|
   | `mail` | the `Actor.email`, and the join key to `person.email` |
   | `displayName` | the `Actor.name` shown in the history |
   | `eduPersonPrincipalName` | stable unique id, if `mail` ever changes |
   | `eduPersonAffiliation` | staff/student/faculty, for the roles below |

5. **Protect the app and pass the attributes through.** Apache:
   ```apache
   <Location />
       AuthType shibboleth
       ShibRequestSetting requireSession 1
       Require valid-user
       RequestHeader set X-Shib-Mail        %{mail}e
       RequestHeader set X-Shib-DisplayName %{displayName}e
   </Location>
   ```
   Those are the header names `current_actor()` already looks for.

6. **Leave the public page unauthenticated.** `/public/*` must stay open — the
   whole point is that anyone can check stock without logging in. Exclude it
   from the protected location.

### Code changes required

Genuinely small, and mostly deletion:

- `deps.py::current_actor()` — drop the cookie fallback once SSO is live, so
  identity cannot be spoofed by clearing a cookie.
- `deps.py::require_actor()` — a missing session becomes a 401 rather than a
  redirect to `/whoami`.
- Delete the `/whoami` route and template.
- `base.html` — replace the "change" link with a Shibboleth logout link.

### Roles, to add at the same time

Authentication answers *who*; the stockroom will immediately want *what they
may do*. Suggested minimum, driven off `eduPersonAffiliation` plus a staff
allow-list in the `person` table:

| Role | May |
|---|---|
| **Viewer** (any RIT login) | see inventory, availability and their own loans |
| **Staff** | everything: check in/out for others, edit items, import |

This needs a `person.role` column and a dependency that gates the mutating
routes. It is the one piece of real work beyond configuration.

## Alternative: SAML inside the app

`python3-saml` or `pysaml2` can speak SAML directly from FastAPI, avoiding the
Apache/nginx layer. **Not recommended**: it puts certificate rotation,
signature validation and replay protection into our code, and TLS termination
is wanted in front of the app anyway.

## Deliberately out of scope

- **Authorising borrowers.** The person a loan is *tagged to* is still just a
  name and email, and should stay that way — the stockroom lends things to
  visitors and collaborators who have no RIT login.
- **Login for the public page.** It stays open.
