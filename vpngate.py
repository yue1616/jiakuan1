#!/usr/bin/env python3
"""
VPN Gate SSTP 节点检测流水线 (精简版)
=====================================
流程:
  1. 获取 VPN Gate 原始节点
  2. 只保留带 TCP 入口的 SSTP 节点
  3. 去重
  4. 并发调用检测 Worker
  5. 生成 public/data.json + public/index.html + public/nodes.txt
"""

import base64
import csv
import io
import ipaddress
import json
import os
import re
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
REPO_DIR = os.path.dirname(os.path.abspath(__file__))

VPNGATE_API = os.environ.get("VPNGATE_API", "http://www.vpngate.net/api/iphone/")
VPNGATE_MIRROR = os.environ.get(
    "VPNGATE_MIRROR",
    "https://raw.githubusercontent.com/fdciabdul/Vpngate-Scraper-API/main/json/data.json",
)
WORKER_CHECK_URL = os.environ.get("CHECK_WORKER", "https://xy1.xiaoyue1.ccwu.cc/check?sstp=vpn:vpn@")
CONCURRENCY = max(1, int(os.environ.get("CHECK_CONCURRENCY", "32")))
CHECK_TIMEOUT = float(os.environ.get("CHECK_TIMEOUT", "90"))
MAX_CHECK_NODES = int(os.environ.get("MAX_CHECK_NODES", "0"))
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "60"))

# 优选入口 API：自动获取候选 IP；GitHub Actions 可通过环境变量覆盖
OPTIMAL_API = os.environ.get(
    "OPTIMAL_API",
    "https://bestcf.pages.dev/cfyes/ipv4.txt,"
    "https://bestcf.pages.dev/wetest/ipv4.txt",
)
EDGE_TEST_TIMEOUT = max(0.5, float(os.environ.get("EDGE_TEST_TIMEOUT", "2.5")))
EDGE_TEST_REPEATS = max(1, int(os.environ.get("EDGE_TEST_REPEATS", "2")))
EDGE_TEST_CONCURRENCY = max(1, int(os.environ.get("EDGE_TEST_CONCURRENCY", "48")))
EDGE_MAX_SELECTED = max(1, int(os.environ.get("EDGE_MAX_SELECTED", "20")))
PUBLIC_DIR = os.environ.get("PUBLIC_DIR", os.path.join(REPO_DIR, "public"))
TEMPLATE_HTML = next(
    (path for path in (
        os.path.join(REPO_DIR, "web", "index.html"),
        os.path.join(REPO_DIR, "index.html"),
    ) if os.path.isfile(path)),
    os.path.join(REPO_DIR, "index.html"),
)

DATA_CENTER_ORG_KEYWORDS = [
    "GOOGLE", "AMAZON", "AWS", "MICROSOFT", "OVH", "HETZNER", "DIGITALOCEAN",
    "AKAMAI", "CLOUDFLARE", "FASTLY", "RACKSPACE", "EQUINIX", "LINODE", "VULTR",
    "HURRICANE", "TENCENT", "ALIBABA", "ALIYUN", "LEASWEB",
]
RESIDENTIAL_ORG_KEYWORDS = [
    "NTT EAST", "NTT WEST", "NTT COMMUNICATIONS", "NTT BROADBAND", "KDDI", "DOCOMO",
    "SOFTBANK", "AU COMMUNICATIONS", "J:COM", "JCOM", "OCN", "BIGLOBE",
    "IIJ", "SEIKO", "CLEVER-NET", "AT&T", "COMCAST", "XFINITY", "VERIZON",
    "TELUS", "ROGERS", "BELL CANADA", "VODAFONE", "ORANGE", "DEUTSCHE TELEKOM",
    "BREEZE", "TIM S.P.A", "LIBERO", "FASTWEB", "FREE FRANCE", "BT OPEN",
]

COUNTRY_ZH = {
    "JP": "日本", "KR": "韩国", "US": "美国", "CA": "加拿大", "RU": "俄罗斯",
    "RO": "罗马尼亚", "TH": "泰国", "VN": "越南", "DE": "德国", "FR": "法国",
    "GB": "英国", "UK": "英国", "SG": "新加坡", "TW": "台湾", "HK": "香港",
    "CN": "中国", "AU": "澳大利亚", "NL": "荷兰", "SE": "瑞典", "CH": "瑞士",
    "IT": "意大利", "ES": "西班牙", "PL": "波兰", "IN": "印度", "BR": "巴西",
    "MX": "墨西哥", "ID": "印度尼西亚", "MY": "马来西亚", "PH": "菲律宾",
    "TR": "土耳其", "UA": "乌克兰", "CZ": "捷克", "GR": "希腊", "PT": "葡萄牙",
    "FI": "芬兰", "NO": "挪威", "DK": "丹麦", "IE": "爱尔兰", "BE": "比利时",
    "AT": "奥地利", "HU": "匈牙利", "AR": "阿根廷", "CL": "智利", "CO": "哥伦比亚",
    "NZ": "新西兰", "ZA": "南非", "IL": "以色列", "AE": "阿联酋", "SA": "沙特",
    "EG": "埃及", "HR": "克罗地亚", "BY": "白俄罗斯", "GD": "格林纳达",
    "LV": "拉脱维亚", "EE": "爱沙尼亚", "LT": "立陶宛", "SK": "斯洛伐克",
    "SI": "斯洛文尼亚", "BG": "保加利亚", "RS": "塞尔维亚", "GE": "格鲁吉亚",
    "MD": "摩尔多瓦", "AM": "亚美尼亚", "KZ": "哈萨克斯坦", "UZ": "乌兹别克斯坦",
    "MN": "蒙古", "NP": "尼泊尔", "LK": "斯里兰卡", "MM": "缅甸",
}

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
_section = None

def log(section, msg=""):
    global _section
    if section != _section:
        print(f"========== {section} ==========")
        _section = section
    if msg:
        print(msg, flush=True)

def die(msg):
    log("FATAL", f"[失败] {msg}")
    sys.exit(1)

# ---------------------------------------------------------------------------
# 数据抓取
# ---------------------------------------------------------------------------
def fetch_vpngate():
    try:
        log("VPN GATE", f"获取官方 API: {VPNGATE_API}")
        resp = requests.get(VPNGATE_API, timeout=HTTP_TIMEOUT, headers={"User-Agent": "Mozilla/5.0 (compatible; gate-checker)"})
        resp.raise_for_status()
        rows = parse_csv(resp.text)
        if rows:
            log("VPN GATE", f"主源(官方 API) 获取到 {len(rows)} 个原始节点")
            return rows, "vpngate.net/api/iphone"
        raise RuntimeError("官方 API 返回 0 行数据")
    except Exception as exc:
        log("VPN GATE", f"官方 API 获取失败: {exc}")

    try:
        log("VPN GATE", f"回退镜像: {VPNGATE_MIRROR}")
        resp = requests.get(VPNGATE_MIRROR, timeout=HTTP_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        rows = parse_mirror_json(resp.json())
        if rows:
            log("VPN GATE", f"回退源(镜像) 获取到 {len(rows)} 个原始节点")
            return rows, "github-mirror"
    except Exception as exc:
        log("VPN GATE", f"回退镜像也失败: {exc}")
    die("VPN Gate 官方 API 与回退镜像均不可用, 数据源完全失败")

def parse_csv(text):
    lines = [ln for ln in text.splitlines() if ln.strip()]
    header_idx = None
    for i, ln in enumerate(lines):
        if ln.lstrip("#").startswith("HostName"):
            header_idx = i
            break
    if header_idx is None:
        raise RuntimeError("找不到 CSV 表头行 (HostName)")

    header = lines[header_idx].lstrip("#").split(",")
    data_lines = lines[header_idx + 1:]
    idx = {}
    for col in ("hostname", "ip", "countrylong", "countryshort", "openvpn_configdata_base64"):
        for i, h in enumerate(header):
            if h.strip().lstrip("*").lower() == col:
                idx[col] = i
                break
    if "openvpn_configdata_base64" not in idx:
        for i, h in enumerate(header):
            if "base64" in h.lower():
                idx["openvpn_configdata_base64"] = i
                break
    pos = {"hostname": idx.get("hostname", 0), "ip": idx.get("ip", 1), "countrylong": idx.get("countrylong", 5), "countryshort": idx.get("countryshort", 6), "openvpn_configdata_base64": idx.get("openvpn_configdata_base64", len(header) - 1)}

    rows = []
    for ln in data_lines:
        fields = next(csv.reader(io.StringIO(ln)))
        if len(fields) < 7: continue
        host = fields[pos["hostname"]].strip()
        ip = fields[pos["ip"]].strip()
        if not host or not ip: continue
        rows.append({"host": host, "ip": ip, "country_long": fields[pos["countrylong"]].strip(), "country_short": fields[pos["countryshort"]].strip(), "config_b64": fields[pos["openvpn_configdata_base64"]].strip()})
    return rows

def parse_mirror_json(data):
    servers = []
    items = data if isinstance(data, list) else [data]
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("servers"), list):
            servers.extend(item["servers"])
        elif isinstance(item, dict):
            servers.append(item)
    rows = []
    for s in servers:
        host = str(s.get("hostname") or s.get("host") or "").strip()
        ip = str(s.get("ip") or "").strip()
        if not host or not ip: continue
        rows.append({"host": host, "ip": ip, "country_long": str(s.get("countrylong") or s.get("country_long") or s.get("country") or "").strip(), "country_short": str(s.get("countryshort") or s.get("country_short") or "").strip(), "config_b64": str(s.get("openvpn_configdata_base64") or s.get("config_b64") or "").strip()})
    return rows

# ---------------------------------------------------------------------------
# 筛选 SSTP 节点
# ---------------------------------------------------------------------------
_PROTO_TCP_RE = re.compile(r"^proto\s+(tcp|tcp4|tcp6)\b", re.M)
_REMOTE_RE = re.compile(r"^remote\s+\S+\s+(\d+)", re.M)

def to_sstp_nodes(rows):
    nodes = []
    for r in rows:
        cfg = ""
        if r["config_b64"]:
            try:
                cfg = base64.b64decode(r["config_b64"], validate=False).decode("utf-8", "replace")
            except Exception:
                cfg = ""
        if not _PROTO_TCP_RE.search(cfg): continue
        m = _REMOTE_RE.search(cfg)
        if not m: continue
        port = int(m.group(1))
        if not (1 <= port <= 65535): continue
        host = r["host"]
        if not host.endswith(".opengw.net"):
            host = f"{host}.opengw.net"
        nodes.append({"host": host, "port": port, "ip": r["ip"], "country": r["country_long"], "country_code": r["country_short"]})
    return nodes

def dedupe(nodes):
    seen = set()
    out = []
    for n in nodes:
        key = (n["host"].lower(), n["port"], "sstp")
        if key in seen: continue
        seen.add(key)
        out.append(n)
    return out

# ---------------------------------------------------------------------------
# 检测 Worker
# ---------------------------------------------------------------------------
def classify_network(host, exit_org, is_datacenter=None):
    if is_datacenter is True: return "datacenter"
    if is_datacenter is False: return "residential"
    org = (exit_org or "").upper()
    if org:
        if any(k in org for k in DATA_CENTER_ORG_KEYWORDS): return "datacenter"
        if any(k in org for k in RESIDENTIAL_ORG_KEYWORDS): return "residential"
    h = host.lower()
    if h.startswith("public-vpn"): return "datacenter"
    if re.match(r"^vpn\d{5,}", h) or re.match(r"^vpnv\d+", h): return "residential"
    return "unknown"

def build_worker_check_url(node):
    """构造 Worker 检测 URL，避免仅传入 vpn:vpn@ 而漏掉节点主机名。"""
    base = (WORKER_CHECK_URL or "").strip()
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("CHECK_WORKER 必须是完整的 http(s) Worker URL")

    target = f"{node['host']}:{int(node['port'])}"
    params = parse_qsl(parts.query, keep_blank_values=True)
    found = False
    for i, (key, value) in enumerate(params):
        if key.lower() != "sstp":
            continue
        found = True
        value = value.strip()
        if value.endswith("@"):
            value = value + target
        elif "@" in value:
            # 如果 Secret 不小心带了旧主机地址，保留认证信息并替换成当前节点
            credentials = value.rsplit("@", 1)[0]
            value = f"{credentials}@{target}"
        elif value:
            # 兼容只配置了 sstp= 的情况；默认使用 VPN Gate 常见凭据 vpn:vpn
            value = f"vpn:vpn@{target}"
        else:
            value = f"vpn:vpn@{target}"
        params[i] = (key, value)
        break

    if not found:
        params.append(("sstp", f"vpn:vpn@{target}"))

    query = urlencode(params, doseq=True)
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/check", query, parts.fragment))


def check_one(node, session):
    out = dict(node)
    out["protocol"] = "sstp"
    out["link"] = f"sstp://vpn:vpn@{node['host']}:{node['port']}"
    out["status"] = "failed"
    out["checked_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out["exit"] = None
    out["residential"] = "unknown"
    try:
        url = build_worker_check_url(node)
        r = session.get(url, timeout=CHECK_TIMEOUT, headers={"User-Agent": "Mozilla/5.0 (gate-checker)"})
        if r.status_code != 200:
            out["error"] = f"HTTP {r.status_code}"
            out["worker_error"] = True
            return out
        j = r.json()
        worker_hostname = str(j.get("hostname") or "").strip().rstrip(".").lower()
        expected_hostname = str(node.get("host") or "").strip().rstrip(".").lower()
        if not worker_hostname or worker_hostname != expected_hostname:
            out["error"] = (
                "Worker 未能正确解析节点主机名"
                f"（返回 hostname={worker_hostname or '空'}，预期 {expected_hostname or '空'}）"
            )
            out["worker_error"] = True
            out["worker_candidate"] = j.get("candidate")
            return out

        ok = bool(j.get("success"))
        out["success"] = ok
        out["status"] = "success" if ok else "failed"
        out["latency_ms"] = j.get("responseTime")
        out["colo"] = j.get("colo")
        out["error"] = (None if ok else (j.get("error") or j.get("message") or "check failed"))
        exit_info = j.get("exit") or {}
        if exit_info:
            asn = exit_info.get("asn") or {}
            org = asn.get("org") or asn.get("name") or ""
            out["exit"] = {"ip": exit_info.get("ip"), "country": exit_info.get("country"), "country_code": exit_info.get("country_code"), "city": exit_info.get("city"), "continent": exit_info.get("continent"), "asn": asn.get("asn"), "org": org, "type": asn.get("type"), "is_datacenter": exit_info.get("is_datacenter")}
            out["residential"] = classify_network(out["host"], org, exit_info.get("is_datacenter"))
        else:
            out["residential"] = classify_network(out["host"], None, None)
        return out
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["worker_error"] = True
        return out

def check_all(nodes, session):
    results = []
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = [pool.submit(check_one, n, session) for n in nodes]
        for fut in as_completed(futures):
            results.append(fut.result())
    return results

# ---------------------------------------------------------------------------
# 生成数据
# ---------------------------------------------------------------------------
def build_outputs(results, raw_count, sstp_count, source):
    available = [r for r in results if r.get("success")]
    countries = {}
    for n in available:
        c = n["country"] or "未知"
        countries.setdefault(c, {"code": n["country_code"] or "?", "nodes": []})["nodes"].append(n)

    stats = {"raw_nodes": raw_count, "sstp_nodes": sstp_count, "checked": len(results), "success": len(available), "failed": len(results) - len(available), "countries": len(countries), "residential_est": sum(1 for n in available if n["residential"] == "residential"), "datacenter_est": sum(1 for n in available if n["residential"] == "datacenter")}
    by_country = {}
    for name, grp in countries.items():
        grp["count"] = len(grp["nodes"])
        grp["residential"] = sum(1 for n in grp["nodes"] if n["residential"] == "residential")
        grp["datacenter"] = sum(1 for n in grp["nodes"] if n["residential"] == "datacenter")
        grp["nodes"].sort(key=lambda n: (n.get("latency_ms") is None, n.get("latency_ms") or 0, n["host"]))
        by_country[name] = grp

    data = {"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"), "source": source, "worker": WORKER_CHECK_URL, "stats": stats, "countries": by_country, "available": available}
    return data

# edgetunnel 入口地址池：静态列表仅作回退，不再是唯一来源
EDGE_HOSTS = [
    h.strip()
    for h in os.environ.get(
        "EDGE_HOSTS",
        "vps.cheng2001.top:443,api.gzcrtw.com:443,cf-cname.xingpingcn.top:443,"
        "p.etime.vip:443,cf.3666888.xyz:443,www.mlkj888.com:443,www.galgamex.net:443,"
        "tt.78607323.xyz:443,bbs.alipansou.com:443,cf.itv888.cn:443,cf.nyanya.moe:443",
    ).split(",")
    if h.strip()
]

NODES_URL = os.environ.get("NODES_URL", "https://yue1616.github.io/jiakuan1/nodes.txt")



CLOUDFLARE_IPV4_RANGES_URL = "https://www.cloudflare.com/ips-v4"
CLOUDFLARE_IPV6_RANGES_URL = "https://www.cloudflare.com/ips-v6"


def fetch_cloudflare_networks():
    """从 Cloudflare 官方发布的 IPv4/IPv6 清单加载代理网段。"""
    networks = []
    urls = (CLOUDFLARE_IPV4_RANGES_URL, CLOUDFLARE_IPV6_RANGES_URL)
    for url in urls:
        try:
            response = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0 (compatible; gate-edge-checker)"})
            response.raise_for_status()
            added = 0
            for line in response.text.splitlines():
                value = line.strip().split("#", 1)[0].strip()
                if not value:
                    continue
                try:
                    networks.append(ipaddress.ip_network(value, strict=False))
                    added += 1
                except ValueError:
                    continue
            log("CF RANGES", f"官方清单 {url}：读取 {added} 个网段")
        except Exception as exc:
            log("CF RANGES", f"官方清单读取失败 ({url}): {type(exc).__name__}: {exc}")
    if not networks:
        log("CF RANGES", "无法读取 Cloudflare 官方网段；本轮不接受未经核验的 API IP 候选")
        return []
    return networks


def filter_cloudflare_ip_candidates(candidates):
    """只保留落在 Cloudflare 官方公布网段内的 IP 字面量；域名候选不作为 API IP 使用。"""
    networks = fetch_cloudflare_networks()
    if not networks:
        return []

    accepted = []
    seen = set()
    non_ip = 0
    outside = 0
    for candidate in candidates or []:
        endpoint = normalize_edge_endpoint(candidate)
        if not endpoint or endpoint in seen:
            continue
        seen.add(endpoint)
        if endpoint.startswith("["):
            close = endpoint.find("]")
            host = endpoint[1:close]
        else:
            host = endpoint.rsplit(":", 1)[0]
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            non_ip += 1
            continue
        if any(address.version == network.version and address in network for network in networks):
            accepted.append(endpoint)
        else:
            outside += 1

    log("CF RANGES", f"API 候选校验：Cloudflare 官方网段内 {len(accepted)} 个，网段外剔除 {outside} 个，非 IP 格式跳过 {non_ip} 个")
    return accepted


def normalize_edge_endpoint(endpoint):
    """将 API 的 IP:端口 / 域名:端口规范化；无端口时默认 443。"""
    text = str(endpoint or "").split("#", 1)[0].strip()
    if not text:
        return None

    host = ""
    port = 443
    if text.startswith("["):
        match = re.match(r"^\[([^\]]+)\](?::(\d+))?$", text)
        if not match:
            return None
        host = match.group(1).strip()
        if match.group(2):
            port = int(match.group(2))
    elif text.count(":") == 1:
        maybe_host, maybe_port = text.rsplit(":", 1)
        if not maybe_port.isdigit():
            return None
        host, port = maybe_host.strip(), int(maybe_port)
    elif text.count(":") > 1:
        # 未加方括号的 IPv6 地址，按无端口地址处理
        host, port = text, 443
    else:
        host = text

    host = host.strip().strip("[]")
    if not host or not (1 <= int(port) <= 65535):
        return None
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def fetch_optimal_api_endpoints(api_urls_str):
    """从优选 API 拉取候选入口，剥离 # 之后的备注，并去重。"""
    urls = [u.strip() for u in (api_urls_str or "").split(",") if u.strip()]
    endpoints = []
    seen = set()
    for url in urls:
        try:
            log("OPTIMAL API", f"请求优选 API: {url}")
            response = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
            count = 0
            for line in response.text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                endpoint = normalize_edge_endpoint(line)
                if endpoint and endpoint not in seen:
                    seen.add(endpoint)
                    endpoints.append(endpoint)
                    count += 1
            log("OPTIMAL API", f"本 API 新增候选入口 {count} 个")
        except Exception as exc:
            log("OPTIMAL API", f"拉取失败 ({url}): {type(exc).__name__}: {exc}")
    log("OPTIMAL API", f"去重后候选入口共 {len(endpoints)} 个")
    return endpoints


def probe_edge_endpoint(endpoint):
    """用 TCP connect 测量入口可达性与建连耗时；不是端到端 VPN 测速。"""
    normalized = normalize_edge_endpoint(endpoint)
    if not normalized:
        return None
    parsed = normalize_edge_endpoint(normalized)
    if not parsed:
        return None

    if parsed.startswith("["):
        close = parsed.find("]")
        host = parsed[1:close]
        port = int(parsed[close + 2:])
    else:
        host, port_text = parsed.rsplit(":", 1)
        port = int(port_text)

    timings = []
    for _ in range(EDGE_TEST_REPEATS):
        started = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=EDGE_TEST_TIMEOUT):
                timings.append((time.perf_counter() - started) * 1000)
        except (OSError, TimeoutError):
            continue
    if not timings:
        return None
    timings.sort()
    mid = len(timings) // 2
    latency = timings[mid] if len(timings) % 2 else (timings[mid - 1] + timings[mid]) / 2
    return normalized, latency


def rank_edge_hosts(candidates, label):
    """并发探测候选入口，按 TCP 建连耗时从低到高排序。"""
    unique = []
    seen = set()
    for candidate in candidates or []:
        endpoint = normalize_edge_endpoint(candidate)
        if endpoint and endpoint not in seen:
            seen.add(endpoint)
            unique.append(endpoint)
    if not unique:
        log("EDGE TEST", f"{label}: 没有候选入口")
        return []

    scored = []
    with ThreadPoolExecutor(max_workers=min(EDGE_TEST_CONCURRENCY, len(unique))) as pool:
        futures = [pool.submit(probe_edge_endpoint, endpoint) for endpoint in unique]
        for future in as_completed(futures):
            try:
                item = future.result()
                if item:
                    scored.append(item)
            except Exception as exc:
                log("EDGE TEST", f"探测异常: {type(exc).__name__}: {exc}")
    scored.sort(key=lambda item: item[1])
    log("EDGE TEST", f"{label}: 候选 {len(unique)} 个，TCP 可连接 {len(scored)} 个")
    for endpoint, latency in scored[:min(8, len(scored))]:
        log("EDGE TEST", f"  {endpoint}  {latency:.1f} ms")
    return [endpoint for endpoint, _ in scored]


def get_combined_edge_hosts():
    """优先从 API 拉取候选并实测排序；API 不可用时回退静态入口。"""
    manual = os.environ.get("HOSTS_ENTRY", "").strip()
    if manual:
        hosts = []
        seen = set()
        for item in manual.split(","):
            endpoint = normalize_edge_endpoint(item)
            if endpoint and endpoint not in seen:
                seen.add(endpoint)
                hosts.append(endpoint)
        if hosts:
            log("EDGE POOL", f"使用 HOSTS_ENTRY 手动指定的 {len(hosts)} 个入口（跳过自动筛选）")
            return hosts

    api_hosts = fetch_optimal_api_endpoints(OPTIMAL_API)
    verified_api_hosts = filter_cloudflare_ip_candidates(api_hosts)
    ranked_api = rank_edge_hosts(verified_api_hosts, "Cloudflare 官方网段内 API 候选")
    if ranked_api:
        selected = ranked_api[:EDGE_MAX_SELECTED]
        log("EDGE POOL", f"自动选出 {len(selected)} 个可连接入口（经官方网段验证 {len(verified_api_hosts)} 个，TCP 测试 {len(verified_api_hosts)} 个）")
        return selected

    log("EDGE POOL", "没有可用的官方网段内 API IP，将测试手动维护的静态域名回退池")
    static_hosts = [endpoint for endpoint in (normalize_edge_endpoint(h) for h in EDGE_HOSTS) if endpoint]
    ranked_static = rank_edge_hosts(static_hosts, "静态回退池")
    if ranked_static:
        selected = ranked_static[:EDGE_MAX_SELECTED]
        log("EDGE POOL", f"API 候选不可连接，改用 {len(selected)} 个可连接的静态回退入口")
        return selected

    # 不把未通过官方网段验证或未通过 TCP 探测的 API IP 当作优选入口。
    # 如静态回退池也全部不可连接，返回空列表，让主流程停止发布，避免生成误导性节点。
    log("EDGE POOL", "错误：自动候选与静态回退池均未找到可连接入口；拒绝使用未验证 IP")
    return []

def build_nodes_text(data, edge_hosts=None):
    """生成纯节点行版本 (无注释): 每行 = 入口地址#名字$sstp://..."""
    countries = data["countries"]
    edge = [e for e in (edge_hosts or EDGE_HOSTS) if e]
    if not edge:
        edge = EDGE_HOSTS
    lines = []
    idx = 0
    ordered = sorted(countries.items(), key=lambda kv: (-int(kv[1].get("count") or 0), str(kv[1].get("code") or kv[0])))
    for cname, grp in ordered:
        code = str(grp.get("code") or "?").upper()
        zh = COUNTRY_ZH.get(code) or (code if code and code != "?" else cname)
        nodes = sorted(grp["nodes"], key=lambda n: (0 if n.get("residential") == "residential" else 1, n.get("latency_ms") is None, n.get("latency_ms") or 0, n.get("host") or ""))
        res_nodes = [n for n in nodes if n.get("residential") == "residential"]
        dc_nodes = [n for n in nodes if n.get("residential") != "residential"]
        for i, n in enumerate(res_nodes, 1):
            entry = edge[idx % len(edge)]
            idx += 1
            lines.append(f"{entry}#{zh}-住宅-{i:02d}$sstp://vpn:vpn@{n['host']}:{n['port']}")
        for i, n in enumerate(dc_nodes, 1):
            entry = edge[idx % len(edge)]
            idx += 1
            lines.append(f"{entry}#{zh}-机房-{i:02d}$sstp://vpn:vpn@{n['host']}:{n['port']}")
    return "\n".join(lines) + "\n"

def write_outputs(data, edge_hosts=None):
    os.makedirs(PUBLIC_DIR, exist_ok=True)
    data_path = os.path.join(PUBLIC_DIR, "data.json")
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    html_path = os.path.join(PUBLIC_DIR, "index.html")
    if os.path.exists(TEMPLATE_HTML):
        with open(TEMPLATE_HTML, "r", encoding="utf-8") as f:
            html = f.read()
    else:
        html = ("<html><head><meta charset='utf-8'><title>VPN Gate SSTP 节点</title></head>"
                "<body><h1>VPN Gate SSTP 节点</h1><pre id='out'></pre></body>"
                "<script>fetch('data.json').then(r=>r.json()).then(d=>out.textContent=JSON.stringify(d.stats)).catch(e=>out.textContent='加载失败:'+e)</script></html>")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    nodes_path = os.path.join(PUBLIC_DIR, "nodes.txt")
    with open(nodes_path, "w", encoding="utf-8") as f:
        f.write(build_nodes_text(data, edge_hosts))

    return data_path, html_path, nodes_path

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    session = requests.Session()
    rows, source = fetch_vpngate()
    raw_count = len(rows)
    if raw_count == 0:
        die("VPN Gate 返回 0 个原始节点 (数据源异常, 不允许生成空结果)")

    sstp_nodes = to_sstp_nodes(rows)
    sstp_count = len(sstp_nodes)
    if sstp_count == 0:
        die(f"从 {raw_count} 个原始节点中没有解析出任何 SSTP(TCP) 节点 — 数据格式可能已变化, 需要人工适配")
    uniq = dedupe(sstp_nodes)

    if MAX_CHECK_NODES > 0:
        uniq = uniq[:MAX_CHECK_NODES]

    log("VPN GATE", f"获取原始节点: {raw_count}")
    log("VPN GATE", f"SSTP 节点: {sstp_count}")
    log("VPN GATE", f"去重后: {len(uniq)}")

    log("CLOUDFLARE WORKER", f"提交检测: {len(uniq)} (并发 {CONCURRENCY}, 单请求超时 {CHECK_TIMEOUT}s)")
    t0 = time.time()
    results = check_all(uniq, session)
    elapsed = time.time() - t0

    success = [r for r in results if r.get("success")]
    failed = [r for r in results if not r.get("success")]
    worker_errors = [r for r in failed if r.get("worker_error")]

    log("CLOUDFLARE WORKER", f"检测成功: {len(success)}")
    log("CLOUDFLARE WORKER", f"检测失败: {len(failed)}" + (f" (其中 Worker 异常 {len(worker_errors)})" if worker_errors else ""))
    log("CLOUDFLARE WORKER", f"耗时: {elapsed:.1f}s")

    if uniq and not success:
        if len(worker_errors) == len(uniq):
            die("Worker 全部请求异常或未能解析节点主机名 — 本次运行判定失败，不发布空结果")
        die("本轮没有检测成功的 SSTP 节点；为避免覆盖上一轮有效 nodes.txt，本次不发布新结果")

    data = build_outputs(results, raw_count, sstp_count, source)
    log("RESULT", f"可用节点: {len(success)}")
    log("RESULT", f"国家数量: {data['stats']['countries']}")

    edge_hosts = get_combined_edge_hosts()
    if not edge_hosts:
        die("没有可用的入口候选地址，拒绝生成不完整的 nodes.txt")
    log("EDGE POOL", f"本轮用于生成 nodes.txt 的入口数: {len(edge_hosts)}")

    data_path, html_path, nodes_path = write_outputs(data, edge_hosts)
    log("WEBSITE", f"生成 {os.path.relpath(data_path, REPO_DIR)}")
    log("WEBSITE", f"生成 {os.path.relpath(html_path, REPO_DIR)}")
    log("WEBSITE", f"生成 {os.path.relpath(nodes_path, REPO_DIR)}")
    log("USAGE", f"自动轮换: 把 {NODES_URL} 填入 edgetunnel 后台「自定义优选IP」框；入口优选在每次工作流运行时自动更新")
    log("WEBSITE", "完成 (GitHub Pages 部署由 workflow 执行)")

if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        die(f"程序异常: {type(exc).__name__}: {exc}")
