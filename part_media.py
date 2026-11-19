"""Constrained downloads for untrusted OneBot image URLs."""
import asyncio
import ipaddress

import aiohttp
from aiohttp.helpers import is_ip_address
from aiohttp.resolver import ThreadedResolver
from yarl import URL


def _public_ip(value):
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not (addr.is_multicast or addr.is_reserved or addr.is_loopback)


class PublicResolver(ThreadedResolver):
    async def resolve(self, host, port=0, family=0):
        result = await super().resolve(host, port, family)
        if not result or any(not _public_ip(row["host"]) for row in result):
            raise OSError("图片目标不是公网地址")
        return result


def _safe_url(value):
    url = URL(value)
    host = url.raw_host
    if url.scheme not in ("http", "https") or not host or url.user is not None or url.port not in (80, 443):
        raise ValueError("不允许的图片地址")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if is_ip_address(host) or host.replace(".", "").isdigit():
            raise ValueError("不允许非标准 IP 字面量")
        return url
    if not _public_ip(host):
        raise ValueError("图片目标不是公网地址")
    return url


def image_mime(data):
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


_session = None
_resolver = None


async def download_image(value, max_bytes, timeout=15):
    global _session, _resolver
    try:
        url = _safe_url(value)
        if _session is None or _session.closed:
            _resolver = PublicResolver()
            _session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(resolver=_resolver, limit=8, limit_per_host=4),
                cookie_jar=aiohttp.DummyCookieJar(),
            )
        async with asyncio.timeout(timeout):
            for _ in range(4):
                async with _session.get(url, allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                        url = _safe_url(url.join(URL(resp.headers["Location"])))
                        continue
                    if resp.status != 200 or (resp.content_length or 0) > max_bytes:
                        return None
                    data = bytearray()
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        if len(data) + len(chunk) > max_bytes:
                            return None
                        data.extend(chunk)
                    return bytes(data) if image_mime(data) else None
    except (ValueError, TypeError, TimeoutError, OSError, aiohttp.ClientError):
        return None
    return None


async def close():
    if _session and not _session.closed:
        await _session.close()
    if _resolver:
        await _resolver.close()
