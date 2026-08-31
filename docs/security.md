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
default and permits 22/80/443 only from the subnet given to
`deploy/harden-pi.sh --subnet`.

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
| **Lockout** | 5 failures per address in 15 minutes; a separate per-IP ceiling stops spraying. Stored in the database, so restarting the service does **not** reset it. |

There is deliberately **no way to create an administrator over the network**.
The first one is made with `stockroom user create --admin` by someone who
already has shell access to the Pi.

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
- **Headers.** `X-Content-Type-Options`, `X-Frame-Options: DENY`,
  `Referrer-Policy`, `Permissions-Policy`, `Cross-Origin-Opener-Policy`, and
  HSTS when the request actually arrived over TLS.
- **Host header.** `TrustedHostMiddleware` rejects unexpected `Host` values.
- **Redirects.** Every caller-supplied destination — a `next` parameter, a
  `Referer` — is reduced to a local path. Absolute URLs are refused.
- **The audit log.** Every change is written in the same transaction as the
  change itself, including logins, failed logins, lockouts, approvals, role
  changes and password changes.

## TLS

`deploy/setup-pi.sh` generates a self-signed certificate with the right SANs.
That **encrypts** traffic, which is the part that matters once there are
passwords on the wire. It does **not authenticate the server**, so browsers
warn until it is trusted.

Two ways to fix that, in order of preference:

1. **Get a certificate from RIT ITS** for a proper hostname. This is the real
   answer, and it is the same conversation as SSO.
2. **Trust the self-signed certificate** on the handful of stockroom machines.
   Copy `/etc/ssl/stockroom/stockroom.crt` and install it as a trusted root.

Be honest about option 2's cost: teaching people to click through certificate
warnings is a habit that hurts them elsewhere. Prefer option 1.

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
4. **The SD card will fail.** Backups run nightly, but they live on the same
   card. Copy them off the machine — `docs/operations.md` says how.
5. **Someone has to own this.** Unapplied patches eight months from now, a
   reboot nobody performed, the person who set it up graduating: these are the
   realistic ways this system dies. Put a name and a monthly check in the
   stockroom's own records.

## If something goes wrong

```bash
# Who has been signing in, and who has been failing?
sudo -u stockroom /opt/stockroom/.venv/bin/stockroom history --action auth.login
sudo -u stockroom /opt/stockroom/.venv/bin/stockroom history --action auth.login_failed

# Cut off one account immediately.
sudo -u stockroom /opt/stockroom/.venv/bin/stockroom user disable someone@rit.edu

# Sign an account out everywhere without disabling it.
sudo -u stockroom /opt/stockroom/.venv/bin/stockroom sessions revoke someone@rit.edu

# Take the service off the network while you investigate. The database and
# the audit log are untouched by this.
sudo systemctl stop nginx
```

The event log is append-only and never rewritten, so the record of what
happened survives whatever you do next.
