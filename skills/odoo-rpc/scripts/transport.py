#!/usr/bin/env python3
"""JSON-RPC and JSON-2 transports for talking to an Odoo server.

Odoo exposes two external APIs. Classic JSON-RPC (POST /jsonrpc) works on 17, 18
and 19 but is deprecated and scheduled for removal in Odoo 22. JSON-2
(POST /json/2/<model>/<method>) exists from Odoo 19 and uses named arguments,
bearer authentication and real HTTP status codes.

The two disagree about how failure is reported, which is the whole reason this
module exists: classic RPC answers 200 even for an AccessError and hides the
failure in the body, while JSON-2 answers 4xx/5xx. Both are unwrapped into one
OdooError so callers never have to care.

Usage:
    from transport import probe, build_transport
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

# Anything json.loads can produce. Deliberately not Any: callers must narrow
# before use, and an RPC result genuinely has no static shape.
JsonValue = object

DEFAULT_TIMEOUT = 30.0
JSON_HEADERS = {"Content-Type": "application/json"}

# urllib speaks file:// and ftp:// as readily as http://. A profile URL reaches
# here from a config file or --url, so a typo'd or pasted scheme would otherwise
# turn an RPC call into a local file read that returns its bytes as the answer.
ALLOWED_SCHEMES = frozenset({"http", "https"})


class TransportError(Exception):
    """The server could not be reached, or answered something unusable."""


def _as_version_info(value: object, url: str) -> list[Any]:
    """Coerce a reported version_info, refusing a shape list() would choke on."""
    if value is None or value == []:
        return []
    if not isinstance(value, (list, tuple)):
        raise TransportError(
            f"{url} reported version_info as {type(value).__name__}, not a list; "
            f"it answers on the version route but is not an Odoo server"
        )
    return list(value)


class OdooError(Exception):
    """An error raised by Odoo itself and reported through either protocol."""

    def __init__(
        self,
        name: str,
        message: str,
        arguments: list[Any] | None = None,
        debug: str = "",
    ) -> None:
        super().__init__(message)
        self.name = name
        self.message = message
        self.arguments = arguments or []
        self.debug = debug

    def short_name(self) -> str:
        """The exception class without its module path."""
        return self.name.rsplit(".", 1)[-1] if self.name else "OdooError"

    def __str__(self) -> str:
        return f"{self.short_name()}: {self.message}"


class AuthError(TransportError):
    """Authentication was refused."""


@dataclass(frozen=True)
class ServerInfo:
    """What the version probe learned about a server."""

    version: str
    version_info: list[Any]
    source: str

    @property
    def major(self) -> int:
        """Major version, or 0 when the server did not report a usable one."""
        if self.version_info and isinstance(self.version_info[0], int):
            return self.version_info[0]
        head = self.version.split(".")[0]
        return int(head) if head.isdigit() else 0


def _urlopen(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
    """Perform one HTTP request, returning (status, body).

    An HTTP error status is a normal return here, not an exception: JSON-2 uses
    4xx to carry a structured error body that the caller needs to read.
    """
    scheme = urlparse(request.full_url).scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise TransportError(
            f"{request.full_url} uses the {scheme or 'empty'!r} scheme; "
            f"odoo-rpc speaks only {', '.join(sorted(ALLOWED_SCHEMES))}"
        )
    try:
        # ALLOWED_SCHEMES is enforced above, so file:// and ftp:// never reach here.
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310  # nosemgrep
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except TimeoutError as exc:
        raise TransportError(
            f"{request.full_url} timed out after {timeout:g}s; narrow the query or "
            f"raise --timeout"
        ) from exc
    except urllib.error.URLError as exc:
        raise TransportError(f"cannot reach {request.full_url}: {exc.reason}") from exc
    except OSError as exc:
        # A server can drop the connection outright (ConnectionResetError) instead
        # of answering. That is an OSError but not a URLError, so it would escape
        # unhandled and surface as a traceback rather than a message.
        raise TransportError(f"cannot reach {request.full_url}: {exc}") from exc


def _post_json(
    url: str,
    payload: dict[str, Any],
    timeout: float,
    headers: dict[str, str] | None = None,
) -> tuple[int, Any]:
    """POST a JSON body and decode the JSON response."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={**JSON_HEADERS, **(headers or {})},
        method="POST",
    )
    status, raw = _urlopen(request, timeout)
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError as exc:
        snippet = raw[:200].decode(errors="replace")
        raise TransportError(
            f"{url} answered {status} with a non-JSON body: {snippet!r}"
        ) from exc


def _error_from_payload(payload: dict[str, Any]) -> OdooError:
    """Build an OdooError from either protocol's error object."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    name = data.get("name") or payload.get("name") or ""
    message = (
        data.get("message")
        or payload.get("message")
        or payload.get("error")
        or "unknown Odoo error"
    )
    return OdooError(
        name=str(name),
        message=str(message).strip(),
        arguments=data.get("arguments") or [],
        debug=str(data.get("debug") or ""),
    )


def probe(url: str, timeout: float = DEFAULT_TIMEOUT) -> ServerInfo:
    """Determine the server version, and thus which transports are available.

    GET /web/version is served by the `rpc` addon and exists only from Odoo 19,
    so a usable answer there is itself the signal. A 200 alone proves nothing —
    a website module can answer 200 with HTML for an unknown path — so the body
    must parse as JSON and carry version_info.
    """
    request = urllib.request.Request(f"{url}/web/version", method="GET")
    try:
        status, raw = _urlopen(request, timeout)
    except TransportError:
        status, raw = 0, b""
    if status == 200:
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = None
        if isinstance(body, dict) and "version_info" in body:
            return ServerInfo(
                version=str(body.get("version", "")),
                version_info=_as_version_info(body.get("version_info"), url),
                source="/web/version",
            )

    status, body = _post_json(
        f"{url}/jsonrpc",
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "call",
            "params": {"service": "common", "method": "version", "args": []},
        },
        timeout,
    )
    if not isinstance(body, dict) or "result" not in body:
        raise TransportError(
            f"{url} is not an Odoo server: neither /web/version nor /jsonrpc "
            f"returned version information"
        )
    result = body["result"] or {}
    if not isinstance(result, dict):
        raise TransportError(
            f"{url} answered /jsonrpc with a {type(result).__name__}, not version "
            f"information; it is reachable but does not look like an Odoo server"
        )
    return ServerInfo(
        version=str(result.get("server_version", "")),
        version_info=_as_version_info(result.get("server_version_info"), url),
        source="/jsonrpc common.version",
    )


class Transport:
    """Common interface: authenticate once, then call model methods."""

    name = "abstract"

    def authenticate(self) -> int:
        raise NotImplementedError

    def call(
        self,
        model: str,
        method: str,
        ids: list[int] | None = None,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> JsonValue:
        raise NotImplementedError

    def create(self, model: str, values: JsonValue) -> JsonValue:
        """Create records. The two protocols disagree about how to pass values."""
        raise NotImplementedError


class JsonRpcTransport(Transport):
    """Classic /jsonrpc: stateless execute_kw, errors hidden inside a 200."""

    name = "jsonrpc"

    def __init__(
        self,
        url: str,
        db: str | None,
        login: str | None,
        secret: str | None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.url = url
        self.db = db
        self.login = login
        self.secret = secret
        self.timeout = timeout
        self.uid: int | None = None

    def _rpc(self, service: str, method: str, args: list[Any]) -> JsonValue:
        _status, body = _post_json(
            f"{self.url}/jsonrpc",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "call",
                "params": {"service": service, "method": method, "args": args},
            },
            self.timeout,
        )
        if not isinstance(body, dict):
            raise TransportError(f"{self.url}/jsonrpc returned {body!r}")
        if "error" in body:
            raise _error_from_payload(body["error"])
        return body.get("result")

    def authenticate(self) -> int:
        if self.uid is not None:
            return self.uid
        if not self.db or not self.login:
            raise AuthError(
                "classic JSON-RPC needs both a database and a login; set 'db' and "
                "'login' in the profile or pass --db/--login"
            )
        result = self._rpc("common", "authenticate", [self.db, self.login, self.secret, {}])
        if not result:
            raise AuthError(
                f"Odoo refused the credentials for '{self.login}' on database "
                f"'{self.db}'.\nIf this is an API key, it must be GLOBAL (created "
                f"with no scope) and unexpired."
            )
        self.uid = int(result)
        return self.uid

    def call(
        self,
        model: str,
        method: str,
        ids: list[int] | None = None,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> JsonValue:
        uid = self.authenticate()
        positional: list[Any] = []
        if ids is not None:
            positional.append(ids)
        positional.extend(args or [])
        return self._rpc(
            "object",
            "execute_kw",
            [self.db, uid, self.secret, model, method, positional, kwargs or {}],
        )

    def create(self, model: str, values: JsonValue) -> JsonValue:
        """Pass values positionally.

        Odoo 17/18 dispatch create through _call_kw_model_create, which reads
        args[0] to decide whether it returns one id or a list. Sent as a keyword
        the positional list is empty and the call dies with IndexError. Odoo 19
        accepts either form, so positional is the one that works everywhere.
        """
        return self.call(model, "create", args=[values])


class Json2Transport(Transport):
    """Odoo 19+ /json/2: bearer auth, named arguments, HTTP-status errors."""

    name = "json2"

    def __init__(
        self,
        url: str,
        db: str | None,
        api_key: str | None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.url = url
        self.db = db
        self.api_key = api_key
        self.timeout = timeout
        self.uid: int | None = None

    def _headers(self) -> dict[str, str]:
        headers = {"Authorization": f"bearer {self.api_key}"}
        if self.db:
            headers["X-Odoo-Database"] = self.db
        return headers

    def authenticate(self) -> int:
        if self.uid is not None:
            return self.uid
        if not self.api_key:
            raise AuthError(
                "JSON-2 authenticates with an API key only; set 'api_key' in the "
                "profile or choose transport = \"jsonrpc\""
            )
        context = self.call("res.users", "context_get")
        if not isinstance(context, dict) or "uid" not in context:
            raise AuthError(f"res.users/context_get returned {context!r}")
        self.uid = int(context["uid"])
        return self.uid

    @staticmethod
    def _auth_or_access_error(status: int, payload: JsonValue) -> Exception:
        """Tell a rejected credential apart from a refused permission.

        Odoo maps both `AccessDenied` (wrong key) and `AccessError` (a real user
        who may not touch this record) to HTTP 403, so the status alone cannot
        say which happened. Reporting a permission refusal as a bad key sends the
        caller off to regenerate a key that was working.
        """
        name = payload.get("name", "") if isinstance(payload, dict) else ""
        if status == 403 and name and not name.endswith("AccessDenied"):
            return _error_from_payload(payload)
        return AuthError(
            f"Odoo rejected the credential ({status}). An RPC key must be GLOBAL "
            f"(created with no scope) and unexpired."
        )

    def call(
        self,
        model: str,
        method: str,
        ids: list[int] | None = None,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> JsonValue:
        if args:
            raise TransportError(
                "JSON-2 takes named arguments only, so positional --args cannot be "
                "sent. Use --kwargs, or set transport = \"jsonrpc\" for this call."
            )
        body: dict[str, Any] = dict(kwargs or {})
        if ids is not None:
            body["ids"] = ids
        status, payload = _post_json(
            f"{self.url}/json/2/{model}/{method}", body, self.timeout, self._headers()
        )
        if status in (401, 403):
            raise self._auth_or_access_error(status, payload)
        if status >= 400:
            raise (
                _error_from_payload(payload)
                if isinstance(payload, dict)
                else TransportError(f"HTTP {status}: {payload!r}")
            )
        return payload

    def create(self, model: str, values: JsonValue) -> JsonValue:
        """JSON-2 has no positional arguments, so use the real parameter name."""
        return self.call(model, "create", kwargs={"vals_list": values})


def build_transport(
    url: str,
    db: str | None,
    login: str | None,
    secret: str | None,
    secret_kind: str,
    preference: str = "auto",
    timeout: float = DEFAULT_TIMEOUT,
    server: ServerInfo | None = None,
) -> tuple[Transport, str]:
    """Choose a transport, returning it with a one-line explanation.

    JSON-2 is preferred on 19+ because classic RPC is scheduled for removal in
    Odoo 22, but it can only authenticate with an API key.
    """
    if preference == "jsonrpc":
        return JsonRpcTransport(url, db, login, secret, timeout), "forced by config"
    if preference == "json2":
        return Json2Transport(url, db, secret, timeout), "forced by config"

    info = server or probe(url, timeout)
    if info.major >= 19:
        if secret_kind == "api_key":
            return (
                Json2Transport(url, db, secret, timeout),
                f"server is {info.major}.x and an API key is available",
            )
        return (
            JsonRpcTransport(url, db, login, secret, timeout),
            f"server is {info.major}.x but JSON-2 needs an API key; using classic RPC",
        )
    return (
        JsonRpcTransport(url, db, login, secret, timeout),
        f"server is {info.major or 'pre-19'}.x, which predates JSON-2",
    )
