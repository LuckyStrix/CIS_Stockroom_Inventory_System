# Registering this service provider with RIT ITS

Everything the ITS ticket asks for, in one place. The ticket is
[**Configure New use of RIT Login for an application**][form] in the RIT
Service Center; RIT's [SSO — Deploying][deploy] page and the
[SAML Cookbook][cookbook] say what they expect of us first.

The form's own words: *"Before submitting this request, you must have a SAML
service provider configured."* Its fields are, in order — metadata location,
application/server administrator, description, desired attributes, how the
attributes will be used, whether they will be stored, whether the server is
compliant with the RIT ISO server security standard, and a desired due date.
Sections 1–5 below answer them in that order.

**Do this first.** It is a request to another team, it is the long pole, and
no code change substitutes for it. The application ships with
`STOCKROOM_AUTH_MODE="password"` and stays that way until this comes back.

[deploy]: https://shibboleth.main.ad.rit.edu/ITSOperations/SSO---Deploying_22252854.html
[cookbook]: https://shibboleth.main.ad.rit.edu/docs/saml-cookbook/
[form]: https://help.rit.edu/sp?id=sc_cat_item&sys_id=ab6aeaf31be2c0505d6afeeccd4bcb2a&sysparm_category=4d715cbb1b0ac0d07cc34377cc4bcba3

## Before you open the ticket

Run this on the Pi, with `STOCKROOM_SSO_BASE_URL` set in `/etc/stockroom.env`:

```bash
sudo -u stockroom stockroom sso init      # keypair + cached RIT metadata
sudo -u stockroom stockroom sso metadata  # the XML to attach
```

**Who owns this.** RIT's [Web Security Standard][webstd] §2.1 requires the
department, owner, developer and administrator to be registered centrally and
refreshed annually, and says the owner, developer and administrator **shall be
an RIT employee**. A student employee can reasonably be listed as developer
and administrator; the **owner should be a CIS faculty or staff member**
regardless, because students graduate and the registration does not. Get your
supervisor to open the ticket or be named on it — the storage and compliance
answers below are departmental commitments, not one person's.

[webstd]: https://www.rit.edu/security/sites/rit.edu.security/files/Web2017r1.pdf

## 1. Service provider details

| Field | Value |
|---|---|
| entityID | `https://cisstockroom.device.rit.edu/shibboleth` |
| Assertion Consumer Service | `https://cisstockroom.device.rit.edu/sso/acs` |
| ACS binding | HTTP-POST, `index="1"` — the index in RIT's own template |
| AuthnRequestsSigned | `true` |
| Key descriptors | `signing` **and** `encryption`, the same self-signed certificate |
| Metadata URL | `https://cisstockroom.device.rit.edu/sso/metadata` |
| Single Logout | **none offered** — RIT's IdP publishes none either |
| Host | `cisstockroom.device.rit.edu` → 129.21.66.182 |
| Reachable from | the campus network only (ufw: 129.21.0.0/16 and the private ranges eduroam NATs behind) |
| Implementation | `python3-saml`, the OneLogin Python toolkit RIT document |

Attach the metadata document to the ticket as well as giving the URL. The form
says metadata "must be available at the provided URL, using HTTPS, and from
the RIT network. Otherwise attach the metadata file to this request" — and
this host is firewalled to campus, so assume they cannot fetch it.

`stockroom sso metadata` emits a document in the shape the form's template
shows: `AuthnRequestsSigned="true"`, a `signing` and an `encryption`
`KeyDescriptor`, and `AssertionConsumerService … index="1"`. Two deliberate
differences to mention if asked:

- **`WantAssertionsSigned="true"`** where their template shows `false`. We
  refuse an unsigned assertion; that is stricter than they ask for, not
  looser.
- **`NameIDFormat` is `transient`** where their template shows `unspecified`.
  Transient is one of the two formats `rit-metadata.xml` actually advertises,
  and we identify people by the `uid` attribute rather than by NameID, so
  nothing depends on which is issued.

**The encryption key is published whether or not we require encryption.** They
are separate settings here (`STOCKROOM_SSO_ENCRYPTED_ASSERTIONS` controls only
whether an unencrypted assertion is *refused*), so ITS can turn encryption on
at their end at any time without us re-registering. Signing is the opposite:
`STOCKROOM_SSO_SIGN_REQUESTS` is written into the metadata, so changing it
after registration means sending ITS a new document.

## 2. Application purpose

Inventory and checkout tracking for the RIT Carlson Center for Imaging Science
stockroom. It records what equipment the stockroom owns, who has borrowed
what, and when it came back. It runs on a Raspberry Pi on the stockroom
network and is used by stockroom staff and by CIS students borrowing
equipment.

Single sign-on replaces locally-held passwords. That is the point of the
request: the application currently stores scrypt password hashes, and would
rather not hold credentials at all.

## 3. Attributes requested, and what each is for

| Attribute | Use |
|---|---|
| `uid` | The stable account identifier. Stored as `account.sso_uid` and used as the primary match, because email addresses can be reassigned and `uid` is not. |
| `mail` | The account's email address. Also the one-time join key that links an existing password account to its RIT identity on first sign-in, so nobody re-registers. |
| `givenName` | First name, shown in the interface and in the audit log. |
| `sn` | Surname, likewise. |
| `ritEduAffiliation` | Stored for the record. **Not used for authorisation** — roles are granted by hand by a stockroom administrator. |
| `ritEduMemberOfUid` | Read but not currently used. Requested so that group-driven staff roles remain possible later without a second ticket. Say so if you would rather it were not released. |

Authorisation is **not** derived from any attribute except by human decision.
A first-time signer-in is created at the lowest role, which can only ask to
borrow something.

## 4. Storage plan for released attributes

Asked explicitly by the ticket, and this application's answer is unusual
enough to state plainly rather than let ITS discover it.

- `uid`, `mail`, `givenName` and `sn` are stored in the `account` table on the
  Pi, in SQLite, and refreshed from the assertion on each sign-in.
  `ritEduAffiliation` is stored in the same row. `ritEduMemberOfUid` is not
  stored.
- The database file is `0640`, owned by the service account, on an
  unencrypted SD card in a locked stockroom. Nightly snapshots go to the same
  card and optionally to a USB stick or an rclone remote the department
  controls.
- **The audit log is append-only and its entries are never deleted.** It is a
  SHA-256 hash chain, which is the point of the system — the stockroom needs
  to be able to answer who had what and when, years later. A sign-in writes a
  row naming the person. **So name and email are retained indefinitely and
  cannot be erased without invalidating the chain.** This is deliberate, and
  it is the answer to "storage plans for attributes".
- Nothing is sent anywhere else. There is no analytics, no email, no external
  service, and no internet exposure.

## 4a. Logging and backups, for the server checklist

The form's compliance question is about the [Server Security
Standard][serverstd], whose worked form is the [Server Security
Checklist][checklist] — 60 items with a signature block. Two groups of them
are answered by configuration rather than by argument, so they are recorded
here:

| Item | Answer |
|---|---|
| **16** — ≥2 weeks of OS/application logging, timestamped | `deploy/harden-pi.sh` sets `Storage=persistent`, `MaxRetentionSec=30day`, `SystemMaxUse=500M`. Pi OS ships the journal **volatile**; without this there was no retention at all. |
| **18** — logging mirrored in real time to another secure server | **Not met.** The nightly job exports the journal off-box to the same destination as the database, which is a night behind. Either ask ITS for a central log destination to forward to, or record this as a compensating control — do not initial it. |
| **42** — operationally critical data backed up | Nightly, verified with SQLite's integrity check before it counts. |
| **43** — documented backup/restore procedures | `docs/operations.md` §Backups and §Restore. |
| **44** — verified at least monthly | Trial restore, `docs/operations.md`. |
| **45** — not stored solely in the same building | `STOCKROOM_BACKUP_REMOTE` to RIT Google Drive. **A USB stick left in the Pi does not satisfy this** — it is the same building. |
| **55** — registered in a centralized registration system | **To do.** RIT does not publish the system or the form; ask in this ticket. |
| **7** — software no longer vendor/community supported | `python3-saml`'s last release was October 2023. The item's own remedy is "an exception request pending or granted by the ISO" — file one. |
| **24/25** — HIPS, required for authentication servers | fail2ban is enabled. In password mode this *is* an authentication server; under SSO it stops being one. |

[serverstd]: https://www.rit.edu/security/server-security
[checklist]: https://www.rit.edu/security/sites/rit.edu.security/files/documents/ServerSecurityChecklist-2019.pdf

## 5. ISO security standard compliance

Against the [Web Security Standard][webstd]:

| Clause | Status |
|---|---|
| §2 Registration | **To do as part of this ticket** — needs a faculty/staff owner of record. |
| §3 Private information | **Not applicable.** The application holds names and RIT email addresses, which are Internal under the Information Access and Protection Standard. It holds no SSN, driver's licence or financial account data, so §3.1's VP-approval requirement is not triggered. |
| §4 Vulnerability scanning | Available for ITS scanning from 129.21.0.0/16, which the firewall already permits. |
| §6 Patching | `unattended-upgrades` is enabled by `deploy/harden-pi.sh`. **See the note below on `python3-saml`.** |
| §7 Encryption | TLS 1.2+ only, forward secrecy, no legacy ciphers. Currently a self-signed certificate — **a trusted certificate for this hostname is a second thing to ask ITS for.** |
| §8 Content filtering | Server-side validation on every input; a strict CSP with no `unsafe-inline`; no inline JavaScript anywhere. |
| §9 Logging | Every change is recorded in a tamper-evident audit log. |
| §10.1 Accounts | The service runs as its own unprivileged account; SSH is key-only. |
| §10.2.1 New session ID on authentication | Yes — a fresh 256-bit token is minted on every sign-in, so a session fixed beforehand cannot be reused. |
| §10.2.2 Session IDs not in cleartext | Yes — HTTPS only, `HttpOnly`, `__Host-` prefixed, `SameSite=Strict`, and only the SHA-256 of the token is stored. |
| §11 Development | Documented conventions in `CLAUDE.md`; 800+ automated tests including a dedicated suite for this integration. |

**Disclose this.** The `python3-saml` toolkit — which RIT's own documentation
recommends for Python — last published a release in **October 2023**. Against
§6.3 that is worth naming rather than hiding. The mitigation is that the
entire toolkit is confined to one module (`src/stockroom/saml.py`) behind
plain dataclasses, so replacing it with the actively-released `pysaml2` is a
single-file change if ISO would prefer that. Ask them which they want.

## 6. Questions to ask ITS

1. **What algorithm does the IdP sign assertions with?** This is the one that
   can stop the migration dead. `rit-metadata.xml` carries a signing
   certificate issued in **2008-11-25** and the entity still advertises
   `urn:mace:shibboleth:1.0` and SAML 1.1 endpoints; an identity provider of
   that vintage signed with **RSA-SHA1**, which this application refuses by
   default and should. If ITS confirm SHA-1, `STOCKROOM_SSO_REJECT_SHA1="0"`
   gets sign-ins working while they move to SHA-256 — `stockroom doctor`
   warns for as long as it is set, because it is a stopgap and not an answer.
   Ask this **before** the migration term is planned; nothing in our test
   suite can tell us, since our fake identity provider signs SHA-256.
2. **Do you encrypt assertions to service providers that advertise a key?**
   We advertise one, so nothing needs to change either way. If they do, say
   so and `STOCKROOM_SSO_ENCRYPTED_ASSERTIONS="1"` makes it a requirement
   rather than a courtesy.
3. **Are signed AuthnRequests verified, or ignored?** We sign, per their
   template. Only worth asking so that nobody is surprised later.
4. **Can we get a browser-trusted TLS certificate for
   `cisstockroom.device.rit.edu`?** It is currently self-signed. A certificate
   warning in the middle of a sign-in redirect is where users abandon, and
   `.device.rit.edu` may not be an eligible namespace — this is the question
   to settle early. The SAML signing keypair is separate and is self-signed by
   design; it does not need a certificate authority.
5. **How will we know when the IdP signing certificate rotates?** A stale
   cached copy of `rit-metadata.xml` fails every sign-in with a signature
   error. `stockroom doctor` warns at six months; a heads-up is better.
6. **Is MFA applied to this service?** ITS decide; the application requests no
   particular authentication context and will not override the policy.
7. **Expected turnaround**, so the migration term can be planned.

## 7. After ITS say yes

```bash
sudo -u stockroom stockroom sso init --refresh   # fresh RIT metadata

# Link the staff and admin accounts BEFORE anyone tries to sign in. RIT
# sign-in provisions and links requesters on its own; it refuses to adopt a
# privileged account on an email match, because addresses get reissued and a
# role that can write equipment off is not something to inherit by
# coincidence. `stockroom user list` shows who needs this; the uid is the part
# of the RIT address before the @.
sudo -u stockroom stockroom user list
sudo -u stockroom stockroom user link-sso alice@rit.edu abc1234

sudoedit /etc/stockroom.env                      # STOCKROOM_AUTH_MODE="both"
sudo systemctl restart stockroom
sudo -u stockroom stockroom sso check
sudo -u stockroom stockroom doctor               # WARN here means read it
```

Then **sign in once, in a real browser**, before telling anybody else. Two of
the things that can go wrong at this point are invisible to the test suite:
the identity provider's signature algorithm (question 1 above) and anything
to do with cookies crossing back from RIT. What "working" looks like is a
sign-in that lands you on a stockroom page already signed in — not on the
sign-in page, and not going round in circles.

Run `both` for a term so nobody is locked out, then `sso`. If anything goes
wrong, set the mode back to `password` and restart — everyone with a password
is back in immediately.
