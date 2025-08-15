#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, time, subprocess
import oci  # pip install oci

def measure_latency(ip: str, count: int = 5) -> float | None:
    try:
        # Linux ping：-c 次数，-q 安静输出
        r = subprocess.run(["ping", "-c", str(count), "-q", ip],
                           capture_output=True, text=True, check=False)
        if r.returncode != 0 and not r.stdout:
            return None
        m = re.search(r"rtt [a-z/]+ = [\d\.]+/([\d\.]+)/", r.stdout)
        return float(m.group(1)) if m else None
    except Exception:
        return None

# 读取 OCI 认证（用 GitHub Secrets 注入）
cfg = {
    "user":        os.environ.get("OCI_CLI_USER"),
    "tenancy":     os.environ.get("OCI_CLI_TENANCY"),
    "region":      os.environ.get("OCI_CLI_REGION"),
    "fingerprint": os.environ.get("OCI_CLI_FINGERPRINT"),
}
key_content = os.environ.get("OCI_CLI_KEY_CONTENT")
passphrase  = os.environ.get("OCI_CLI_PASSPHRASE")
instance_id = os.environ.get("OCI_INSTANCE_ID")
if not all(cfg.values()) or not key_content or not instance_id:
    raise SystemExit("缺少 OCI_* 或 OCI_INSTANCE_ID 环境变量。")

# 写私钥到临时文件
os.makedirs(os.path.expanduser("~/.oci"), exist_ok=True)
key_file = os.path.expanduser("~/.oci/oci_api_key.pem")
with open(key_file, "w") as f: f.write(key_content)
cfg["key_file"] = key_file
if passphrase: cfg["pass_phrase"] = passphrase

compute = oci.core.ComputeClient(cfg)
vcn     = oci.core.VirtualNetworkClient(cfg)

# 取实例主 VNIC 的主私网 IP
inst = compute.get_instance(instance_id).data
vas  = compute.list_vnic_attachments(inst.compartment_id, instance_id=instance_id).data
if not vas: raise SystemExit("未找到 VNIC。")
vnic_id = vas[0].vnic_id
pips    = vcn.list_private_ips(vnic_id=vnic_id).data
if not pips: raise SystemExit("未找到私网 IP。")
primary_private_ip_id = pips[0].id

# 拿当前公网 IP（没有就分配 EPHEMERAL）
def get_or_assign_public_ip():
    try:
        g = oci.core.models.GetPublicIpByPrivateIpIdDetails(private_ip_id=primary_private_ip_id)
        obj = vcn.get_public_ip_by_private_ip_id(g).data
        return obj
    except oci.exceptions.ServiceError:
        c = oci.core.models.CreatePublicIpDetails(
            compartment_id=inst.compartment_id,
            private_ip_id=primary_private_ip_id,
            lifetime="EPHEMERAL"
        )
        return vcn.create_public_ip(c).data

pub = get_or_assign_public_ip()
current_ip = pub.ip_address
print(f"起始公网 IP：{current_ip}")

LATENCY_THRESHOLD = float(os.environ.get("LATENCY_THRESHOLD_MS", "80"))
ATTEMPTS = int(os.environ.get("PING_COUNT", "5"))

tries = 0
while True:
    tries += 1
    print(f"\n=== 尝试 #{tries}: 测试 {current_ip} ===")
    avg = measure_latency(current_ip, ATTEMPTS)
    if avg is not None:
        print(f"平均延迟：{avg:.2f} ms  (阈值 {LATENCY_THRESHOLD} ms)")
    else:
        print("Ping 不可达或超时。")

    if avg is not None and avg < LATENCY_THRESHOLD:
        print(f"✅ 满足阈值，结束。最终 IP：{current_ip}，平均 {avg:.2f} ms")
        break

    print("⏩ 更换临时公网 IP ...")
    # 删除旧 EPHEMERAL 公网 IP（等价解绑）
    try:
        vcn.delete_public_ip(pub.id)
        time.sleep(3)
    except oci.exceptions.ServiceError as e:
        print(f"删除旧 IP 出错（可能已不存在）：{e.message}")

    # 分配新 IP 并等待 ASSIGNED
    c = oci.core.models.CreatePublicIpDetails(
        compartment_id=inst.compartment_id,
        private_ip_id=primary_private_ip_id,
        lifetime="EPHEMERAL"
    )
    pub = vcn.create_public_ip(c).data
    for _ in range(10):
        time.sleep(2)
        pub = vcn.get_public_ip(pub.id).data
        if pub.lifecycle_state == "ASSIGNED":
            break
    current_ip = pub.ip_address
    print(f"新公网 IP：{current_ip}")
