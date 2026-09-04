# Accounts and requests

How the day-to-day workflows run, for staff and for everyone else.

## Getting an account

Anyone with an RIT address can request one at `/register`. The account is
created **pending** and cannot sign in until a staff member approves it.

Staff see waiting signups in two places: a badge on the **Accounts** tab, and
at the top of the **Requests** inbox. Approving is one click.

There is no email server on the Pi, which has two consequences worth being
plain about:

- **Approval is the only verification.** Nothing has proved the address belongs
  to the person who typed it. Approve people you recognise, or check with them.
- **Nobody is notified of anything.** Not signups, not approvals, not
  decisions on requests. Staff work the inbox; requesters check
  `/requests/mine`. Tell people that when you approve them.

### Once RIT single sign-on is turned on

Both of those change. RIT proves the address belongs to the person, so
approval stops being the verification step and becomes a decision about
access — which is why a first sign-in creates a working `requester` account
straight away unless `STOCKROOM_SSO_AUTO_APPROVE="0"` says otherwise.

Nobody re-registers — with one deliberate exception. A `requester` account is
matched by RIT's `mail` attribute on its owner's first RIT sign-in and lights
up with its history and its person record intact.

**A `staff` or `admin` account is not**, and is refused until somebody links
it by hand:

```bash
sudo -u stockroom stockroom user link-sso alice@rit.edu abc1234
```

An email match is enough to provision a new requester and not enough to
inherit a role that can write equipment off: RIT reissue addresses after
people leave, which is exactly why `uid` and not `mail` is the account's
primary key. So promoting an identity into a privileged account is a decision
a human makes from a shell on the Pi — the same reasoning as "there is
deliberately no way to create an administrator over the network".

Do the linking **before** the migration term starts, not during it. There are
only a handful of staff accounts, `stockroom user list` shows them, and the
`uid` is the part of an RIT address before the `@`.

Single sign-on is off until ITS have registered the service — see
[its-registration.md](its-registration.md). It is switched on with
`STOCKROOM_AUTH_MODE` in `/etc/stockroom.env` (`both` during a migration
term, then `sso`), and switched off again the same way if anything goes
wrong.

### The first administrator

Made from a shell on the Pi. There is deliberately no way to do it over the
network:

```bash
stockroom user create \
    --first-name Your --last-name Name --email you@rit.edu --admin
```

After that, admins promote others from the Accounts page.

### Roles

| Role | Can |
|---|---|
| **requester** | Browse the inventory, file requests, see their own loans and requests |
| **staff** | Everything operational: check in/out, edit items, approve requests and accounts |
| **admin** | Staff, plus changing roles and disabling accounts |

Everyone starts as a requester. The system refuses to demote the last
remaining administrator, so you cannot accidentally lock the room out of its
own settings.

## The three request forms

All three live under **My requests** for a requester, and land in the staff
**Requests** inbox. All three share one lifecycle:

```
pending ──approve──▶ approved ──fulfil──▶ fulfilled
    │                    │
    ├──decline──▶ declined
    └──cancel───▶ cancelled        (the requester withdrawing)
```

### 1. Borrow equipment

"I would like to take the Canon body out next Tuesday."

The requester picks an item, a quantity, and optionally the dates they need it
between. Staff approve or decline with a note.

**Approving does not move any equipment.** It means "yes, you may have this".
The loan is created separately, with the **Check out now** button, when the
person physically collects it — because that is the moment the shelf actually
changes. That checkout runs through the same code as the counter, so
availability limits and the audit trail apply exactly as normal.

A request also **does not reserve stock**. If someone else borrows the last one
first, the approved request cannot be fulfilled and staff will see why. This is
deliberate: availability must describe the shelf, not intentions about it.

### 2. Add to inventory

"The stockroom should own a second tripod."

Name, description, quantity, vendor, product link, and why it is needed. If
staff approve it and the item is eventually bought, they create the item and
link it back to the request, which closes the loop for whoever asked.

### 3. Open the stockroom

"Could someone be there Thursday afternoon so I can return this?"

A proposed window and whether they need to borrow, return, or both. Approving
this one **publishes a confirmed slot immediately** — it appears on the public
inventory page, so the answer to "when can I come and collect this?" is
available without logging in.

Staff can also publish open hours directly, from the bottom of the Requests
page, without anyone having asked.

## Staff routine

Roughly, once a day:

1. Open **Requests**.
2. Clear any pending account approvals at the top.
3. Work down the pending requests — approve, decline with a reason, or ask.
4. Check **Checked out** for anything overdue.
5. Hand over anything approved that has been collected, using **Check out now**.

Every one of those actions is recorded against your name in **History**.

## Command line

Useful when the browser is inconvenient, or from a script:

```bash
stockroom user list                       # everyone, pending first
stockroom user list --status pending      # just the queue
stockroom user approve an1234@rit.edu
stockroom user role an1234@rit.edu staff
stockroom user disable an1234@rit.edu     # --enable to reverse
stockroom user passwd an1234@rit.edu      # prompts; revokes their sessions
stockroom sessions revoke an1234@rit.edu  # sign out everywhere, keep the account
```

On the Pi that is `/usr/local/bin/stockroom`, which runs the real CLI as the
service account — see [operations.md](operations.md#the-command-line). Add
`--actor "Your Name <you@rit.edu>"`, or the audit log records the change
against the service account instead of you.

## Things people ask

**Someone forgot their password.** There is no self-service reset — that needs
email. Reset it for them with `stockroom user passwd`, which also signs them
out everywhere. Hand them the new password in person.

**Someone left.** `stockroom user disable`. Their sessions stop working
immediately and their history stays intact. Do not delete anything; the record
of what they borrowed is the point of the system.

**Can a non-RIT visitor borrow something?** Yes. They cannot have an *account*
— signup requires an `@rit.edu` address — but staff can lend to anyone by
typing a name and email at checkout. Accounts are for logging in; people are
for holding equipment, and the two are deliberately separate.

**Can I approve my own request?** If you are staff, technically yes, and the
audit log will show you did both. That is a policy question for the stockroom,
not something the software forbids.
