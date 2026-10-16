# ADR 0024: Staff sign in with e-mail, password and an authenticator app

**Status:** accepted (2026-10-15). The owner asked for the team to sign in "con un usuario y contraseña y que se haga una doble autenticación con el correo de la persona". The first version sent the code by e-mail; the practice had no way to send e-mail (Gmail would not create an app password for a new account), so the owner chose the second factor of an authenticator app instead ("A"), over a paid-or-contracted sending service. "Sign in with Google" (ADR 0018) was offered and not chosen. The design is mine, under the owner's authority.

## Context

Until now a person entered the staff app with a personal key (`sk_...`, 43 random characters), copied from a private file. It is strong but hard to use (the owner had to be guided through copying it from Notepad), and it is a single factor: once copied somewhere, it is all an intruder needs. Single sign-on (ADR 0018) is built on the server but needs an identity provider the practice does not have.

The free instance has 512 MB and 0.1 CPU (ADR 0022), and the rehearsal's memory budget had 9 MB of room on 2026-10-15.

## Decision

1. **E-mail and password, then a code from an authenticator app.** `POST /v1/auth/sign-in` checks the e-mail and the password and opens a sign-in valid 10 minutes; `POST /v1/auth/code` takes the app's 6-digit code (5 tries) and answers a session key. The code is TOTP (RFC 6238: HMAC-SHA1, 30-second steps), what Google Authenticator, Microsoft Authenticator and the like compute; a code is accepted for the current step or one either side, and each step only once (a replayed code is refused). The app's secret is sealed at rest (`sealing.py`, AES-256-GCM, key derived from `PSEUDONYM_KEY` with its own purpose label, the tenant as associated data).
2. **Sessions are keys with an end.** The session key (`ss_...`) travels in `X-API-Key` like any key and maps to the same staff principal, so roles, need to know and the audit trail are unchanged. It lasts 12 hours, or 30 days when the person asks to be remembered on that computer (`STAFF_SESSION_HOURS`, `STAFF_SESSION_REMEMBER_DAYS`). Only its SHA-256 is stored (`staff_sessions`). It ends when the person signs out, when a new link is used, when the administration removes the person, or with "sign out everywhere".
3. **A personal link, sent by WhatsApp.** The administration adds a person in Ajustes → Equipo (name, e-mail, profiles explained in plain words) and gets a link valid 72 hours to hand over, like the patients' links. The link leads to two steps: choose a password (`POST /v1/auth/link`), then add the practice to the app by QR code, by a link that opens the app on the phone, or by typing the key, and confirm with its first code (`POST /v1/auth/enroll`), which also signs the person in. A person with a key account can make their own link from Tu cuenta (`POST /v1/session/email`). Links carry a random token whose hash is stored; the app moves it out of the address bar as soon as it reads it.
4. **The password and the authenticator are saved together**, only when the first code confirms the link: knowing a password never lets someone add their own app, and an abandoned link changes nothing.
5. **One remedy for everything.** A forgotten password or a lost phone is solved with a new link from Equipo ("Enlace de acceso"): using it sets a new password and a new authenticator and ends every session; until it is used, the old ones keep working, so making a link locks nobody out.
6. **Passwords** are hashed with scrypt, N=2^13, r=8, p=10: the 8 MiB profile of OWASP's Password Storage Cheat Sheet (memory-hard like its larger profiles, sized for the free instance), a random salt each, compared in constant time, computed off the event loop. An unknown e-mail costs the same work as a wrong password. Rules: 10 to 200 characters, not a very common one, not containing the e-mail's name.
7. **Nothing tells whether an e-mail exists**: a wrong e-mail and a wrong password answer the same 401.
8. **Limits.** 5 wrong passwords lock that login for 15 minutes; 5 wrong codes end a sign-in or an enrolment; every public route is limited per client address (`AUTH_RATE_PER_MINUTE`, 10).
9. **The key stays** as break-glass access for the owner ("Entrar con llave de emergencia").
10. **Audit**, by principal id and never with a password, code, secret or key: `auth.login.created`, `auth.link.created`, `auth.totp.enrolled`, `auth.totp.wrong`, `auth.password.wrong`, `auth.sign_in.locked`, `auth.sign_in.started`, `auth.session.opened`.
11. **Retention.** Links, enrolments and sign-ins, and ended sessions, are deleted a day after they stopped working (`agency retention`).

## Consequences

- A real second factor with no provider, no cost and no new data processor: the secret is generated on our server and lives sealed in our database and in the person's app.
- Phishing-resistant it is not (a fake page could relay a code within 30 seconds); a security key (WebAuthn) would be. It can be added later on the same tables.
- The administration hands over the links, so it could use one itself; it is trusted with the team's access anyway (it creates and removes people), and every link and enrolment is audited.
- E-mail addresses are unique across the deployment: signing in names no practice. The administration of one practice adding an address used in another gets "e-mail already in use"; today the deployment serves one practice.
- `staff_sessions` and `login_challenges` are found by a secret before the tenant is known, like `principals.key_hash`; every row carries its tenant (tenant-isolation matrix, `GLOBAL_KEYS`).
- QR codes are made by `segno` (BSD-3-Clause, pure Python, no dependencies) on the server and drawn by the app as SVG, under the strict CSP.
- Memory: one scrypt computation holds 8 MiB for about a second at 0.1 CPU; the rehearsal does not sign in with e-mail, and sign-ins are rare.
