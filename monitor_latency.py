#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, time, sys, subprocess, socket, datetime, json
import urllib.request, urllib.parse, urllib.error
import oci

def log(msg): print(msg, flush=True)

# --------- 实用函数 ---------
def env_float(name, default):
    v = os.environ.get(name, '')
    try: return float(v) if str(v).strip() != '' else default
    except Exception: return default

def env_int(name, default):
    v = os.environ.get(name, '')
    try: return int(v) if str(v).strip() != '' else default
    except Exception: return default

def now_ts():
    return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

# TCP 探测（当 ICMP 被挡时用于辅助判断连通性）
def tcp_ping(ip, port=22, timeout=2):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False

# ICMP 平均延迟
def measure_latency(ip: str, count: int = 5, timeout_s: int = 2) -> float | None:
    try:
        # -n 纯数字；-W 超时秒；-c 次数
        cmd = ["ping", "-n", "-W", str(timeout_s), "-c", str(count), ip]
        r = subprocess.run(cmd, capture_output=True, text=True)
        out = (r.stdout or "") + "\n" + (r.stderr or "")
        m = re.search(r"=\s*[\d\.]+/([\d\.]+)/", out) or re.search(r"avg[/=]\s*([\d\.]+)", out)
        return float(m.group(1)) if m else None
    except Exception:
        return None

def build_config_from_env():
    need = ["OCI_CLI_USER","OCI_CLI_TENANCY","OCI_CLI_REGION","OCI_CLI_FINGERPRINT","OCI_CLI_KEY_CONTENT","OCI_INSTANCE_ID"]
    missing = [k for k in need if not os.environ.get(k)]
    if missing:
        log(f"❌ 缺少环境变量：{', '.join(missing)}")
        sys.exit(2)

    key_dir  = os.path.expanduser("~/.oci")
    os.makedirs(key_dir, exist_ok=True)
    key_file = os.path.join(key_dir, "oci_api_key.pem")
    with open(key_file, "w") as f:
        f.write(os.environ["OCI_CLI_KEY_CONTENT"])
    try: os.chmod(key_file, 0o600)
    except Exception: pass

    cfg = {
        "user":        os.environ["OCI_CLI_USER"],
        "tenancy":     os.environ["OCI_CLI_TENANCY"],
        "region":      os.environ["OCI_CLI_REGION"],
        "fingerprint": os.environ["OCI_CLI_FINGERPRINT"],
        "key_file":    key_file,
    }
    if os.environ.get("OCI_CLI_PASSPHRASE"):
        cfg["pass_phrase"] = os.environ["OCI_CLI_PASSPHRASE"]
    return cfg, os.environ["OCI_INSTANCE_ID"]

# ------- OCI 网络对象辅助 -------
def pick_primary_vnic(compute, net, compartment_id, instance_id):
    vas = compute.list_vnic_attachments(compartment_id, instance_id=instance_id).data
    if not vas: log("❌ 未找到 VNIC"); sys.exit(2)
    for va in vas:
        v = net.get_vnic(va.vnic_id).data
        if getattr(v, "is_primary", False): return va.vnic_id
    return vas[0].vnic_id

def pick_primary_private_ip(net, vnic_id):
    pips = net.list_private_ips(vnic_id=vnic_id).data
    if not pips: log("❌ 未找到私网 IP"); sys.exit(2)
    for p in pips:
        if getattr(p, "is_primary", False): return p.id
    return pips[0].id

def get_public_ip_obj_by_private(net, private_ip_id):
    try:
        d = oci.core.models.GetPublicIpByPrivateIpIdDetails(private_ip_id=private_ip_id)
        return net.get_public_ip_by_private_ip_id(d).data
    except oci.exceptions.ServiceError:
        return None

def wait_assigned(net, pub_id, tries=20, sleep_s=1.0):
    for _ in range(tries):
        time.sleep(sleep_s)
        obj = net.get_public_ip(public_ip_id=pub_id).data
        if obj.lifecycle_state == "ASSIGNED": return obj
    return net.get_public_ip(public_ip_id=pub_id).data

def ensure_ephemeral_attached(net, compartment_id, private_ip_id):
    obj = get_public_ip_obj_by_private(net, private_ip_id)
    if obj and obj.lifetime == "EPHEMERAL": return obj
    if obj and obj.lifetime == "RESERVED":
        log(f"ℹ️ 发现 RESERVED 公网 IP（{obj.ip_address}），先解绑再换临时 IP。")
        net.update_public_ip(public_ip_id=obj.id,
                             update_public_ip_details=oci.core.models.UpdatePublicIpDetails(private_ip_id=None))
        time.sleep(2)
    c = oci.core.models.CreatePublicIpDetails(
        compartment_id=compartment_id, private_ip_id=private_ip_id, lifetime="EPHEMERAL"
    )
    new_obj = net.create_public_ip(c).data
    return wait_assigned(net, new_obj.id)

def switch_ephemeral_ip(net, compartment_id, private_ip_id, old_obj, backoff_s=0):
    try:
        if old_obj and getattr(old_obj, "lifetime", "") == "EPHEMERAL":
            net.delete_public_ip(public_ip_id=old_obj.id)
            time.sleep(3)
        elif old_obj and getattr(old_obj, "lifetime", "") == "RESERVED":
            net.update_public_ip(public_ip_id=old_obj.id,
                                 update_public_ip_details=oci.core.models.UpdatePublicIpDetails(private_ip_id=None))
            time.sleep(2)
    except oci.exceptions.ServiceError as e:
        log(f"⚠️ 删除/解绑旧 IP 出错：{e.message}（忽略继续）")
    if backoff_s > 0:
        time.sleep(backoff_s)
    c = oci.core.models.CreatePublicIpDetails(
        compartment_id=compartment_id, private_ip_id=private_ip_id, lifetime="EPHEMERAL"
    )
    new_obj = net.create_public_ip(c).data
    return wait_assigned(net, new_obj.id)

# ------- Telegram -------
def tg_send_message(token, chat_id, text, parse_mode="Markdown"):
    if not token or not chat_id: return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": text[:3900],  # 预防过长
            "parse_mode": parse_mode
        }).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as e:
        log(f"⚠️ 发送 Telegram 失败：{e}")

# ------- 主逻辑 -------
def main():
    cfg, instance_id = build_config_from_env()
    compute = oci.core.ComputeClient(cfg)
    net     = oci.core.VirtualNetworkClient(cfg)

    inst = compute.get_instance(instance_id).data
    comp = inst.compartment_id

    vnic_id = pick_primary_vnic(compute, net, comp, instance_id)
    primary_private_ip_id = pick_primary_private_ip(net, vnic_id)

    threshold     = env_float("LATENCY_THRESHOLD_MS", 80.0)
    ping_count    = env_int("PING_COUNT", 5)
    per_pkt_tout  = env_int("PING_TIMEOUT_S", 2)
    max_switches  = env_int("MAX_SWITCHES", 12)
    sleep_between = env_int("SLEEP_BETWEEN_SWITCH_S", 6)  # 建议 5~10 秒
    tcp_fallback_port = env_int("TCP_FALLBACK_PORT", 22)  # ICMP 不通时测 TCP 连通性

    tg_token = os.environ.get("TG_BOT_TOKEN", "")
    tg_chat  = os.environ.get("TG_CHAT_ID", "")

    trials = []  # 记录每次尝试

    pub = ensure_ephemeral_attached(net, comp, primary_private_ip_id)
    current_ip = pub.ip_address
    log(f"起始公网 IP：{current_ip}（{pub.lifetime}）")

    switches = 0
    success = False
    while True:
        log(f"\n=== 测试 {current_ip} （{ping_count} 次）===")
        avg = measure_latency(current_ip, count=ping_count, timeout_s=per_pkt_tout)
        reachable_tcp = False
        if avg is None:
            # ICMP 不通时，顺便探测下 TCP（端口可按需改）
            reachable_tcp = tcp_ping(current_ip, port=tcp_fallback_port, timeout=2)

        trials.append({
            "time": now_ts(),
            "ip": current_ip,
            "avg_ms": avg,
            "icmp_ok": avg is not None,
            "tcp_ok": reachable_tcp
        })

        if avg is not None:
            log(f"📊 平均延迟：{avg:.2f} ms（阈值 {threshold} ms）")
            if avg < threshold:
                log(f"✅ 达标：{current_ip}  平均 {avg:.2f} ms")
                success = True
                break
        else:
            log("❌ ping 不可达或被过滤。")

            # 连 TCP 也不通，八成是安全组/防火墙挡了；别无脑换到天荒地老
            if not reachable_tcp:
                log("⚠️ TCP 探测也失败（可能安全组/NSG/实例防火墙未放行）。建议先检查网络策略。")
                # 可选择直接退出；这里继续按你的原逻辑小规模再试
                # break

        if switches >= max_switches:
            log(f"❌ 超过最大更换次数（{max_switches}），停止。")
            break

        switches += 1
        backoff = min(10, 2 + switches // 3)  # 轻微退避，配合 sleep_between
        log(f"⏩ 第 {switches} 次更换临时公网 IP …")
        pub = switch_ephemeral_ip(net, comp, primary_private_ip_id, pub, backoff_s=backoff)
        current_ip = pub.ip_address
        log(f"🆕 新 IP：{current_ip}（状态 {pub.lifecycle_state}）")
        time.sleep(sleep_between)

    # ------- 汇总 & 推送 -------
    final_ip = trials[-1]["ip"] if trials else "N/A"
    final_avg = trials[-1]["avg_ms"] if trials else None
    ok = "✅ 成功" if success else "❌ 失败"

    # 组装简洁文本（最多列出最近 15 条）
    lines = [
        f"*OCI 延迟测试结果* {ok}",
        f"区域: `{cfg['region']}`",
        f"实例: `{instance_id[:14]}…`",
        f"阈值: {threshold} ms",
        f"尝试: {len(trials)} 次",
        f"最终IP: `{final_ip}`" + (f"  平均: {final_avg:.2f} ms" if final_avg is not None else "  （不可达）"),
        "",
        "*明细(最近 15 条)*"
    ]
    for t in trials[-15:]:
        stat = "OK" if t["avg_ms"] is not None else ("TCP" if t["tcp_ok"] else "DOWN")
        avgtxt = f"{t['avg_ms']:.2f}ms" if t["avg_ms"] is not None else "-"
        lines.append(f"`{t['ip']}`  {avgtxt}  {stat}  {t['time']}")

    tg_text = "\n".join(lines)
    tg_send_message(tg_token, tg_chat, tg_text)

    # 也把完整明细保存到工作目录（便于调试/归档）
    with open("oci_latency_trials.json", "w") as f:
        json.dump(trials, f, ensure_ascii=False, indent=2)

    # 成功返回 0，失败返回 1（方便 Actions 显示状态）
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()

