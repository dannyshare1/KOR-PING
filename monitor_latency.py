#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, time, sys, subprocess
import oci

def log(msg):
    print(msg, flush=True)

def measure_latency(ip, count=5, timeout_s=2):
    """用 ping 测平均延迟(ms)，不可达返回 None。"""
    try:
        cmd = ["ping", "-n", "-W", str(timeout_s), "-c", str(count), ip]
        r = subprocess.run(cmd, capture_output=True, text=True)
        out = (r.stdout or "") + "\n" + (r.stderr or "")
        # 适配两种常见输出
        m = re.search(r"=\s*[\d\.]+/([\d\.]+)/", out) or re.search(r"avg[/=]\s*([\d\.]+)", out)
        return float(m.group(1)) if m else None
    except Exception:
        return None

def build_cfg_from_env():
    need = ["OCI_CLI_USER","OCI_CLI_TENANCY","OCI_CLI_REGION",
            "OCI_CLI_FINGERPRINT","OCI_CLI_KEY_CONTENT","OCI_INSTANCE_ID"]
    missing = [k for k in need if not os.environ.get(k)]
    if missing:
        log("❌ 缺少环境变量：" + ", ".join(missing))
        sys.exit(2)

    key_dir = os.path.expanduser("~/.oci")
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

def pick_primary_vnic(compute, net, compartment_id, instance_id):
    vas = compute.list_vnic_attachments(compartment_id, instance_id=instance_id).data
    if not vas:
        log("❌ 未找到 VNIC"); sys.exit(2)
    # 挑 is_primary 的，没有就第一个
    for va in vas:
        v = net.get_vnic(va.vnic_id).data
        if getattr(v, "is_primary", False):
            return va.vnic_id
    return vas[0].vnic_id

def pick_primary_private_ip(net, vnic_id):
    pips = net.list_private_ips(vnic_id=vnic_id).data
    if not pips:
        log("❌ 未找到私网 IP"); sys.exit(2)
    for p in pips:
        if getattr(p, "is_primary", False):
            return p.id
    return pips[0].id

def get_pub_by_private(net, private_ip_id):
    try:
        d = oci.core.models.GetPublicIpByPrivateIpIdDetails(private_ip_id=private_ip_id)
        return net.get_public_ip_by_private_ip_id(d).data
    except oci.exceptions.ServiceError:
        return None

def wait_assigned(net, pub_id, tries=15, sleep_s=2.0):
    for _ in range(tries):
        time.sleep(sleep_s)
        obj = net.get_public_ip(public_ip_id=pub_id).data
        if obj.lifecycle_state == "ASSIGNED":
            return obj
    return net.get_public_ip(public_ip_id=pub_id).data

def ensure_ephemeral(net, compartment_id, private_ip_id):
    """确保挂的是 EPHEMERAL 公网 IP；没有则创建；如是 RESERVED 就先解绑再创建。"""
    obj = get_pub_by_private(net, private_ip_id)
    if obj and obj.lifetime == "EPHEMERAL":
        return obj
    if obj and obj.lifetime == "RESERVED":
        net.update_public_ip(public_ip_id=obj.id,
            update_public_ip_details=oci.core.models.UpdatePublicIpDetails(private_ip_id=None))
        time.sleep(2)
    c = oci.core.models.CreatePublicIpDetails(
        compartment_id=compartment_id, private_ip_id=private_ip_id, lifetime="EPHEMERAL"
    )
    new_obj = net.create_public_ip(c).data
    return wait_assigned(net, new_obj.id)

def switch_ephemeral(net, compartment_id, private_ip_id, old_obj):
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
    c = oci.core.models.CreatePublicIpDetails(
        compartment_id=compartment_id, private_ip_id=private_ip_id, lifetime="EPHEMERAL"
    )
    new_obj = net.create_public_ip(c).data
    return wait_assigned(net, new_obj.id)

def main():
    cfg, instance_id = build_cfg_from_env()
    compute = oci.core.ComputeClient(cfg)
    net     = oci.core.VirtualNetworkClient(cfg)

    inst = compute.get_instance(instance_id).data
    comp = inst.compartment_id
    vnic_id = pick_primary_vnic(compute, net, comp, instance_id)
    primary_private_ip_id = pick_primary_private_ip(net, vnic_id)

    threshold     = float(os.environ.get("LATENCY_THRESHOLD_MS", "80"))
    ping_count    = int(os.environ.get("PING_COUNT", "5"))
    per_pkt_tout  = int(os.environ.get("PING_TIMEOUT_S", "2"))
    max_switches  = int(os.environ.get("MAX_SWITCHES", "25"))

    pub = ensure_ephemeral(net, comp, primary_private_ip_id)
    current_ip = pub.ip_address
    log(f"起始公网 IP：{current_ip}（{pub.lifetime}）")

    switches = 0
    while True:
        log(f"\n=== 测试 {current_ip} （{ping_count} 次）===")
        avg = measure_latency(current_ip, count=ping_count, timeout_s=per_pkt_tout)
        if avg is None:
            log("❌ ping 不可达或解析失败。")
        else:
            log(f"📊 平均延迟：{avg:.2f} ms（阈值 {threshold} ms）")

        if avg is not None and avg < threshold:
            log(f"✅ 达标：{current_ip}  平均 {avg:.2f} ms")
            break

        if switches >= max_switches:
            log(f"❌ 超过最大更换次数（{max_switches}），停止。")
            sys.exit(1)

        switches += 1
        log(f"⏩ 第 {switches} 次更换临时公网 IP …")
        pub = switch_ephemeral(net, comp, primary_private_ip_id, pub)
        current_ip = pub.ip_address
        log(f"🆕 新 IP：{current_ip}（状态 {pub.lifecycle_state}）")
        time.sleep(3)

if __name__ == "__main__":
    main()
