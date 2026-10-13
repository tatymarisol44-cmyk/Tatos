"""Create or update the Render service of the free managed profile (ADR 0022) from one
private file the owner fills in, after checking that every managed service answers.

    python deploy/render/setup.py C:\\Users\\<you>\\agency-render.env [--check-only]

The file has KEY=value lines (docs/DEPLOY-RENDER.md, "Automatic setup"):

    RENDER_API_KEY       Render -> Account Settings -> API Keys
    SUPABASE_POOLER_URL  Supabase -> Connect -> Session pooler, as shown ([YOUR-PASSWORD] too)
    SUPABASE_PASSWORD    the database password
    UPSTASH_ENDPOINT     <name>.upstash.io
    UPSTASH_PASSWORD     the database's password
    QDRANT_URL           the cluster endpoint, https://<id>.<region>.aws.cloud.qdrant.io
    QDRANT_API_KEY       a key of that cluster
    PRACTICE_ID          letters, digits, _ . - (e.g. consultorio)
    PRACTICE_NAME        the practice's name, as patients see it
    SERVICE_KEY          left empty: generated once, written back here, then kept

Nothing secret is printed. The environment comes from render.yaml (the Blueprint), so the
same rules apply whichever way the service is created. Re-running updates the service and
keeps the secrets Render generated (PSEUDONYM_KEY must never change: pseudonyms depend on
it)."""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
import yaml

HERE = Path(__file__).resolve().parent
BLUEPRINT = HERE / "render.yaml"
RENDER_API = "https://api.render.com/v1"
# Supabase's pooler presents a chain from Supabase's own root, not a public CA, so the
# server is verified against that root (sslmode=verify-full): the image ships it
# (Dockerfile, deploy/certs); the checks from the owner's computer use the repo's copy.
# Fingerprint and provenance: deploy/certs/README.md.
IMAGE_SUPABASE_CA = "/app/certs/supabase-root-2021.crt"
LOCAL_SUPABASE_CA = HERE.parent / "certs" / "supabase-root-2021.crt"
REQUIRED = (
    "RENDER_API_KEY",
    "SUPABASE_POOLER_URL",
    "SUPABASE_PASSWORD",
    "UPSTASH_ENDPOINT",
    "UPSTASH_PASSWORD",
    "QDRANT_URL",
    "QDRANT_API_KEY",
    "PRACTICE_ID",
    "PRACTICE_NAME",
)
SECRET_KEYS = (
    "RENDER_API_KEY",
    "SUPABASE_PASSWORD",
    "UPSTASH_PASSWORD",
    "QDRANT_API_KEY",
    "SERVICE_KEY",
)
_PRACTICE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


class SetupError(Exception):
    """A value the owner has to correct; the message never contains a secret."""


# --- the private file --------------------------------------------------------------------


def read_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    missing = [k for k in REQUIRED if not values.get(k)]
    if missing:
        raise SetupError(f"{path.name}: fill in {', '.join(missing)}")
    if not _PRACTICE.fullmatch(values["PRACTICE_ID"]):
        raise SetupError("PRACTICE_ID: use only letters, digits, _ . - (e.g. consultorio)")
    return values


def keep_service_key(path: Path, values: dict[str, str]) -> bool:
    """Generate the practice's service key once and write it back. True if new."""
    if values.get("SERVICE_KEY"):
        return False
    values["SERVICE_KEY"] = f"svc_{secrets.token_hex(24)}"
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    lines = [ln for ln in lines if not ln.strip().startswith("SERVICE_KEY=")]
    lines.append(f"SERVICE_KEY={values['SERVICE_KEY']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


# --- the managed services' addresses -------------------------------------------------------


# Parsed by hand: the "[YOUR-PASSWORD]" placeholder Supabase shows reads as an IPv6 literal
# to urlsplit, and a real password may hold any character. The user info ends at the last @.
_POSTGRES = re.compile(
    r"(?:postgres|postgresql)://(?P<user>[^:@/]+)(?::.*)?@(?P<host>[^@/?#:]+)"
    r"(?::(?P<port>\d+))?(?P<db>/[^?#]*)?(?:[?#].*)?"
)


def database_urls(
    pooler_url: str, password: str, root_ca: str = IMAGE_SUPABASE_CA
) -> tuple[str, str]:
    """(DATABASE_URL, POSTGRES_URL) from Supabase's session-pooler string, verifying the
    server's name and chain against `root_ca`."""
    m = _POSTGRES.fullmatch(pooler_url.strip())
    if m is None:
        raise SetupError("SUPABASE_POOLER_URL: copy the string that starts with postgresql://")
    user, host = m["user"], m["host"]
    if m["port"] != "5432" or not host.endswith("pooler.supabase.com"):
        raise SetupError(
            "SUPABASE_POOLER_URL: use Connect -> Session pooler (a *.pooler.supabase.com "
            "host on port 5432); the transaction pooler (6543) and the direct connection "
            "(IPv6 only) do not work from Render"
        )
    if not user.startswith("postgres."):
        raise SetupError("SUPABASE_POOLER_URL: the user must look like postgres.<project ref>")
    tail = f"{quote(user, safe='.')}:{quote(password, safe='')}@{host}"
    tls = f"sslmode=verify-full&sslrootcert={quote(root_ca, safe='/')}"
    path = f"{tail}:5432{m['db'] or '/postgres'}?{tls}"
    return f"postgresql+psycopg://{path}", f"postgresql://{path}"


def redis_url(endpoint: str, password: str) -> str:
    host = re.sub(r"^\w+://", "", endpoint).split("@")[-1].split("/")[0].split(":")[0]
    if not host.endswith(".upstash.io"):
        raise SetupError("UPSTASH_ENDPOINT: the endpoint looks like <name>.upstash.io")
    return f"rediss://default:{quote(password, safe='')}@{host}:6379"


def qdrant_url(url: str) -> str:
    parts = urlsplit(url if "://" in url else f"https://{url}")
    if parts.scheme != "https" or not parts.hostname:
        raise SetupError("QDRANT_URL: the cluster endpoint, starting with https://")
    return f"https://{parts.hostname}:{parts.port or 6333}"


def owner_values(values: Mapping[str, str], public_url: str) -> dict[str, str]:
    """The Blueprint's `sync: false` variables, built from the private file."""
    database, postgres = database_urls(values["SUPABASE_POOLER_URL"], values["SUPABASE_PASSWORD"])
    practice = values["PRACTICE_ID"]
    return {
        "DATABASE_URL": database,
        "POSTGRES_URL": postgres,
        "REDIS_URL": redis_url(values["UPSTASH_ENDPOINT"], values["UPSTASH_PASSWORD"]),
        "QDRANT_URL": qdrant_url(values["QDRANT_URL"]),
        "QDRANT_API_KEY": values["QDRANT_API_KEY"],
        "API_KEYS": f"{values['SERVICE_KEY']}:{practice}",
        "TENANT_PACKS": json.dumps({practice: "ec-psychologist"}),
        "PRACTICE_DISPLAY_NAME": values["PRACTICE_NAME"],
        "PUBLIC_BASE_URL": public_url,
        "LLM_BACKEND": values.get("LLM_BACKEND") or "fake",
    }


def blueprint() -> dict[str, Any]:
    service: dict[str, Any] = yaml.safe_load(BLUEPRINT.read_text(encoding="utf-8"))["services"][0]
    return service


def env_vars(service: Mapping[str, Any], owner: Mapping[str, str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for var in service["envVars"]:
        key = var["key"]
        if "value" in var:
            out.append({"key": key, "value": str(var["value"])})
        elif var.get("generateValue"):
            out.append({"key": key, "generateValue": True})
        elif key in owner:
            out.append({"key": key, "value": owner[key]})
        else:
            raise SetupError(f"render.yaml asks for {key}, which setup.py does not build")
    return out


# --- checks --------------------------------------------------------------------------------


def _scrub(text: str, values: Mapping[str, str]) -> str:
    for key in SECRET_KEYS:
        secret = values.get(key)
        if secret:
            text = text.replace(secret, "***").replace(quote(secret, safe=""), "***")
    return text


def check_postgres(owner: Mapping[str, str]) -> str:
    import psycopg

    with psycopg.connect(owner["POSTGRES_URL"], connect_timeout=15) as conn:
        row = conn.execute("SELECT current_setting('server_version')").fetchone()
    return f"Postgres {row[0] if row else '?'} over TLS, server verified"


def check_redis(owner: Mapping[str, str]) -> str:
    import redis

    client = redis.Redis.from_url(owner["REDIS_URL"], socket_timeout=15)
    try:
        client.ping()
    finally:
        client.close()
    return "Redis answers over verified TLS"


def check_qdrant(owner: Mapping[str, str]) -> str:
    r = httpx.get(
        f"{owner['QDRANT_URL']}/collections",
        headers={"api-key": owner["QDRANT_API_KEY"]},
        timeout=15,
    )
    r.raise_for_status()
    return "Qdrant answers with the API key"


CHECKS: dict[str, Callable[[Mapping[str, str]], str]] = {
    "Supabase": check_postgres,
    "Upstash": check_redis,
    "Qdrant Cloud": check_qdrant,
}


def run_checks(owner: Mapping[str, str], values: Mapping[str, str]) -> bool:
    ok = True
    for name, check in CHECKS.items():
        try:
            print(f"  ok    {name}: {check(owner)}")
        except Exception as exc:  # report every service, then stop
            ok = False
            print(f"  FAIL  {name}: {_scrub(f'{type(exc).__name__}: {exc}', values)}")
    return ok


# --- Render --------------------------------------------------------------------------------


class Render:
    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        self.http = client or httpx.Client(base_url=RENDER_API, timeout=30)
        self.http.headers.update(
            {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        )

    def _json(self, method: str, url: str, **kw: Any) -> Any:
        r = self.http.request(method, url, **kw)
        if r.status_code == 401:
            raise SetupError("RENDER_API_KEY was refused: create a new one in Account Settings")
        if r.is_error:
            raise SetupError(f"Render answered {r.status_code} to {method} {url}: {r.text[:300]}")
        return r.json() if r.content else None

    def owner_id(self, wanted: str | None = None) -> str:
        owners = [o["owner"] for o in self._json("GET", "/owners", params={"limit": 20})]
        if wanted:
            return wanted
        if len(owners) != 1:
            names = ", ".join(f"{o['name']} ({o['id']})" for o in owners)
            raise SetupError(f"several Render workspaces ({names}): set RENDER_OWNER_ID")
        return str(owners[0]["id"])

    def find(self, name: str) -> dict[str, Any] | None:
        found = self._json("GET", "/services", params={"name": name, "limit": 20})
        return next((s["service"] for s in found if s["service"]["name"] == name), None)

    def create(self, owner: str, service: Mapping[str, Any], env: list[dict[str, Any]]) -> Any:
        body = {
            "type": "web_service",
            "name": service["name"],
            "ownerId": owner,
            "image": {"ownerId": owner, "imagePath": service["image"]["url"]},
            "envVars": env,
            "serviceDetails": {
                "runtime": "image",
                "plan": service["plan"],
                "region": service["region"],
                "healthCheckPath": service["healthCheckPath"],
                "numInstances": 1,
            },
        }
        return self._json("POST", "/services", json=body)

    def current_env(self, service_id: str) -> dict[str, str]:
        rows = self._json("GET", f"/services/{service_id}/env-vars", params={"limit": 100})
        return {r["envVar"]["key"]: r["envVar"]["value"] for r in rows}

    def replace_env(self, service_id: str, env: list[dict[str, Any]]) -> None:
        """Replace every variable, keeping the generated secrets already there."""
        current = self.current_env(service_id)
        body = []
        for var in env:
            if var.get("generateValue") and current.get(var["key"]):
                body.append({"key": var["key"], "value": current[var["key"]]})
            else:
                body.append(var)
        self._json("PUT", f"/services/{service_id}/env-vars", json=body)

    def set_env(self, service_id: str, key: str, value: str) -> None:
        self._json("PUT", f"/services/{service_id}/env-vars/{key}", json={"value": value})

    def deploy(self, service_id: str) -> str:
        made = self._json("POST", f"/services/{service_id}/deploys", json={})
        if made:  # 201: this deploy
            return str(made["id"])
        # 202 with no body: queued behind a deploy in progress (the first one, right after
        # creation). The newest deploy is the one that carries the latest variables.
        rows = self._json("GET", f"/services/{service_id}/deploys", params={"limit": 1})
        return str(rows[0]["deploy"]["id"])

    def wait_live(self, service_id: str, deploy_id: str, timeout: float, poll: float) -> None:
        deadline = time.monotonic() + timeout
        status = "?"
        while time.monotonic() < deadline:
            status = self._json("GET", f"/services/{service_id}/deploys/{deploy_id}")["status"]
            if status == "live":
                return
            if status.endswith("_failed") or status in ("canceled", "deactivated"):
                raise SetupError(f"the deploy ended '{status}': see the service's logs in Render")
            time.sleep(poll)
        raise SetupError(f"the deploy is still '{status}': check it in Render's dashboard")


# --- main ----------------------------------------------------------------------------------


def main(argv: list[str] | None = None, render: Render | None = None, poll: float = 15) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("secrets", type=Path, help="the private file with the values")
    parser.add_argument("--check-only", action="store_true", help="check, change nothing")
    args = parser.parse_args(argv)
    values: dict[str, str] = {}
    try:
        values = read_values(args.secrets)
        if keep_service_key(args.secrets, values):
            print(f"Practice service key generated and saved in {args.secrets} (SERVICE_KEY).")
        service = blueprint()
        public_url = f"https://{service['name']}.onrender.com"
        owner = owner_values(values, public_url)
        env = env_vars(service, owner)
        print("Managed services:")
        # From this computer the root is the repo's copy; in the image, IMAGE_SUPABASE_CA.
        _, local_postgres = database_urls(
            values["SUPABASE_POOLER_URL"], values["SUPABASE_PASSWORD"], str(LOCAL_SUPABASE_CA)
        )
        if not run_checks({**owner, "POSTGRES_URL": local_postgres}, values):
            print("Fix the values marked FAIL in the file, then run this again.")
            return 1
        if args.check_only:
            print("All three answer. Nothing was changed (--check-only).")
            return 0

        render = render or Render(values["RENDER_API_KEY"])
        existing = render.find(service["name"])
        if existing is None:
            created = render.create(render.owner_id(values.get("RENDER_OWNER_ID")), service, env)
            svc, deploy_id = created["service"], created["deployId"]
            print(f"Render service created: {svc['id']}")
            url = _url(svc, public_url)
            if url != public_url:  # Render added a suffix: links must use the real address
                render.set_env(svc["id"], "PUBLIC_BASE_URL", url)
                deploy_id = render.deploy(svc["id"])
        else:
            svc = existing
            url = _url(svc, public_url)
            env = env_vars(service, {**owner, "PUBLIC_BASE_URL": url})
            render.replace_env(svc["id"], env)
            deploy_id = render.deploy(svc["id"])
            print(f"Render service updated: {svc['id']}")
        print("Waiting for the release to go live (the first start migrates the database)...")
        render.wait_live(svc["id"], deploy_id, timeout=1800, poll=poll)
        ready = httpx.get(f"{url}/readyz", timeout=60)
        ready.raise_for_status()
        print(f"Live: {url}")
        print(f"Console: {url}/ (sign in with SERVICE_KEY from {args.secrets})")
        print(f"UptimeRobot: HTTP(s) monitor on {url}/healthz every 5 minutes.")
        print(f"Continuous delivery: RENDER_SERVICE_ID={svc['id']} RENDER_PUBLIC_URL={url}")
        return 0
    except SetupError as exc:
        print(f"setup: {_scrub(str(exc), values)}", file=sys.stderr)
        return 2
    except httpx.HTTPError as exc:
        print(f"setup: {_scrub(f'{type(exc).__name__}: {exc}', values)}", file=sys.stderr)
        return 1


def _url(service: Mapping[str, Any], default: str) -> str:
    return str(service.get("serviceDetails", {}).get("url") or default)


if __name__ == "__main__":
    sys.exit(main())
