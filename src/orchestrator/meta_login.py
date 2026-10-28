"""Meta OAuth adapter. Only fixed provider hosts receive credentials.

Browser redirects and Embedded Signup messages are not proof of asset ownership.
Assets are discovered again using the code exchanged by this server. No caller can
submit an arbitrary page, phone id, recipient, token, redirect URI or API host.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

from orchestrator.config import Settings
from orchestrator.db import utcnow

Provider = Literal["facebook", "instagram", "whatsapp"]
PROVIDERS: tuple[Provider, ...] = ("facebook", "instagram", "whatsapp")
FB_SCOPES = frozenset(
    {
        "pages_show_list",
        "pages_read_engagement",
        "pages_manage_posts",
        "pages_manage_metadata",
        "pages_messaging",
    }
)
WA_SCOPES = frozenset({"whatsapp_business_management", "whatsapp_business_messaging"})
IG_SCOPES = frozenset(
    {
        "instagram_business_basic",
        "instagram_business_content_publish",
        "instagram_business_manage_messages",
    }
)


class LoginError(ValueError):
    """A bounded, non-secret error code, suitable for storage or a response."""


@dataclass(frozen=True)
class Asset:
    network: Provider
    external_id: str
    handle: str
    token: str = field(repr=False)
    expires_at: datetime | None = None
    business_id: str | None = None


def identifier(value: Any) -> str:
    result = str(value or "")
    if not re.fullmatch(r"\d{1,128}", result):
        raise LoginError("provider_response_invalid")
    return result


class MetaLogin:
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings, self.transport = settings, transport
        self.graph = "https://graph.facebook.com/" + settings.social_login_graph_version
        self.ig = "https://graph.instagram.com/" + settings.social_login_graph_version

    def configured(self, provider: Provider) -> bool:
        s = self.settings
        if not s.social_login_enabled or not s.social_login_origin:
            return False
        if provider == "instagram":
            return bool(s.instagram_app_id and s.instagram_app_secret)
        return bool(
            s.meta_app_id
            and s.meta_app_secret
            and (s.meta_login_config_id if provider == "facebook" else s.meta_whatsapp_config_id)
        )

    def redirect_uri(self, provider: Provider) -> str:
        return self.settings.social_login_origin + f"/v1/social/login/{provider}/callback"

    def authorize_url(self, provider: Provider, state: str) -> str:
        if not self.configured(provider):
            raise LoginError("setup_required")
        if provider == "whatsapp":
            raise LoginError("embedded_signup_required")
        s = self.settings
        params = {
            "client_id": s.instagram_app_id if provider == "instagram" else s.meta_app_id,
            "redirect_uri": self.redirect_uri(provider),
            "response_type": "code",
            "state": state,
        }
        if provider == "instagram":
            params.update(
                scope=",".join(sorted(IG_SCOPES)), enable_fb_login="0", force_authentication="1"
            )
            return "https://www.instagram.com/oauth/authorize?" + urlencode(params)
        params.update(config_id=s.meta_login_config_id, scope=",".join(sorted(FB_SCOPES)))
        return f"https://www.facebook.com/{s.social_login_graph_version}/dialog/oauth?" + urlencode(
            params
        )

    async def _request(
        self,
        method: str,
        url: str,
        *,
        token: str | None = None,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": "Bearer " + token} if token else {}
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=20, follow_redirects=False
            ) as client:
                response = await client.request(
                    method, url, headers=headers, params=params, data=data
                )
        except httpx.TransportError as exc:
            raise LoginError("provider_unavailable") from exc
        if response.status_code != 200 or len(response.content) > 1_000_000:
            raise LoginError("provider_refused")
        try:
            body = response.json()
        except ValueError as exc:
            raise LoginError("provider_response_invalid") from exc
        if not isinstance(body, dict) or "error" in body:
            raise LoginError("provider_refused")
        return body

    async def _collection(self, path: str, token: str, fields: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        params: dict[str, Any] = {"fields": fields, "limit": 100}
        for _ in range(5):
            body = await self._request("GET", self.graph + path, token=token, params=params)
            entries = body.get("data")
            if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
                raise LoginError("provider_response_invalid")
            rows.extend(entries)
            paging = body.get("paging", {})
            if not isinstance(paging, dict):
                raise LoginError("provider_response_invalid")
            if not paging.get("next"):
                return rows
            # Never follow paging.next: it could leak the bearer to another host.
            cursors = paging.get("cursors", {})
            cursor = cursors.get("after") if isinstance(cursors, dict) else None
            if not isinstance(cursor, str) or len(cursor) > 2048:
                raise LoginError("provider_response_invalid")
            params["after"] = cursor
        raise LoginError("too_many_accounts")

    async def exchange(self, provider: Provider, code: str) -> list[Asset]:
        if not self.configured(provider):
            raise LoginError("setup_required")
        if provider == "instagram":
            return await self._instagram(code)
        s = self.settings
        assert s.meta_app_secret is not None
        params = {
            "client_id": s.meta_app_id,
            "client_secret": s.meta_app_secret.get_secret_value(),
            "code": code,
        }
        if provider == "facebook":
            params["redirect_uri"] = self.redirect_uri(provider)
        body = await self._request("GET", self.graph + "/oauth/access_token", params=params)
        token = body.get("access_token")
        if not isinstance(token, str) or not token or len(token) > 8192:
            raise LoginError("provider_response_invalid")
        if provider == "facebook":
            extended = await self._request(
                "GET",
                self.graph + "/oauth/access_token",
                params={
                    "grant_type": "fb_exchange_token",
                    "client_id": s.meta_app_id,
                    "client_secret": s.meta_app_secret.get_secret_value(),
                    "fb_exchange_token": token,
                },
            )
            token = extended.get("access_token")
            if not isinstance(token, str) or not token or len(token) > 8192:
                raise LoginError("provider_response_invalid")
        debug = await self._request(
            "GET",
            self.graph + "/debug_token",
            token=s.meta_app_id + "|" + s.meta_app_secret.get_secret_value(),
            params={"input_token": token},
        )
        grant = debug.get("data", {})
        if not isinstance(grant, dict):
            raise LoginError("provider_response_invalid")
        if not grant.get("is_valid") or str(grant.get("app_id")) != s.meta_app_id:
            raise LoginError("wrong_application")
        required = FB_SCOPES if provider == "facebook" else WA_SCOPES
        scopes = grant.get("scopes", [])
        if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
            raise LoginError("provider_response_invalid")
        if not required <= set(scopes):
            raise LoginError("permissions_missing")
        expiry = grant.get("expires_at", 0)
        if not isinstance(expiry, int) or isinstance(expiry, bool) or expiry < 0:
            raise LoginError("provider_response_invalid")
        try:
            expires = datetime.fromtimestamp(expiry, tz=utcnow().tzinfo) if expiry else None
        except (ValueError, OverflowError, OSError) as exc:
            raise LoginError("provider_response_invalid") from exc
        if expires and expires <= utcnow():
            raise LoginError("authorization_expired")
        if provider == "facebook":
            pages = await self._collection("/me/accounts", token, "id,name,access_token,tasks")
            return [
                Asset(
                    "facebook",
                    identifier(p.get("id")),
                    str(p.get("name", "Página"))[:120],
                    p["access_token"],
                    expires,
                )
                for p in pages
                if isinstance(p.get("access_token"), str)
                and p["access_token"]
                and (
                    {"CREATE_CONTENT", "MESSAGING"} <= set(p.get("tasks", []))
                    or "MANAGE" in p.get("tasks", [])
                )
            ]
        granular = grant.get("granular_scopes", [])
        if not isinstance(granular, list) or not all(
            isinstance(g, dict) and isinstance(g.get("target_ids", []), list) for g in granular
        ):
            raise LoginError("provider_response_invalid")
        targets = {
            identifier(i)
            for g in granular
            if g.get("scope") == "whatsapp_business_management"
            for i in g.get("target_ids", [])
        }
        if not targets or len(targets) > 30:
            raise LoginError("no_accounts")
        assets: list[Asset] = []
        for business in sorted(targets):
            numbers = await self._collection(
                f"/{business}/phone_numbers", token, "id,display_phone_number,verified_name"
            )
            assets.extend(
                Asset(
                    "whatsapp",
                    identifier(p.get("id")),
                    (
                        str(p.get("verified_name", "WhatsApp"))
                        + " · "
                        + str(p.get("display_phone_number", ""))
                    )[:120],
                    token,
                    expires,
                    business,
                )
                for p in numbers
            )
        return assets

    async def _instagram(self, code: str) -> list[Asset]:
        s = self.settings
        assert s.instagram_app_secret is not None
        body = await self._request(
            "POST",
            "https://api.instagram.com/oauth/access_token",
            data={
                "client_id": s.instagram_app_id,
                "client_secret": s.instagram_app_secret.get_secret_value(),
                "grant_type": "authorization_code",
                "redirect_uri": self.redirect_uri("instagram"),
                "code": code,
            },
        )
        if "data" in body:
            if (
                "access_token" in body
                or not isinstance(body["data"], list)
                or len(body["data"]) != 1
            ):
                raise LoginError("provider_response_invalid")
            body = body["data"][0]
        if not isinstance(body, dict):
            raise LoginError("provider_response_invalid")
        short = body.get("access_token")
        if not isinstance(short, str) or not short:
            raise LoginError("provider_response_invalid")
        long = await self._request(
            "GET",
            "https://graph.instagram.com/access_token",
            params={
                "grant_type": "ig_exchange_token",
                "client_secret": s.instagram_app_secret.get_secret_value(),
                "access_token": short,
            },
        )
        token, seconds = long.get("access_token"), long.get("expires_in")
        if (
            not isinstance(token, str)
            or not token
            or len(token) > 8192
            or not isinstance(seconds, int)
            or seconds <= 0
        ):
            raise LoginError("provider_response_invalid")
        profile = await self._request(
            "GET", self.ig + "/me", token=token, params={"fields": "user_id,username"}
        )
        external_id = identifier(profile.get("user_id"))
        if body.get("user_id") and identifier(body["user_id"]) != external_id:
            raise LoginError("provider_response_invalid")
        return [
            Asset(
                "instagram",
                external_id,
                str(profile.get("username", "Instagram"))[:120],
                token,
                utcnow() + timedelta(seconds=seconds),
            )
        ]

    async def subscribe(self, asset: Asset) -> None:
        base = self.ig if asset.network == "instagram" else self.graph
        target = asset.business_id if asset.network == "whatsapp" else asset.external_id
        data = (
            {}
            if asset.network == "whatsapp"
            else {"subscribed_fields": "messages,messaging_postbacks"}
        )
        body = await self._request(
            "POST", base + f"/{identifier(target)}/subscribed_apps", token=asset.token, data=data
        )
        if body.get("success") not in (True, "true"):
            raise LoginError("subscription_failed")
