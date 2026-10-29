import threading
import asyncio
import struct
from urllib.parse import urlparse
from typing import Dict, Optional, Tuple
from loguru import logger

_BRIDGES: Dict[str, "Socks5Bridge"] = {}
_LOCK = threading.Lock()


class Socks5Bridge:
    def __init__(self, host: str, port: int, user: str, pwd: str):
        self.host = host
        self.port = port
        self.user = user
        self.pwd = pwd
        self.local_port: Optional[int] = None
        self._loop = asyncio.new_event_loop()
        self._ready_event = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._ready_event.wait(timeout=5)

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._start_server())
        self._loop.run_forever()

    async def _start_server(self):
        server = await asyncio.start_server(self._handle_client, "127.0.0.1", 0)
        self.local_port = server.sockets[0].getsockname()[1]
        self._ready_event.set()

    async def _pipe(self, r: asyncio.StreamReader, w: asyncio.StreamWriter):
        try:
            while True:
                data = await r.read(65536)
                if not data:
                    break
                w.write(data)
                await w.drain()
        except Exception:
            pass
        finally:
            try:
                w.close()
            except Exception:
                pass

    async def _socks5_connect(self, target_host: str, target_port: int) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        up_r, up_w = await asyncio.open_connection(self.host, self.port)
        # 1. 协商认证
        up_w.write(b"\x05\x02\x00\x02")
        await up_w.drain()
        resp = await up_r.readexactly(2)
        if resp[1] == 2:
            ub, pb = self.user.encode("utf-8"), self.pwd.encode("utf-8")
            up_w.write(b"\x01" + bytes([len(ub)]) + ub + bytes([len(pb)]) + pb)
            await up_w.drain()
            ar = await up_r.readexactly(2)
            if ar[1] != 0:
                raise Exception("SOCKS5 身份验证失败")
        elif resp[1] != 0:
            raise Exception("SOCKS5 握手失败")

        # 2. 请求建立连接
        hb = target_host.encode("utf-8")
        up_w.write(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + struct.pack(">H", target_port))
        await up_w.drain()
        cr = await up_r.readexactly(4)
        if cr[1] != 0:
            raise Exception(f"SOCKS5 连接目标失败: {cr[1]}")
        atyp = cr[3]
        if atyp == 1:
            await up_r.readexactly(6)
        elif atyp == 3:
            dlen = (await up_r.readexactly(1))[0]
            await up_r.readexactly(dlen + 2)
        elif atyp == 4:
            await up_r.readexactly(18)
        return up_r, up_w

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            req_line = await reader.readline()
            if not req_line:
                return
            parts = req_line.decode("utf-8", errors="ignore").split()
            if not parts:
                return
            method = parts[0].upper()

            # 读取原始 HTTP 请求头
            headers = []
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                headers.append(line)

            if method == "CONNECT":
                # HTTPS 隧道建立
                target = parts[1]
                thost, tport = target.split(":")
                up_r, up_w = await self._socks5_connect(thost, int(tport))
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                asyncio.create_task(self._pipe(reader, up_w))
                asyncio.create_task(self._pipe(up_r, writer))
            else:
                # 普通 HTTP 转发
                url = parts[1]
                parsed = urlparse(url)
                thost = parsed.hostname or "127.0.0.1"
                tport = parsed.port or 80
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                up_r, up_w = await self._socks5_connect(thost, tport)
                up_w.write(f"{method} {path} HTTP/1.1\r\n".encode())
                for h in headers:
                    up_w.write(h)
                up_w.write(b"\r\n")
                await up_w.drain()
                asyncio.create_task(self._pipe(reader, up_w))
                asyncio.create_task(self._pipe(up_r, writer))
        except Exception:
            pass


def get_socks5_http_bridge(socks5_url: str) -> Optional[str]:
    """
    检查 socks5 代理。如果是带账密的 SOCKS5 代理，
    启动本地轻量 HTTP 桥接器并返回 http://127.0.0.1:{local_port}，
    完美突破 Chromium 内核对 SOCKS5 账密鉴权的限制！
    """
    if not socks5_url:
        return None
    raw = socks5_url.strip()
    # 过滤 fragment (如 #SOCKS-美国五人共享IP-94)
    if "#" in raw:
        raw = raw.split("#")[0].strip()

    if not raw.startswith(("socks5://", "socks5h://")):
        return None

    parsed = urlparse(raw)
    host = parsed.hostname
    port = parsed.port
    user = parsed.username
    pwd = parsed.password

    # 若无需密码认证，Chromium 原生可直连
    if not user and not pwd:
        return None

    if not host or not port:
        return None

    key = f"{host}:{port}:{user}:{pwd}"
    with _LOCK:
        if key not in _BRIDGES:
            logger.info(f"🚀 检测到带账密 SOCKS5 代理 ({host}:{port})，已激活本地无感 HTTP 协议桥接器...")
            bridge = Socks5Bridge(host, port, user or "", pwd or "")
            _BRIDGES[key] = bridge
        return f"http://127.0.0.1:{_BRIDGES[key].local_port}"
