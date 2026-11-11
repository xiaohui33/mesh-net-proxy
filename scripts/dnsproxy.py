# -*- coding: utf-8 -*-
"""dnsproxy — 把 UDP/TCP 53 的 DNS 查询转发到 DoH（dns.google，失败自动换 1.1.1.1 / 9.9.9.9）

容器出口在新加坡，DoH 直连可用（约 30–100ms）；查询结果带小缓存。
用途：把「本地 DNS 被污染」变成「解析在我们这边完成」。

监听：0.0.0.0:53（UDP + TCP）。UDP 供容器本地/局域网用，
TCP 供 EasyTier mapped-listener 发布给 mesh 内设备用（mapped-listener 只走 TCP）。

用法：
    python3 dnsproxy.py [--port 53]
"""
import argparse
import atexit
import asyncio
import logging
import os
import ssl
import struct
import sys
import time

LOG_PATH = os.environ.get("DNSPROXY_LOG", "/tmp/dnsproxy.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH)],
)
log = logging.getLogger("dnsproxy")

UPSTREAMS = ["dns.google", "cloudflare-dns.com", "dns.quad9.net"]
CACHE_TTL = 300
CACHE_MAX = 4096
TIMEOUT = 8

_ctx = ssl.create_default_context()
_cache: dict[bytes, tuple[bytes, float]] = {}


def _cache_get(key: bytes):
    hit = _cache.get(key)
    if not hit:
        return None
    data, exp = hit
    if exp < time.time():
        _cache.pop(key, None)
        return None
    return data


def _cache_put(key: bytes, data: bytes):
    if len(_cache) >= CACHE_MAX:
        # 粗粒度淘汰：清掉最早插入的一批
        for k in list(_cache)[: CACHE_MAX // 4]:
            _cache.pop(k, None)
    _cache[key] = (data, time.time() + CACHE_TTL)


async def _doh_once(host: str, query: bytes) -> bytes:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, 443, ssl=_ctx, server_hostname=host),
        timeout=TIMEOUT,
    )
    try:
        head = (
            "POST /dns-query HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Content-Type: application/dns-message\r\n"
            "Accept: application/dns-message\r\n"
            f"Content-Length: {len(query)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        writer.write(head + query)
        await writer.drain()

        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=TIMEOUT)
            if not chunk:
                raise RuntimeError("eof in headers")
            buf += chunk
        header, _, body = buf.partition(b"\r\n\r\n")
        status = int(header.split(b" ")[1])
        if status != 200:
            raise RuntimeError(f"doh http {status}")
        ctype = b"application/dns-message"
        if ctype not in header.lower():
            raise RuntimeError("doh bad content-type")
        # 按 Content-Length 读完
        clen = None
        for ln in header.split(b"\r\n")[1:]:
            if ln.lower().startswith(b"content-length:"):
                clen = int(ln.split(b":")[1])
                break
        if clen is not None:
            while len(body) < clen:
                chunk = await asyncio.wait_for(reader.read(65536), timeout=TIMEOUT)
                if not chunk:
                    break
                body += chunk
            return body[:clen]
        # 没有 Content-Length：读到 EOF
        while True:
            chunk = await asyncio.wait_for(reader.read(65536), timeout=TIMEOUT)
            if not chunk:
                break
            body += chunk
        return body
    finally:
        writer.close()


async def resolve(query: bytes) -> bytes:
    key = query[2:]  # 去掉 ID：同一问题的答案可复用
    hit = _cache_get(key)
    if hit:
        return query[:2] + hit[2:]
    last = None
    for host in UPSTREAMS:
        try:
            resp = await _doh_once(host, query)
            if len(resp) > 12:
                _cache_put(key, resp)
                return resp
        except Exception as e:  # noqa: BLE001
            last = e
            log.debug("upstream %s failed: %r", host, e)
    log.warning("all upstreams failed: %r", last)
    # 全部失败：返回 SERVFAIL
    flags = struct.unpack("!H", query[2:4])[0] | 0x8000 | 0x0002
    return query[:2] + struct.pack("!H", flags) + query[4:12] + query[12:]


class UDPProto(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        asyncio.create_task(self._answer(data, addr))

    async def _answer(self, data, addr):
        try:
            resp = await resolve(data)
        except Exception as e:  # noqa: BLE001
            log.debug("udp resolve error %r", e)
            return
        try:
            self.transport.sendto(resp, addr)
        except Exception:  # noqa: BLE001
            pass


async def handle_tcp(reader, writer):
    try:
        while True:
            head = await asyncio.wait_for(reader.readexactly(2), timeout=30)
            ln = struct.unpack("!H", head)[0]
            if not ln:
                break
            query = await asyncio.wait_for(reader.readexactly(ln), timeout=30)
            resp = await resolve(query)
            writer.write(struct.pack("!H", len(resp)) + resp)
            await writer.drain()
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionResetError):
        pass
    except Exception as e:  # noqa: BLE001
        log.debug("tcp error %r", e)
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("DNSPROXY_PORT", 53)))
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--pidfile", default="/tmp/dnsproxy.pid")
    a = ap.parse_args()
    try:
        with open(a.pidfile, "w") as fh:
            fh.write(str(os.getpid()))

        def _cleanup():
            try:
                if os.path.exists(a.pidfile):
                    os.remove(a.pidfile)
            except OSError:
                pass

        atexit.register(_cleanup)
    except OSError:
        pass

    loop = asyncio.get_running_loop()
    udp, _ = await loop.create_datagram_endpoint(
        UDPProto, local_addr=(a.bind, a.port), reuse_port=False
    )
    tcp = await asyncio.start_server(handle_tcp, a.bind, a.port, reuse_address=True)
    log.info("dnsproxy on %s:%s udp+tcp -> %s", a.bind, a.port, ",".join(UPSTREAMS))
    async with tcp:
        await tcp.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)