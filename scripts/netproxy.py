# -*- coding: utf-8 -*-
"""netproxy — 极简 HTTP CONNECT / SOCKS5 代理（容器内常驻，供 EasyTier mesh 内设备使用）

为什么需要它：出口机器能直连外网，客户端却因为本地 DNS 污染或线路问题连不上。
连上这个代理后，**域名解析在容器侧完成**，客户端本地 DNS 被污染也不影响——
这正是"外网 DNS 解析不到正确地址"最省事的解法。

监听：0.0.0.0:7890，同一个端口自动识别协议：
    - HTTP 代理（含 CONNECT 隧道、普通 http:// 代理请求）
    - SOCKS5（无认证，支持域名目标 → 远端解析）

用法：
    python3 netproxy.py [--port 7890] [--bind 0.0.0.0]
"""
import argparse
import atexit
import asyncio
import logging
import os
import socket
import sys

LOG_PATH = os.environ.get("NETPROXY_LOG", "/tmp/netproxy.log")
logging.basicConfig(
    level=getattr(logging, os.environ.get("NETPROXY_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH)],
)
log = logging.getLogger("netproxy")

CONNECT_TIMEOUT = 20
MAX_HEADER = 64 * 1024


async def _open(host: str, port: int):
    return await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=CONNECT_TIMEOUT
    )


async def _pipe(reader, writer, tag: str):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
        pass
    except Exception as e:  # noqa: BLE001
        log.debug("%s pipe error: %r", tag, e)
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- SOCKS5
async def handle_socks5(reader, writer, first_byte: bytes):
    peer = writer.get_extra_info("peername")
    try:
        nmethods = (await reader.readexactly(1))[0]
        methods = await reader.readexactly(nmethods)
        if 0 not in methods:
            writer.write(b"\x05\xff")
            await writer.drain()
            return
        writer.write(b"\x05\x00")
        await writer.drain()

        ver, cmd, _rsv, atyp = await reader.readexactly(4)
        if ver != 5:
            return
        if atyp == 1:
            host = socket.inet_ntoa(await reader.readexactly(4))
        elif atyp == 3:
            ln = (await reader.readexactly(1))[0]
            raw_host = await reader.readexactly(ln)
            try:
                host = raw_host.decode("ascii")
            except UnicodeDecodeError:
                host = raw_host.decode("latin-1")
        elif atyp == 4:
            host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
        else:
            return
        port = int.from_bytes(await reader.readexactly(2), "big")

        if cmd != 1:  # 只支持 CONNECT
            writer.write(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            return

        try:
            rh, wh = await _open(host, port)
        except Exception as e:  # noqa: BLE001
            log.info("socks5 connect fail %s:%s %r", host, port, e)
            writer.write(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            return

        writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        await writer.drain()
        log.info("socks5 %s -> %s:%s", peer, host, port)
        await asyncio.gather(
            _pipe(reader, wh, "up"), _pipe(rh, writer, "down")
        )
    except Exception as e:  # noqa: BLE001
        log.debug("socks5 error: %r", e)
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- HTTP
async def handle_http(reader, writer, first_byte: bytes):
    peer = writer.get_extra_info("peername")
    try:
        buf = bytearray(first_byte)
        while b"\r\n\r\n" not in buf:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=30)
            if not chunk:
                return
            buf.extend(chunk)
            if len(buf) > MAX_HEADER:
                return

        head, _, rest = bytes(buf).partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        try:
            method, target, version = lines[0].decode("latin-1").split(" ")
        except ValueError:
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            return
        headers = {}
        for ln in lines[1:]:
            if b":" in ln:
                k, v = ln.split(b":", 1)
                headers[k.strip().lower()] = v.strip()

        if method.upper() == "CONNECT":
            host, _, port = target.rpartition(":")
            port = int(port or 443)
            try:
                rh, wh = await _open(host, port)
            except Exception as e:  # noqa: BLE001
                log.info("http connect fail %s:%s %r", host, port, e)
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                await writer.drain()
                return
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            if rest:
                wh.write(rest)
                await wh.drain()
            log.info("http CONNECT %s -> %s:%s", peer, host, port)
            await asyncio.gather(
                _pipe(reader, wh, "up"), _pipe(rh, writer, "down")
            )
            return

        # 普通 HTTP 代理请求：把绝对 URI 改成 origin-form，其余原样转发
        if not target.lower().startswith("http://"):
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            return
        hostport, _, path = target[7:].partition("/")
        host, _, port = hostport.partition(":")
        port = int(port or 80)
        path = "/" + path
        try:
            rh, wh = await _open(host, port)
        except Exception as e:  # noqa: BLE001
            log.info("http forward fail %s:%s %r", host, port, e)
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await writer.drain()
            return

        out = [f"{method} {path} {version}".encode("latin-1")]
        for k, v in headers.items():
            if k in (b"proxy-connection", b"proxy-authorization", b"connection"):
                continue
            out.append(k + b": " + v)
        if not any(h.startswith(b"host:") for h in out):
            out.append(b"host: " + hostport.encode())
        out.append(b"connection: close")
        wh.write(b"\r\n".join(out) + b"\r\n\r\n" + rest)
        await wh.drain()
        log.info("http proxy %s -> %s:%s%s", peer, host, port, path)
        await asyncio.gather(_pipe(reader, wh, "up"), _pipe(rh, writer, "down"))
    except Exception as e:  # noqa: BLE001
        log.debug("http error: %r", e)
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def handle(reader, writer):
    try:
        first = await asyncio.wait_for(reader.readexactly(1), timeout=30)
    except Exception:  # noqa: BLE001
        writer.close()
        return
    if first == b"\x05":
        await handle_socks5(reader, writer, first)
    else:
        await handle_http(reader, writer, first)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("NETPROXY_PORT", 7890)))
    ap.add_argument("--bind", default=os.environ.get("NETPROXY_BIND", "0.0.0.0"))
    ap.add_argument("--pidfile", default="/tmp/netproxy.pid")
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
    srv = await asyncio.start_server(handle, a.bind, a.port, reuse_address=True)
    log.info("netproxy listening on %s:%s (HTTP CONNECT + SOCKS5)", a.bind, a.port)
    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)