import json
import ssl
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .models import FoundryError, Profile


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A bearer credential must never be forwarded to an endpoint supplied by a redirect.
        return None


def classify_http(status: int) -> FoundryError:
    if status == 401:
        return FoundryError("Inference rejected the bearer token. Check tenant, token audience and "
                           "conditional-access policy.", "authentication")
    if status == 403:
        return FoundryError("Inference access denied. Ask your administrator to check data-plane "
                           "RBAC and resource network policy; management-plane access is not sufficient.",
                           "authorization")
    if status in (404, 405):
        return FoundryError("Inference endpoint or deployment not found, or protocol route unsupported. "
                           "Check the resource URL, deployment name and API protocol.", "endpoint")
    if status in (400, 415, 422):
        return FoundryError("Inference request was rejected. The selected model may not support this "
                           "protocol or smoke-test parameters; check deployment capabilities.", "protocol")
    if status == 429:
        return FoundryError("Inference was rate limited or quota is exhausted.", "quota")
    if 300 <= status < 400:
        return FoundryError("Endpoint redirected the request; redirects are refused to protect credentials.",
                           "endpoint")
    return FoundryError(f"Inference returned HTTP {status}. No response body or credentials were logged.",
                       "service")


def smoke_test(profile: Profile, token: str) -> None:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if profile.protocol == "responses":
        route = "/responses"
        body = {"model": profile.deployment, "input": "Reply with OK.", "max_output_tokens": 32}
        expected = "output"
    elif profile.protocol == "chat":
        route = "/chat/completions"
        body = {"model": profile.deployment, "messages": [{"role": "user", "content": "Reply with OK."}],
                "max_completion_tokens": 32}
        expected = "choices"
    else:
        route = "/v1/messages"
        body = {"model": profile.deployment, "messages": [{"role": "user", "content": "Reply with OK."}],
                "max_tokens": 32}
        headers["anthropic-version"] = "2023-06-01"
        expected = "content"
    request = Request(profile.endpoint + route, data=json.dumps(body).encode(),
                      headers=headers, method="POST")
    try:
        with build_opener(NoRedirects()).open(request, timeout=30) as response:
            if response.status != 200:
                raise classify_http(response.status)
            data = response.read(1_048_577)
            if len(data) > 1_048_576:
                raise FoundryError("Inference response exceeded the smoke-test size limit.", "protocol")
            try:
                result = json.loads(data)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise FoundryError("Inference did not return valid JSON.", "protocol") from exc
            if not isinstance(result, dict) or not isinstance(result.get(expected), list) or "error" in result:
                raise FoundryError("Inference response did not match the requested protocol.", "protocol")
    except HTTPError as exc:
        exc.close()
        raise classify_http(exc.code) from exc
    except (URLError, TimeoutError, ssl.SSLError, ConnectionError, HTTPException) as exc:
        raise FoundryError("Cannot reach inference endpoint. Check DNS, TLS, proxy, firewall and "
                           "private-network access.", "endpoint") from exc
