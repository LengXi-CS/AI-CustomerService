"""只读：检查 C:\\Users\\THINK\\.frp\\input.txt 是否就位（值一律脱敏）。"""
import os

p = os.path.join(os.path.expanduser("~"), ".frp", "input.txt")
print("路径:", p)
print("存在:", os.path.exists(p))
if os.path.exists(p):
    with open(p, encoding="utf-8-sig") as f:
        for i, line in enumerate(f, 1):
            t = line.rstrip("\n")
            if not t.strip() or t.strip().startswith("#"):
                print(f"  {i:>3}| {t}")
                continue
            if "=" in t:
                k, v = t.split("=", 1)
                k = k.strip()
                v = v.strip()
                if any(s in k.upper() for s in ("TOKEN", "KEY", "PWD", "SECRET")):
                    show = "<已填写 长度%d>" % len(v) if v else "<空>"
                else:
                    show = v if v else "<空>"
                print(f"  {i:>3}| {k} = {show}")
            else:
                print(f"  {i:>3}| {t}")
