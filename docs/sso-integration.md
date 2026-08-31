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

Real accounts, held in this application: RIT email plus a password hashed with
scrypt, server-side revocable sessions, staff approval before a new account can
sign in, and role-based authorisation. See [security.md](security.md).

That is a genuine authentication system, and it is deliberately **interim**.
Its weaknesses are the ones SSO exists to remove:

- we are storing passwords, which is a liability we would rather not carry;
- nothing verifies that an address belongs to the person who typed it — staff
  approval is the only check;
- there is no password reset, because there is no mail server;
- it is one more credential for people to manage.

## Why the change is still small

All identity logic is in one function:

```
src/stockroom/web/deps.py :: current_account()
```

Everything downstream — every route, all five service modules, the audit log —
receives an `Account` (or the `Actor` derived from it) and never asks where it
came from. SSO replaces the body of that function and nothing else.

> **Note for whoever does this work.** An earlier revision had
> `current_actor()` read `X-Shib-*` request headers and prefer them over the
> session. That was **removed on purpose**, and it should not be restored
> casually: nginx currently passes client headers through, so trusting them
> would let anyone on the LAN impersonate any user by setting a header. The
> header path is only safe once the SP is actually in front of the app **and**
> nginx explicitly clears those headers from client requests. Do both in the
> same change, or neither.

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

       # Clear anything the client sent under these names FIRST. Without
       # these two lines a user can simply set X-Shib-Mail themselves and
       # become whoever they like -- this is the whole security of the
       # header approach.
       RequestHeader unset X-Shib-Mail
       RequestHeader unset X-Shib-DisplayName

       RequestHeader set X-Shib-Mail        %{mail}e
       RequestHeader set X-Shib-DisplayName %{displayName}e
   </Location>
   ```
   The nginx equivalent, in `deploy/nginx-stockroom.conf`, is to set those
   headers explicitly in the `location` block (an unset `proxy_set_header`
   value clears the header) so a client-supplied value can never pass through.

6. **Leave the public page unauthenticated.** `/public/*` must stay open — the
   whole point is that anyone can check stock without logging in. Exclude it
   from the protected location.

### Code changes required

Genuinely small, because the hard parts — roles, sessions, the audit trail —
already exist:

- `deps.py::current_account()` — resolve the account from the SP headers
  instead of the session cookie, matching on `account.email`.
- `accounts.py` — auto-provision an account on first SSO login, at role
  `requester`. Approval is no longer needed for identity (the IdP has proved
  it), though the stockroom may still want it for access.
- Delete `login`, `register`, the password column and
  `security.hash_password` / `verify_password`. **This is the win**: the
  password liability goes away entirely.
- `base.html` — point "Sign out" at the Shibboleth logout URL.
- `nginx-stockroom.conf` — clear client-supplied `X-Shib-*` headers, as above.

### Roles carry over unchanged

`requester` / `staff` / `admin` already exist and already gate every route.
The only decision is how someone becomes staff: keep it manual (an admin
promotes them, which is what happens now) or drive it from
`eduPersonAffiliation`. Manual is probably right — "employee" is a much larger
set than "works in this stockroom".

### Migrating existing accounts

Accounts are keyed by RIT email, and so is the Shibboleth `mail` attribute, so
existing accounts light up on first SSO login with their roles and history
intact. Keep password login working for one term alongside SSO, then drop it.

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
