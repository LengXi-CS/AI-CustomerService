"""热加载 frpc 配置并验证隧道端到端。"""
import re
import subprocess
import time
import urllib.error
import urllib.request


def run(args, timeout=40):
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout)
        out = (p.stdout or b"").decode("utf-8", "replace") + (p.stderr or b"").decode("utf-8", "replace")
        return p.returncode, out.strip()
    except Exception as e:  # noqa: BLE001
        return -1, f"<error {e}>"


def sect(t):
    print("\n" + "=" * 66)
    print(t)
    print("=" * 66)


sect("1. 校验改后的 frpc.toml")
rc, out = run(["docker", "exec", "frpc", "/usr/bin/frpc", "verify", "-c", "/etc/frp/frpc.toml"])
print(f"  exit={rc}")
print("  " + out)

sect("2. 热加载（不重启容器）")
rc, out = run(["docker", "exec", "frpc", "/usr/bin/frpc", "reload", "-c", "/etc/frp/frpc.toml"])
print(f"  exit={rc}  {out}")

time.sleep(4)

sect("3. 热加载后日志")
rc, out = run(["docker", "logs", "frpc", "--since", "90s"])
print("\n".join("  " + ln for ln in out.splitlines()) if out else "  <近 90 秒无新日志>")

sect("4. 走公网隧道取 /health")
for url in ["http://115.190.250.104:9481/health",
            "http://115.190.250.104:9481/api/wechat/callback"]:
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=12) as r:
                body = r.read(300).decode("utf-8", "replace")
                print(f"  [{attempt}] {url}\n      -> HTTP {r.status}  {re.sub(chr(92)+'s+', ' ', body)[:200]}")
                break
        except urllib.error.HTTPError as e:
            body = e.read(200).decode("utf-8", "replace")
            print(f"  [{attempt}] {url} -> HTTP {e.code}  {re.sub(chr(92)+'s+', ' ', body)[:160]}")
            break
        except Exception as e:  # noqa: BLE001
            print(f"  [{attempt}] {url} -> 失败 ({type(e).__name__}: {e})")
            if attempt < 3:
                time.sleep(3)

sect("5. 容器状态复核")
rc, out = run(["docker", "ps", "--filter", "name=frpc", "--format",
               "{{.Names}}  {{.Status}}  {{.Image}}"])
print("  " + out)
rc, out = run(["docker", "logs", "frpc", "--tail", "12"])
print("  --- frpc 最新日志 ---")
print("\n".join("  " + ln for ln in out.splitlines()) if out else "  <空>")
