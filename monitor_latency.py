#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, time, sys, subprocess
import oci

def log(msg: str): print(msg, flush=True)

def measure_latency(ip: str, count: int = 5, timeout_s: int = 2) -> float | None:
    """
    用系统 ping 测平均延迟（ms）。不可达返回 None。
    """
    try:
        # -n 纯数字输出；-W 每包超时秒；-c 次数
        cmd = ["ping", "-n", "-W", str(timeout_s), "-c", str(count), ip]
        r = subprocess.run(cmd, capture_output=True, text=True)
        out = r.stdout + "\n" + r.stderr
        # 常见两种格式都匹配：
        # rtt min/avg/max/mdev = 10.123/20.456/...
        m = re.search(r"=\s*[\d\.]+/([\d\.]+)/", out)
        if not m:
            # busybox: round-trip min/avg/max = 10.1/20.4/...
            m = re.search(r"avg[/=]\s*([\d\.]+)", out)
        return float(m.group(1)) if m else None
    except Exception:
        return None

def build_config_from_env() -> tuple[dict, str]:
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
    try:
        os.chmod(key_file, 0o600)
    except Exception:
        pass

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

def pick_primary_vnic(compute: oci.core.ComputeClient,
                      net: oci.core.VirtualNetworkClient,
                      compartment_id: str, instance_id: str) -> str:
    vas = compute.list_vnic_attachments(compartment_id, instance_id=instance_id).data
    if not vas: 
        log("❌ 未找到实例的 VNIC。"); sys.exit(2)
    # 选 is_primary 的 VNIC；没有标记就取第一个
    for va in vas:
        v = net.get_vnic(va.vnic_id).data
        if getattr(v, "is_primary", False):
            return va.vnic_id
    return vas[0].vnic_id

def pick_primary_private_ip(net: oci.core.VirtualNetworkClient, vnic_id: str) -> str:
    pips = net.list_private_ips(vnic_id=vnic_id).data
    if not pips:
        log("❌ 未找到主私网 IP。"); sys.exit(2)
    for p in pips:
        if getattr(p, "is_primary", False):
            return p.id
    return pips[0].id

def get_public_ip_obj_by_private(net: oci.core.VirtualNetworkClient, private_ip_id: str):
    try:
        d = oci.core.models.GetPublicIpByPrivateIpIdDetails(private_ip_id=private_ip_id)
        return net.get_public_ip_by_private_ip_id(d).data
    except oci.exceptions.ServiceError:
        return None

def wait_assigned(net: oci.core.VirtualNetworkClient, pub_id: str, tries: int = 15, sleep_s: float = 2.0):
    for _ in range(tries):
        time.sleep(sleep_s)
        obj = net.get_public_ip(public_ip_id=pub_id).data
        if obj.lifecycle_state == "ASSIGNED":
            return obj
    return net.get_public_ip(public_ip_id=pub_id).data

def ensure_ephemeral_attached(net, compartment_id: str, private_ip_id: str):
    """
    确保私网 IP 上挂的是 EPHEMERAL 公网 IP。
    若无公网 IP → 创建临时 IP；
    若挂的是 RESERVED → 先解绑，再创建临时 IP。
    """
    obj = get_public_ip_obj_by_private(net, private_ip_id)
    if obj and obj.lifetime == "EPHEMERAL":
        return obj
    if obj and obj.lifetime == "RESERVED":
        log(f"ℹ️ 发现 RESERVED 公网 IP（{obj.ip_address}），先解绑再换临时 IP。")
        net.update_public_ip(public_ip_id=obj.id, update_public_ip_details=oci.core.models.UpdatePublicIpDetails(private_ip_id=None))
        time.sleep(2)
    # 创建新的 EPHEMERAL
    c = oci.core.models.CreatePublicIpDetails(
        compartment_id=compartment_id, private_ip_id=private_ip_id, lifetime="EPHEMERAL"
    )
    new_obj = net.create_public_ip(c).data
    new_obj = wait_assigned(net, new_obj.id)
    return new_obj

def switch_ephemeral_ip(net, compartment_id: str, private_ip_id: str, old_obj):
    """
    删除旧 EPHEMERAL，再创建新的 EPHEMERAL。
    若 old_obj 为 RESERVED（理论上不会进来），先解绑。
    """
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
    new_obj = wait_assigned(net, new_obj.id)
    return new_obj

def main():
    cfg, instance_id = build_config_from_env()
    compute = oci.core.ComputeClient(cfg)
    net     = oci.core.VirtualNetworkClient(cfg)

    inst = compute.get_instance(instance_id).data
    comp = inst.compartment_id

    vnic_id = pick_primary_vnic(compute, net, comp, instance_id)
    primary_private_ip_id = pick_primary_private_ip(net, vnic_id)

    # 参数
    threshold = float(os.environ.get("LATENCY_THRESHOLD_MS", "80"))
    ping_count = int(os.environ.get("PING_COUNT", "5"))
    per_packet_timeout = int(os.environ.get("PING_TIMEOUT_S", "2"))
    max_switches = int(os.environ.get("MAX_SWITCHES", "25"))

    # 确保使用 EPHEMERAL
    pub = ensure_ephemeral_attached(net, comp, primary_private_ip_id)
    current_ip = pub.ip_address
    log(f"起始公网 IP：{current_ip}（{pub.lifetime}）")

    switches = 0
    while True:
        log(f"\n=== 测试 {current_ip} （{ping_count} 次）===")
        avg = measure_latency(current_ip, count=ping_count, timeout_s=per_packet_timeout)
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
        pub = switch_ephemeral_ip(net, comp, primary_private_ip_id, pub)
        current_ip = pub.ip_address
        log(f"🆕 新 IP：{current_ip}（状态 {pub.lifecycle_state}）")
        # 稍等片刻再测，给路由/缓存一点收敛时间
        time.sleep(3)

if __name__ == "__main__":
    main()
