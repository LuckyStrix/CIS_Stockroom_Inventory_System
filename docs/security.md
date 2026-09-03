# Security

What this system protects, how, and — just as importantly — what it does not.

## The short version

The stockroom app runs on a Raspberry Pi with **no inbound exposure to the
internet**. It is reachable from the RIT network over HTTPS, requires an
account for everything except the public inventory page, and records every
change against a named person.

It is not "risk free", and this document does not pretend otherwise. The
residual risks are listed at the bottom, and the biggest one is not technical.

## Network posture

```
internet ──✗── Pi                      no inbound. ever.

RIT LAN ──https──▶ nginx :443 ──▶ 127.0.0.1:8000 uvicorn
RIT VPN ──https──▶ (same path, for off-campus staff)

GitHub Pages ◀──push── Pi              public page, still no inbound
```

**What is guaranteed:** no port forwarding, no public DNS record, no public IP,
no UPnP. Nothing outside RIT's network can open a connection to this machine.

**What is necessarily true:** the Pi accepts connections *from the RIT network*.
Something has to listen, or nobody can fill in a form. `ufw` denies inbound by
default and permits 22/80/443 only from the ranges `deploy/harden-pi.sh` was
given — by default RIT's `129.21.0.0/16` plus `10.0.0.0/8`, `172.16.0.0/12`
and `192.168.0.0/16`.

The unit is the campus network, not a subnet, because eduroam is campus-wide:
a device is handed an address from whichever wireless VLAN it lands on, so
allowing one CIDR bars most of the people who need the stockroom. The private
ranges widen who on campus can connect without widening what the internet can
reach — RFC1918 addresses are not routable across it, so those rules can only
match traffic that arrived from this network. Narrow it with `--allow-from`,
and keep SSH tighter than 80/443 with `--ssh-from`, if your site allows it.

So the accurate claim is **no new attack surface facing the internet**, plus a
firewalled, authenticated, TLS-only service facing the campus network.

The application itself binds `127.0.0.1`. nginx is the only thing that can
reach it, which is what makes it safe for the app to trust `X-Forwarded-Proto`
and `X-Forwarded-For`.

## Authentication

| | |
|---|---|
| **Accounts** | First name, last name, RIT email, password. `@rit.edu` (and subdomains) only. |
| **Signup** | Self-service, but the account is `pending` and **cannot sign in** until staff approve it. |
| **Passwords** | `hashlib.scrypt`, memory-hard, parameters stored per hash so they can be raised later. Minimum 12 characters; common passwords and passwords built from your own name are refused. |
| **Sessions** | Server-side and revocable. A 256-bit random token in the cookie; only its SHA-256 is stored. |
| **Cookie** | `__Host-` prefixed under TLS: `HttpOnly`, `Secure`, `SameSite=Strict`, `Path=/`, no `Domain`. |
| **Timeouts** | 8 hours idle (slides), 7 days absolute (never extends). |
| **Lockout** | 5 failures per address in 15 minutes; a separate per-IP ceiling (20 in 15 minutes) stops spraying. Stored in the database, so restarting the service does **not** reset it. |

One caveat on that per-IP ceiling: wireless clients that reach the Pi
through NAT share a source address, so they share the allowance too — an
unlucky run of typos on eduroam could in principle spend it for everyone
behind the same gateway. The per-account limit is the one doing the real
work; raise `security.MAX_FAILURES_PER_IP` if this is ever seen in
`stockroom history --action auth.login_failed`.

There is deliberately **no way to create an administrator over the network**.
The first one is made with `stockroom user create --admin` by someone who
already has shell access to the Pi.

### Single sign-on

`STOCKROOM_AUTH_MODE` selects `password` (the default), `both` or `sso`. Under
the last two, RIT's Shibboleth identity provider authenticates people and this
application never sees a password. Full detail in
[sso-integration.md](sso-integration.md); the security-relevant points:

- **An SSO sign-in produces an ordinary session.** It is not a parallel
  identity mechanism — `/sso/acs` calls `accounts.sso_login`, which writes the
  same `session` row the password path writes. Revocation, idle expiry and the
  absolute cap all work unchanged.
- **The application never reads `X-Shib-*` or `X-Remote-User`.** SAML is
  spoken in-process. nginx still blanks those headers, and
  `test_the_application_never_reads_an_identity_header` fails the build if any
  code starts reading one. On a campus-reachable host, trusting such a header
  would let anyone on the network become anyone.
- **`/sso/acs` is the one POST with no CSRF token**, because it is a
  cross-site POST from RIT. What replaces it is a signed assertion whose
  `InResponseTo` names a sign-in this server started, bound by a `HttpOnly`
  cookie to the browser that started it, single-use, and valid for five
  minutes. The cookie is the part that stops login CSRF; the signature alone
  does not, because the assertion is genuinely signed. `deps.CSRF_EXEMPT_PATHS`
  has exactly one member and a test fails if it grows.
- **That cookie is `SameSite=None`**, which is the only value that works for a
  cross-site POST, and therefore **single sign-on requires TLS**. It is worth
  very little on its own: not the session, one-time, five minutes,
  `HttpOnly`, and useless without a signed assertion from RIT.
- **An SSO account has no password.** `password_hash` is empty, and
  `accounts.login` refuses it after spending the same CPU as a real
  verification, so this does not become a way to ask who signs in with RIT.
- **Signing out cannot end the RIT session.** RIT's identity provider
  publishes no single-logout endpoint, so there is nothing to call. `/logout`
  ends the stockroom session and `/sso/signed-out` says plainly that the
  browser is still signed in to RIT. On the shared counter machine this is a
  real residual risk; the mitigations are a short
  `STOCKROOM_SESSION_IDLE_HOURS` and closing the browser.
- **SHA-1 is refused by default.** `STOCKROOM_SSO_REJECT_SHA1="0"` accepts
  assertions signed with RSA-SHA1, and exists only because RIT's identity
  provider is old enough that it might still sign that way — see
  [sso-integration.md](sso-integration.md). It is a real weakening,
  `stockroom doctor` reports WARN for as long as it is set, and it should be
  held open only until ITS move to SHA-256.
- **The escape hatch.** Set `STOCKROOM_AUTH_MODE="password"` and restart.
  Everyone with a password is back in at once. That is why the password
  machinery has not been deleted.

### On account enumeration

Login and registration are careful not to reveal who has an account:

- every failed login returns one message, whether the address is unknown, the
  password is wrong, or the account is not yet approved;
- the unknown-address path performs a dummy hash so it takes the same time as
  a real one;
- registering with an address that already exists reports the same "awaiting
  approval" result as a fresh signup;
- lockout applies to unknown addresses too, so "this one locked out" is not a
  signal either.

## Authorisation

| Role | Can |
|---|---|
| `requester` | Browse inventory, file requests, see their own requests and loans |
| `staff` | All inventory operations, check in/out, approve requests and accounts |
| `admin` | Everything, plus change roles and disable accounts |

Enforcement is **deny by default**: middleware rejects any route not on an
explicit public list (`login`, `register`, `health`, `/public/*`, `/static/*`).
Two tests walk the application's real route table and fail the build if a route
is reachable anonymously or a `POST` accepts a request without a CSRF token —
so a new page cannot be exposed by forgetting a guard.

Pages that name people — the audit log, the people directory, the loan list,
the CSV export, label sheets — are staff-only. A requester sees their own loans
and requests, and nobody else's.

## Application hardening

- **CSRF.** A synchroniser token on every unsafe method, checked in middleware
  rather than route by route. Anonymous forms (login, registration) use a
  double-submit cookie, because login CSRF is a real attack: an attacker who
  can log your browser into *their* account gets to watch what you do in it.
- **Content-Security-Policy.** `default-src 'self'` with a per-request nonce.
  No inline event handlers and no inline `style` attributes anywhere — which is
  why the templates use utility classes, and why barcode SVGs use `fill=`
  attributes rather than `style="fill:…"`.

  The **generated public page is the one exception**, and it is stricter
  rather than looser. It is a static file that has to work from a USB stick
  and from GitHub Pages, where no server exists to mint a nonce, so
  `publish/render._csp_hashes` builds it a `<meta>` policy naming the SHA-256
  hashes of its own inline `<style>` and `<script>` — `default-src 'none'`,
  no `'self'`, no `unsafe-inline`. The app deliberately does not add its own
  header to `/public` responses: a browser enforces every policy it receives
  and takes the intersection, so the nonce policy and the hash policy together
  allowed nothing at all, and the page rendered unstyled with an empty table
  wherever it was served by the application rather than by nginx.

- **Middleware order.** `HostCheck` outermost, then CSRF and the security
  headers, then the authentication gate. It is registered in one explicit
  block in `web/app.py` because `add_middleware` reverses declaration order,
  and getting it wrong is invisible: with the auth gate outermost, every
  refusal it produced — the 401 page and the redirect to `/login` — went to
  the browser with none of the headers below.
- **Headers.** `X-Content-Type-Options`, `X-Frame-Options: DENY`,
  `Referrer-Policy`, `Permissions-Policy`, `Cross-Origin-Opener-Policy`, and
  HSTS when the request actually arrived over TLS.
- **Host header.** `HostCheckMiddleware` rejects unexpected `Host` values
  (`STOCKROOM_ALLOWED_HOSTS`, defaulting to this machine's own name). Bare IP
  addresses are accepted: the app never builds an absolute URL from the Host —
  it reads only the path, query and scheme — and it sends no mail, so there is
  no link for a forged address to poison, while refusing them broke every
  device without an mDNS resolver. `STOCKROOM_ALLOW_IP_HOSTS="0"` if a future
  change introduces such a link.
- **Redirects.** Every caller-supplied destination — a `next` parameter, a
  `Referer` — is reduced to a local path. Absolute URLs are refused.
- **The audit log.** Every change is written in the same transaction as the
  change itself, including logins, failed logins, lockouts, approvals, role
  changes and password changes.

## TLS

`deploy/setup-pi.sh` generates a self-signed certificate covering every name
the Pi answers to: its fully qualified name, its short name, that name with
`.local`, and its IP address. That **encrypts** traffic, which is the part that
matters once there are passwords on the wire. It does **not authenticate the
server**, so browsers warn until it is trusted.

Two ways to fix that, in order of preference:

1. **Get a certificate from RIT ITS** for the DNS name. Install it as
   `/etc/ssl/stockroom/stockroom.crt` and `.key`, then
   `sudo systemctl reload nginx` — the paths are already what nginx reads, so
   there is no configuration change. This is the real answer, it needs no
   client-side work at all, and it is the same conversation as SSO.
2. **Trust the self-signed certificate** on the handful of stockroom machines.
   Copy `/etc/ssl/stockroom/stockroom.crt` and install it as a trusted root.

Be honest about option 2's cost: teaching people to click through certificate
warnings is a habit that hurts them elsewhere. Prefer option 1.

Let's Encrypt is not an option here and is not worth attempting: HTTP-01
requires the Pi to be reachable from the internet, which is a stated non-goal,
and DNS-01 requires write access to the `rit.edu` zone, which is ITS's.

### Two failures that look identical and are not

A browser says roughly the same thing about an untrusted issuer and about a
name the certificate does not cover, and the second is the one that wastes an
afternoon: it is **not** fixed by installing the certificate as a trusted root,
because the name is simply not in it. Which one you have:

```bash
openssl x509 -in /etc/ssl/stockroom/stockroom.crt -noout -checkhost "$(hostname -f)"
```

`does NOT match certificate` means the SANs are wrong — regenerate:

```bash
sudo rm /etc/ssl/stockroom/stockroom.crt /etc/ssl/stockroom/stockroom.key
sudo deploy/setup-pi.sh
```

Re-running the installer keeps an existing certificate, so a Pi imaged under
one name and later given a DNS record has to be told explicitly. It now warns
when the certificate it is keeping does not cover the machine's own name.

Note that `-checkhost` exits 0 either way — read the line it prints, do not
test its status.

### HSTS does nothing until the certificate is valid

The app sends `Strict-Transport-Security` on any request that arrived over TLS,
but a browser **ignores** an HSTS header received over a connection with
certificate errors (RFC 6797 §8.1). So on a self-signed deployment the header
is inert. Once a trusted certificate is in place it starts being honoured, and
that hostname is pinned to HTTPS in every browser that has seen it for a year —
which is the intent, but it does mean the host cannot be served over plain HTTP
again without waiting the `max-age` out.

## Threats this design accepts

- **A malicious authenticated insider.** Anyone with a staff account can lend
  equipment to whoever they like. The control is the audit log — every action
  is attributable — not prevention.
- **Someone on the LAN with a stolen session cookie.** Mitigated by TLS,
  `HttpOnly`, `SameSite=Strict` and short idle timeouts, and by the fact that
  sessions can be revoked (`stockroom sessions revoke <email>`).
- **A determined attacker with physical access to the Pi.** Full-disk
  encryption is not configured; an unattended headless machine cannot ask for
  a passphrase at boot. Physical security is a locked stockroom, not software.
- **Denial of service from inside the network.** Password hashing is
  deliberately expensive, so login is rate limited and locked out to keep it
  from becoming a CPU amplifier. Beyond that, someone on the LAN can make the
  Pi slow.

## Residual risks

These are real, and none of them is fixed by more code:

1. **Passwords exist at all.** They are a liability that SSO removes. This is
   an interim design; see [sso-integration.md](sso-integration.md).
2. **A self-signed certificate trains people to click through warnings.**
3. **No email verification.** Staff approval is the *only* check that an
   address belongs to the person claiming it. Approve people you recognise.
4. **The SD card will fail.** Backups run nightly and are now verified before
   they count, but by default they still live on the same card. Set
   `STOCKROOM_BACKUP_COPY_DIR` (a USB stick) or `STOCKROOM_BACKUP_REMOTE` (an
   rclone remote) so a copy actually leaves the machine; `stockroom doctor`
   warns while neither is configured.
5. **An uploaded backup is a readable copy of everything.** A snapshot sent to
   Google Drive contains every email address and the whole audit log, in the
   clear. Keep that folder private to the account that owns it and do not
   share the link. (rclone can wrap the remote in a `crypt` layer if that
   trade-off ever stops being acceptable.)
6. **The audit chain detects tampering; it does not prevent it.** Anyone who
   can write to the database file can also recompute the chain. What makes
   that expensive is that the head hash is copied outside the Pi — into
   `inventory.json`, into `/health`, and into every nightly snapshot — so a
   convincing rewrite means finding and rewriting all of those too.
7. **Someone has to own this.** Unapplied patches eight months from now, a
   reboot nobody performed, the person who set it up graduating: these are the
   realistic ways this system dies. Put a name and a monthly check in the
   stockroom's own records.

## If something goes wrong

```bash
# Who has been signing in, and who has been failing?
stockroom history --action auth.login
stockroom history --action auth.login_failed

# Cut off one account immediately.
stockroom user disable someone@rit.edu

# Sign an account out everywhere without disabling it.
stockroom sessions revoke someone@rit.edu

# Take the service off the network while you investigate. The database and
# the audit log are untouched by this.
sudo systemctl stop nginx
```

The event log is append-only and never rewritten, so the record of what
happened survives whatever you do next.
