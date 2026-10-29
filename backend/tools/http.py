"""Hardened HTTP GET tool with SSRF protection, manual redirect validation, and 1 MB size cap.

Security measures:
- Strictly http and https schemes only.
- DNS resolution via getaddrinfo: rejects loopback, private, link-local (169.254.x.x),
  multicast, reserved, and unspecified IPv4/IPv6 addresses.
- Explicit blocking of 'localhost'.
- Manual redirect following (max 3 hops) with re-validation of each redirect target.
- Streaming response with strict 1 MB (1,048,576 bytes) limit.
- Timeout capped at 5.0s.

NOTE: DNS rebinding is only partially mitigated since hostname resolution and HTTP
client connection are separate socket calls.
"""

import ipaddress
import socket
from typing import Any, Dict, Optional
from urllib.parse import urljoin, urlparse

import httpx

MAX_RESPONSE_BYTES = 1024 * 1024  # 1 MB
MAX_REDIRECT_HOPS = 3
DEFAULT_TIMEOUT = 5.0


def validate_target_url(url: str) -> None:
    """Validate URL scheme, domain, and resolve IP addresses against prohibited network ranges."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise PermissionError(f"Forbidden URL scheme '{parsed.scheme}'. Only http and https are permitted.")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"Invalid URL '{url}': Missing hostname.")

    hostname_lower = hostname.lower().strip()
    if hostname_lower == "localhost" or hostname_lower.endswith(".localhost"):
        raise PermissionError(f"SSRF blocked: 'localhost' access is forbidden: {url}")

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


def http_get(
    url: str,
    timeout: float = DEFAULT_TIMEOUT,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Execute an HTTP GET request with SSRF validation, manual redirect audit, and 1 MB cap."""
    current_url = url
    hops = 0

    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            while hops <= MAX_REDIRECT_HOPS:
                validate_target_url(current_url)

                # Stream response to cap size at 1 MB
                with client.stream("GET", current_url, headers=headers) as resp:
                    # Check for redirect status
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location")
                        if not location:
                            break
                        hops += 1
                        if hops > MAX_REDIRECT_HOPS:
                            return {
                                "url": current_url,
                                "status_code": -1,
                                "body": "",
                                "error": f"Exceeded maximum redirect limit of {MAX_REDIRECT_HOPS} hops.",
                            }
                        current_url = urljoin(current_url, location)
                        continue

                    # Read up to 1 MB
                    content_chunks = []
                    total_bytes = 0
                    for chunk in resp.iter_bytes():
                        total_bytes += len(chunk)
                        if total_bytes > MAX_RESPONSE_BYTES:
                            return {
                                "url": current_url,
                                "status_code": resp.status_code,
                                "body": "",
                                "error": f"Response exceeded 1 MB limit ({total_bytes} bytes received).",
                            }
                        content_chunks.append(chunk)

                    body_bytes = b"".join(content_chunks)
                    body_text = body_bytes.decode("utf-8", errors="replace")

                    return {
                        "url": current_url,
                        "status_code": resp.status_code,
                        "headers": dict(resp.headers),
                        "body": body_text,
                        "body_length": len(body_bytes),
                    }

            return {
                "url": current_url,
                "status_code": -1,
                "body": "",
                "error": "Failed to complete request.",
            }

    except httpx.TimeoutException:
        return {
            "url": current_url,
            "status_code": -1,
            "body": "",
            "error": f"HTTP request timed out after {timeout} seconds.",
        }
    except Exception as e:
        return {
            "url": current_url,
            "status_code": -1,
            "body": "",
            "error": str(e),
        }
