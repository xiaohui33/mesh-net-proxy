---
name: mesh-net-proxy
description: 在一台能正常出网、但内网设备上网受限的机器上跑两个零依赖小服务（HTTP/SOCKS5 代理 + DoH DNS 转发），再通过 EasyTier 之类的虚拟组网把 7890 与 53 发布给同网段设备，让手机/电视/旧电脑共享这条干净出口。适用于容器云主机、跨境办公、内网 DNS 被污染的场景。
metadata:
  builtin_skill_version: "1.0"
  qwenpaw:
    emoji: "🌐"
    requires: {}
---

# mesh-net-proxy：把一台干净出口的机器变成组网内的代理 + DNS

## 什么时候用

- 你有一台能直连外网的机器（云容器、VPS、家里的小主机），但**它没有公网 IP、没法做端口映射**；
- 你的手机/平板/电视/老电脑在同一个 EasyTier / WireGuard / Tailscale 之类的**虚拟局域网**里；
- 你希望这些设备共用那条干净出口，而不是每台设备各装一套客户端。

思路很简单：代理和 DNS 都跑在干净出口那台机器上，**域名在远端解析**，所以客户端本地 DNS 被污染也不影响结果；再用组网的 mapped-listener 把端口发布出去，客户端只填 `10.x.x.x:7890`。

两个脚本都只用 Python 3 标准库，不需要 pip、不需要 root（53 端口除外）。

## 目录

```
mesh-net-proxy/
├── SKILL.md
├── README.md
└── scripts/
    ├── netproxy.py    # 7890：HTTP 代理（含 CONNECT）+ SOCKS5，同端口自动识别
    └── dnsproxy.py    # 53：UDP/TCP DNS → DoH（dns.google → 1.1.1.1 → 9.9.9.9）
```

## 一、在出口机器上起服务

```sh
python3 scripts/netproxy.py --port 7890 --bind 0.0.0.0 &
python3 scripts/dnsproxy.py  --port 53   --bind 0.0.0.0 &
```

- `netproxy.py`：同一端口同时认 HTTP 代理和 SOCKS5，**域名交给远端解析**（SOCKS5 用域名目标，HTTP 用 CONNECT 里的 host）。
- `dnsproxy.py`：先把 UDP/TCP 53 收下来，转成 DoH 发出去，带小缓存；三个 DoH 端点按顺序回退。
- 两个进程都写 `/tmp/<名字>.pid`，日志在 `/tmp/<名字>.log`，环境变量 `NETPROXY_LOG` / `DNSPROXY_LOG` 可改路径。

先在本机自测，别急着发布：

```sh
curl -x http://127.0.0.1:7890 -o /dev/null -w '%{http_code}\n' https://www.google.com
curl --socks5-hostname 127.0.0.1:7890 -o /dev/null -w '%{http_code}\n' https://www.youtube.com
nslookup -vc example.com 127.0.0.1        # -vc 表示走 TCP，最稳
```

## 二、把端口发布到虚拟局域网

以 EasyTier 为例，**必须用配置文件启动**（`easytier-core -c /etc/easytier/config.toml`），然后用 CLI 热加端口才不会报 `configuration file is read-only`：

```toml
# /etc/easytier/config.toml
[[mapped_listeners]]
bind_addr = "0.0.0.0:7890"

[[mapped_listeners]]
bind_addr = "0.0.0.0:53"     # 注意：mapped-listener 只走 TCP
```

或者启动后热加：

```sh
easytier-cli mapped-listener add tcp://0.0.0.0:7890
easytier-cli mapped-listener add tcp://0.0.0.0:53
```

发布完，在同网段的另一台设备上验证：

```sh
ping 10.x.x.x                                  # 先确认在同一个 mesh 里
curl -x http://10.x.x.x:7890 -o /dev/null -w '%{http_code}\n' https://www.google.com
nslookup -vc example.com 10.x.x.x               # 走 TCP 的 DNS
```

## 三、客户端怎么填

| 场景 | 怎么配 |
|---|---|
| Windows / macOS 系统代理 | 手动代理，地址 `10.x.x.x`，端口 `7890`，类型 HTTP |
| 浏览器（SwitchyOmega 等） | 同一个 `7890`，SOCKS5 也填这个端口 |
| 手机（同 WiFi / 同 mesh） | WiFi 代理 → 手动 → 主机 `10.x.x.x`、端口 `7890`（iOS/Android 只支持 HTTP 代理） |
| 命令行 | `export https_proxy=http://10.x.x.x:7890` |
| 只想修 DNS | `nslookup -vc example.com 10.x.x.x` |

注意：改 DNS 只解决「解析到错误 IP」。如果解析出来的 IP 本身连不通，还是得走代理。

## 四、常驻与自愈

机器重启/容器重建后端口就没了，两个选择：

1. **cron 定时拉起**：每分钟检查 pidfile + `kill -0`，不在就重启（本 skill 推荐，最简单）；
2. **挂到现有启动脚本末尾**（比如网络组件启动脚本），用幂等的 `ensure-services.sh` 统一拉起。

判断存活**别用 `pgrep`**：受限容器里读不到别的进程，容易误判；用 pidfile + `kill -0`。

## 五、必须知道的坑

- **mapped-listener 只走 TCP**。UDP 53 的发布在多数实现里不通，所以 `dnsproxy.py` 同时监听 UDP/TCP，但发布出去只能用 TCP（客户端加 `-vc`）。
- **不用 `-c` 启动 EasyTier 就用不了 CLI 热加**，报 `configuration file is read-only`——它想改配置但配置压根没被加载。
- **端口冲突**：53 被 systemd-resolved / dnsmasq 占了的话，要么停掉它们，要么改 `--port` 并同步改客户端。
- **DoH 需要能出 443**。出口机器如果被限速或只允许白名单域名，`dnsproxy.py` 会全部回退失败，此时把 DNS 换成普通 UDP 转发（改脚本里的 upstream 即可）。
- **代理无认证**。只发布在虚拟网段内，别把这套端口映射到公网；要暴露公网先加认证或前置一层带鉴权的网关。

## 六、自测清单

```sh
# 出口机器
curl -x http://127.0.0.1:7890 -o /dev/null -w 'http %{http_code}\n' https://www.google.com
nslookup -vc example.com 127.0.0.1

# mesh 内另一台设备
ping 10.x.x.x
curl -x http://10.x.x.x:7890 -o /dev/null -w 'http %{http_code}\n' https://www.google.com
nslookup -vc example.com 10.x.x.x
```

四项都通，才算发布成功。