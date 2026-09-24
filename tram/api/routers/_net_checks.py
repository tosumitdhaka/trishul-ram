"""Connectivity-target classification for the connectors router (GH #44).

Ports the loopback/private classification from ``ai.py``'s base-URL allowlist
and extends it with the link-local ranges the SSRF probe must also reject
(cloud-metadata endpoints live at 169.254.0.0/16).  CGNAT (100.64.0.0/10) is
deliberately NOT classified as local — it is routable infrastructure, not the
daemon's own network position (same rationale as ``ai.py``).

DNS names other than ``*.localhost`` are never treated as local: resolving them
at validation time would make the check vulnerable to DNS-rebinding and add
network lookups to a hot path.
"""

from __future__ import annotations

import ipaddress

# Loopback + RFC1918 private + IPv6 unique-local + link-local (IPv4/IPv6).
_FORBIDDEN_NETS = (
    ipaddress.ip_network("0.0.0.0/8"),      # "this network" — not a valid target
    ipaddress.ip_network("127.0.0.0/8"),    # IPv4 loopback
    ipaddress.ip_network("10.0.0.0/8"),     # RFC1918
    ipaddress.ip_network("172.16.0.0/12"),  # RFC1918
    ipaddress.ip_network("192.168.0.0/16"), # RFC1918
    ipaddress.ip_network("169.254.0.0/16"), # IPv4 link-local (cloud metadata)
    ipaddress.ip_network("::1/128"),        # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),       # IPv6 unique-local
    ipaddress.ip_network("fe80::/10"),      # IPv6 link-local
)


def forbidden_target_reason(host: str) -> str | None:
    """Return a human-readable rejection reason for *host*, or None when a
    connection attempt to it is acceptable.

    Loopback/private/link-local IP literals and ``*.localhost`` names are
    rejected (the daemon's own network position); unresolvable DNS names pass —
    they may legitimately point anywhere.
    """
    host = host.strip().strip("[]")  # strip brackets from IPv6 literals
    if not host:
        return None
    if host == "localhost" or host.endswith(".localhost"):
        return f"target {host!r} is a loopback host"
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return None
    # IPv4-mapped IPv6 (e.g. ::ffff:127.0.0.1) resolves to the embedded IPv4.
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    for net in _FORBIDDEN_NETS:
        if addr in net:
            return f"target {host!r} resolves to {addr} which is a private/link-local/loopback address"
    return None