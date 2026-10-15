# ADR 0024: Staff sign in with e-mail, password and a code sent by e-mail

**Status:** accepted (2026-10-15). The owner asked for the team to sign in "con un usuario y contraseña y que se haga una doble autenticación con el correo de la persona", and chose this option over "Sign in with Google" (ADR 0018, single sign-on). The design is mine, under the owner's authority. It is off in production until the owner chooses an e-mail provider (see Consequences).

## Context

Until now a person entered the staff app with a personal key (`sk_...`, 43 random characters), copied from a private file. It is strong, but it is hard to use: the owner had to be guided through copying it from Notepad, and a key is a single factor that, once copied somewhere, is all an intruder needs. Single sign-on (ADR 0018) is built on the server but needs an identity provider the practice does not have.

The free instance has 512 MB and 0.1 CPU (ADR 0022), and the rehearsal's memory budget had 9 MB of room on 2026-10-15.

## Decision

1. **E-mail, password, then a code.** `POST /v1/auth/sign-in` checks the e-mail and the password and sends a 6-digit code by e-mail; `POST /v1/auth/code` exchanges the code for a session key. The code is valid 10 minutes, allows 5 tries, and can be resent once a minute, 5 times. It is stored as an HMAC under a key derived from `PSEUDONYM_KEY`, bound to its sign-in.
2. **Sessions are keys with an end.** The session key (`ss_...`) travels in `X-API-Key` like any key and maps to the same staff principal, so roles, need to know and the audit trail are unchanged. It lasts 12 hours, or 30 days when the person asks to be remembered on that computer (`STAFF_SESSION_HOURS`, `STAFF_SESSION_REMEMBER_DAYS`). Only its SHA-256 is stored (`staff_sessions`). It ends when the person signs out, when a new password is chosen, when the administration removes the person, or with "sign out everywhere".
3. **Passwords** are hashed with scrypt, N=2^13, r=8, p=10: the 8 MiB profile of OWASP's Password Storage Cheat Sheet (memory-hard like its larger profiles, sized for the free instance), a random salt each, compared in constant time, computed off the event loop. An unknown e-mail costs the same work as a wrong password. Rules: 10 to 200 characters, not a very common one, not containing the e-mail's name.
4. **Nobody chooses another person's password.** The administration adds a person in Ajustes → Equipo (name, e-mail, profiles); the person receives a link valid 72 hours to choose a password. "I forgot my password" sends a link valid 30 minutes. A person with a key account can add an e-mail login to it (`POST /v1/session/email`). Links carry a random token whose hash is stored; the app moves it out of the address bar as soon as it reads it.
5. **Nothing tells whether an e-mail exists.** A wrong e-mail and a wrong password answer the same 401; "I forgot my password" answers the same 202 for any address.
6. **Limits.** 5 wrong passwords lock that login for 15 minutes; every public sign-in route is limited per client address (`AUTH_RATE_PER_MINUTE`, 10).
7. **The key stays** as break-glass access for the owner ("Entrar con llave de emergencia").
8. **E-mail.** `mailer.py` sends plain text over SMTP with STARTTLS. Without `SMTP_HOST`, production reports e-mail sign-in as unavailable (`GET /v1/auth/options`) and the app shows the key; outside production the e-mails are kept for tests and, with `DEV_OUTBOX_DIR`, written as files (the local practice uses them). The e-mails carry the code or the link and the person's name, never health data.
9. **Audit**, by principal id and never with a password, code or key: `auth.code.sent`, `auth.code.wrong`, `auth.password.wrong`, `auth.sign_in.locked`, `auth.session.opened`, `auth.password.set`, `auth.reset.sent`, `auth.invitation.sent`, `auth.login.created`.

## Consequences

- A second factor that is not a key: someone who learns a password still needs the person's mailbox. It is weaker than an authenticator app or a security key (whoever controls the mailbox controls the second factor), which is why the team's mailboxes should have their own two-step verification. An authenticator-app factor can be added later on the same tables.
- E-mail addresses are unique across the deployment: signing in names no practice. The administration of one practice adding an address used in another gets "e-mail already in use", which tells that the address signs in somewhere on this deployment; today the deployment serves one practice.
- `staff_sessions` and `login_challenges` are found by a secret before the tenant is known, like `principals.key_hash`; every row carries its tenant (tenant-isolation matrix, `GLOBAL_KEYS`).
- **Before it is on in production** the owner chooses an e-mail provider (the practice's own mailbox over SMTP, or a sending service). Its contract is a data processing agreement like the others (it sees staff names and addresses), and `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD` and `SMTP_FROM` go into Render. Until then production keeps the key sign-in exactly as before.
- Memory: one scrypt computation holds 8 MiB for about a second at 0.1 CPU; the rehearsal does not sign in with e-mail, and sign-ins are rare.
