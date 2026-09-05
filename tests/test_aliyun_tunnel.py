from pathlib import Path


def test_aliyun_tunnel_has_direct_then_proxy_fallback():
    connector = Path("deploy/maxread-aliyun-connect").read_text(encoding="utf-8")
    unit = Path("deploy/systemd/maxread-aliyun-tunnel.service").read_text(encoding="utf-8")
    proxy = Path("deploy/arxiv-egress/config.example.yaml").read_text(encoding="utf-8")

    assert '/usr/bin/nc -z -w "$timeout" "$host" "$port"' in connector
    assert '/usr/bin/nc -X connect -x "$proxy" "$host" "$port"' in connector
    assert "After=network-online.target maxread-egress.service" in unit
    assert "ProxyCommand=/home/lixiaolong/.local/bin/maxread-aliyun-connect %%h %%p" in unit
    assert "ConnectTimeout=20" in unit
    assert "IP-CIDR,47.103.111.28/32,ARXIV,no-resolve" in proxy
