# mesh-net-proxy

把一台能正常出网的机器，变成虚拟组网内所有设备共用的 **HTTP/SOCKS5 代理 + DoH DNS**。

- `scripts/netproxy.py` — 7890 端口，HTTP CONNECT 与 SOCKS5 同端口自动识别，域名远端解析
- `scripts/dnsproxy.py` — 53 端口（UDP+TCP），转 DoH：dns.google → 1.1.1.1 → 9.9.9.9，带缓存
- 只用 Python 3 标准库，无需 pip、无需 root（占用 53 除外）

## 快速开始

```sh
python3 scripts/netproxy.py --port 7890 &
python3 scripts/dnsproxy.py --port 53 &

curl -x http://127.0.0.1:7890 -o /dev/null -w '%{http_code}\n' https://www.google.com
nslookup -vc example.com 127.0.0.1
```

## 发布到组网

EasyTier（必须先 `-c` 加载配置文件，否则 CLI 热加报 `configuration file is read-only`）：

```toml
[[mapped_listeners]]
bind_addr = "0.0.0.0:7890"
[[mapped_listeners]]
bind_addr = "0.0.0.0:53"
```

客户端填 `10.x.x.x:7890` 即可；DNS 用 `nslookup -vc example.com 10.x.x.x`（mapped-listener 只走 TCP）。

详见 `SKILL.md`。

## 安全

代理**没有认证**，仅限虚拟网段内使用。需要公网暴露时请先加鉴权或前置网关。
