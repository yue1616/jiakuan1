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
