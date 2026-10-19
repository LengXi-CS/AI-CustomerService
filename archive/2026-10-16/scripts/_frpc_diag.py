"""看 frpc 容器日志、健康检查、管理口状态。本地操作，不外连。"""
import json
import re
import subprocess
import urllib.request
import base64


def run(args, timeout=45):
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout)
        out = (p.stdout or b"").decode("utf-8", "replace") + (p.stderr or b"").decode("utf-8", "replace")
        return p.returncode, out.strip()
    except Exception as e:  # noqa: BLE001
        return -1, f"<error {e}>"


def sect(t):
    print("\n" + "=" * 68)
    print(t)
    print("=" * 68)


sect("1. frpc 容器日志（最后 80 行）")
rc, out = run(["docker", "logs", "frpc", "--tail", "80"])
print("\n".join("  " + ln for ln in out.splitlines()) if out else "  <空>")

sect("2. frpc 容器状态 / 健康检查详情")
rc, out = run(["docker", "inspect", "frpc", "--format",
               "Image={{.Config.Image}}\nState={{.State.Status}}\n"
               "Health={{if .State.Health}}{{.State.Health.Status}} "
               "failures={{.State.Health.FailingStreak}}{{else}}<no healthcheck>{{end}}\n"
               "NetworkMode={{.HostConfig.NetworkMode}}\nRestart={{.HostConfig.RestartPolicy.Name}}\n"
               "StartedAt={{.State.StartedAt}}"])
print("\n".join("  " + ln for ln in out.splitlines()))

sect("3. 最近一次健康检查的输出")
rc, out = run(["docker", "inspect", "frpc", "--format",
               "{{range .State.Health.Log}}exitcode={{.ExitCode}} out={{.Output}}\n---\n{{end}}"])
print("\n".join("  " + ln for ln in out.splitlines()) if out else "  <空>")

sect("4. 容器内 frpc 进程 / 环境变量（脱敏）")
rc, out = run(["docker", "exec", "frpc", "sh", "-c",
               "ps aux 2>/dev/null | head -10; echo '--- env ---'; "
               "env | grep -i frp_ | sed -E 's/(TOKEN|PWD)=.*/\\1=<已填>/'"])
print("\n".join("  " + ln for ln in out.splitlines()) if out else "  <空>")

sect("5. frpc 本机管理口 127.0.0.1:7400")
envp = r"D:\瀚思AI客服\frc配置\client\.env"
pwd = ""
if __import__("os").path.exists(envp):
    for line in open(envp, encoding="utf-8-sig"):
        if line.strip().upper().startswith("FRP_ADMIN_PWD"):
            pwd = line.split("=", 1)[1].strip()
for path in ["/api/status", "/api/config"]:
    url = "http://127.0.0.1:7400" + path
    try:
        req = urllib.request.Request(url)
        if pwd:
            req.add_header("Authorization",
                           "Basic " + base64.b64encode(f"admin:{pwd}".encode()).decode())
        with urllib.request.urlopen(req, timeout=6) as r:
            body = r.read().decode("utf-8", "replace")
            print(f"  {url} -> HTTP {r.status}")
            print("     " + body[:1500].replace("\n", "\n     "))
    except Exception as e:  # noqa: BLE001
        print(f"  {url} -> 失败 ({type(e).__name__}: {e})")

sect("6. 走隧道取 9481（看响应头/正文，判断是谁在应答）")
targets = [
    "http://115.190.250.104:9481/health",
    "http://115.190.250.104:9481/",
    "http://115.190.250.104:8080/health",
]
for url in targets:
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            body = r.read(400).decode("utf-8", "replace")
            print(f"  {url}\n     -> HTTP {r.status}")
            for k, v in list(r.headers.items())[:10]:
                print(f"        {k}: {v}")
            print("        body:", body[:250].replace("\n", " "))
    except urllib.error.HTTPError as e:
        body = e.read(400).decode("utf-8", "replace")
        print(f"  {url}\n     -> HTTP {e.code}")
        for k, v in list(e.headers.items())[:10]:
            print(f"        {k}: {v}")
        print("        body:", re.sub(r"\s+", " ", body)[:250])
    except Exception as e:  # noqa: BLE001
        print(f"  {url} -> 失败 ({type(e).__name__}: {e})")
