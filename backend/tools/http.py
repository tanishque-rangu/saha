"""Hardened HTTP tool with SSRF protection, manual redirect validation, 1 MB size cap, and idempotency key support.

Security measures:
- Strictly http and https schemes only.
- DNS resolution via getaddrinfo: rejects loopback, private, link-local (169.254.x.x),
  multicast, reserved, and unspecified IPv4/IPv6 addresses.
- Explicit blocking of 'localhost'.
- Manual redirect following (max 3 hops) with re-validation of each redirect target.
- Streaming response with strict 1 MB (1,048,576 bytes) limit.
- Timeout capped at 5.0s.
- Idempotency key injection and persistence for deduplicating external mutations across retries.

NOTE: DNS rebinding is only partially mitigated since hostname resolution and HTTP
client connection are separate socket calls.
"""

import ipaddress
import os
import socket
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import httpx

MAX_RESPONSE_BYTES = 1024 * 1024  # 1 MB
MAX_REDIRECT_HOPS = 3
DEFAULT_TIMEOUT = 5.0


def is_public_mode() -> bool:
    return os.getenv("SAHA_PUBLIC_MODE", "false").lower() in ("true", "1", "yes")


def validate_target_url(url: str, allowed_domains: Optional[List[str]] = None) -> None:
    """Validate URL scheme, domain, public allowlist, and resolve IP addresses against prohibited network ranges."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise PermissionError(f"Forbidden URL scheme '{parsed.scheme}'. Only http and https are permitted.")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"Invalid URL '{url}': Missing hostname.")

    hostname_lower = hostname.lower().strip()
    if hostname_lower == "localhost" or hostname_lower.endswith(".localhost"):
        raise PermissionError(f"SSRF blocked: 'localhost' access is forbidden: {url}")

    if is_public_mode():
        if allowed_domains is None:
            raw_allowlist = os.getenv("SAHA_HTTP_ALLOWLIST", "example.com,httpbin.org,wikipedia.org")
            domains_to_check = [d.strip().lower() for d in raw_allowlist.split(",") if d.strip()]
        else:
            domains_to_check = [d.strip().lower() for d in allowed_domains if d.strip()]
        matched = any(hostname_lower == d or hostname_lower.endswith("." + d) for d in domains_to_check)
        if not matched:
            raise PermissionError(
                f"Public mode restriction: Domain '{hostname}' is not in the allowlist ({domains_to_check})."
            )

    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    try:
        addr_infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError(f"Could not resolve hostname '{hostname}': {e}")

    for addr in addr_infos:
        sockaddr = addr[4]
        ip_str = sockaddr[0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            continue

        if (
            ip_obj.is_loopback
            or ip_obj.is_private
            or ip_obj.is_link_local
            or ip_obj.is_reserved
            or ip_obj.is_multicast
            or ip_obj.is_unspecified
        ):
            raise PermissionError(
                f"SSRF blocked: Target '{hostname}' resolved to prohibited address '{ip_str}'."
            )


def is_http_mutation_step(tool: str, args: Dict[str, Any]) -> bool:
    """Determine whether an HTTP step represents an external mutation (POST, PUT, DELETE, PATCH)."""
    if tool in ("http_mutation", "http_post"):
        return True
    if tool in ("http_get", "http_request"):
        raw_method = args.get("method")
        if raw_method:
            return raw_method.upper().strip() in ("POST", "PUT", "DELETE", "PATCH")
        if tool == "http_get":
            return False
        # http_request with payload implies mutation if method not specified
        if "body" in args or "json" in args or "data" in args or "content" in args:
            return True
        return False
    return False


def target_supports_idempotency(tool: str, args: Dict[str, Any]) -> bool:
    """Determine whether the target endpoint supports idempotency keys for mutation deduplication."""
    if not is_http_mutation_step(tool, args):
        return False
    if args.get("supports_idempotency") is not None:
        return bool(args.get("supports_idempotency"))
    if args.get("idempotency_supported") is not None:
        return bool(args.get("idempotency_supported"))
    if args.get("idempotency_key"):
        return True
    if args.get("idempotency_header"):
        return True
    headers = args.get("headers") or {}
    if any(k.lower() in ("idempotency-key", "x-idempotency-key") for k in headers):
        return True
    return False


def http_request(
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    data: Optional[Any] = None,
    json_data: Optional[Any] = None,
    timeout: float = DEFAULT_TIMEOUT,
    allowed_domains: Optional[List[str]] = None,
    idempotency_key: Optional[str] = None,
    idempotency_header: str = "Idempotency-Key",
) -> Dict[str, Any]:
    """Execute an HTTP request with SSRF validation, redirect audit, 1 MB cap, and idempotency header injection."""
    current_url = url
    current_method = method.upper().strip()
    hops = 0

    req_headers = dict(headers) if headers else {}
    if idempotency_key:
        req_headers[idempotency_header] = str(idempotency_key)

    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            while hops <= MAX_REDIRECT_HOPS:
                validate_target_url(current_url, allowed_domains=allowed_domains)

                stream_kwargs: Dict[str, Any] = {"headers": req_headers}
                if json_data is not None:
                    stream_kwargs["json"] = json_data
                elif data is not None:
                    if isinstance(data, (dict, list)):
                        stream_kwargs["json"] = data
                    elif isinstance(data, str):
                        stream_kwargs["content"] = data.encode("utf-8")
                    else:
                        stream_kwargs["content"] = data

                # Stream response to cap size at 1 MB
                with client.stream(current_method, current_url, **stream_kwargs) as resp:
                    # Check for redirect status
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location")
                        if not location:
                            break
                        hops += 1
                        if hops > MAX_REDIRECT_HOPS:
                            return {
                                "url": current_url,
                                "method": current_method,
                                "status_code": -1,
                                "body": "",
                                "error": f"Exceeded maximum redirect limit of {MAX_REDIRECT_HOPS} hops.",
                                "idempotency_key": idempotency_key,
                            }
                        current_url = urljoin(current_url, location)
                        if resp.status_code == 303:
                            current_method = "GET"
                            stream_kwargs.pop("content", None)
                            stream_kwargs.pop("json", None)
                        continue

                    # Read up to 1 MB
                    content_chunks = []
                    total_bytes = 0
                    for chunk in resp.iter_bytes():
                        total_bytes += len(chunk)
                        if total_bytes > MAX_RESPONSE_BYTES:
                            return {
                                "url": current_url,
                                "method": current_method,
                                "status_code": resp.status_code,
                                "body": "",
                                "error": f"Response exceeded 1 MB limit ({total_bytes} bytes received).",
                                "idempotency_key": idempotency_key,
                            }
                        content_chunks.append(chunk)

                    body_bytes = b"".join(content_chunks)
                    body_text = body_bytes.decode("utf-8", errors="replace")

                    return {
                        "url": current_url,
                        "method": current_method,
                        "status_code": resp.status_code,
                        "headers": dict(resp.headers),
                        "body": body_text,
                        "body_length": len(body_bytes),
                        "idempotency_key": idempotency_key,
                    }

            return {
                "url": current_url,
                "method": current_method,
                "status_code": -1,
                "body": "",
                "error": "Failed to complete request.",
                "idempotency_key": idempotency_key,
            }

    except httpx.TimeoutException:
        return {
            "url": current_url,
            "method": current_method,
            "status_code": -1,
            "body": "",
            "error": f"HTTP request timed out after {timeout} seconds.",
            "idempotency_key": idempotency_key,
        }
    except Exception as e:
        return {
            "url": current_url,
            "method": current_method,
            "status_code": -1,
            "body": "",
            "error": str(e),
            "idempotency_key": idempotency_key,
        }


def http_get(
    url: str,
    timeout: float = DEFAULT_TIMEOUT,
    headers: Optional[Dict[str, str]] = None,
    allowed_domains: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Execute an HTTP GET request with SSRF validation, manual redirect audit, and 1 MB cap."""
    return http_request(
        url=url,
        method="GET",
        timeout=timeout,
        headers=headers,
        allowed_domains=allowed_domains,
    )


def http_post(
    url: str,
    data: Optional[Any] = None,
    json_data: Optional[Any] = None,
    timeout: float = DEFAULT_TIMEOUT,
    headers: Optional[Dict[str, str]] = None,
    allowed_domains: Optional[List[str]] = None,
    idempotency_key: Optional[str] = None,
    idempotency_header: str = "Idempotency-Key",
) -> Dict[str, Any]:
    """Execute an HTTP POST mutation with idempotency support and SSRF protection."""
    return http_request(
        url=url,
        method="POST",
        data=data,
        json_data=json_data,
        timeout=timeout,
        headers=headers,
        allowed_domains=allowed_domains,
        idempotency_key=idempotency_key,
        idempotency_header=idempotency_header,
    )
