"""实验三：数字后置对账（_verify_numbers）的检出能力——受控扰动注入。
从公开标的（美股/港股关注列表里的上市公司）的本地资料中抽取带单位的真实数字，
构造「正确引用」与「各类错误引用」两类句子，统计误报率与检出率。不涉及任何持仓数据（只取基本面/财报/研报段落）。"""
import json, os, random, re, sys
sys.path.insert(0, os.environ.get("HARNESS_ROOT", str(pathlib.Path(__file__).resolve().parent.parent)))
from harness.ask_service import _verify_numbers_v1, verify_numbers_v2, context_for  # noqa: E402
V = {'v1': lambda a, s: bool(_verify_numbers_v1(a, s)), 'v2': lambda a, s: bool(verify_numbers_v2(a, s)['unverified'])}

random.seed(7)
CODES = ["AAPL.US", "MSFT.US", "AMD.US", "NVDA.US", "GOOGL.US", "META.US", "TSM.US", "00700.HK", "09988.HK", "03690.HK"]
UNIT_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(%|倍|亿)")


def public_text(code):
    ctx = context_for(code)
    keep = []
    for part in ctx["text"].split("\n## "):
        head = part.split("〔")[0]
        if any(k in head for k in ("基本面", "财报", "机构研究", "一页纸", "SEC")):
            keep.append(part)
    return "\n## ".join(keep)


def perturb(v, kind):
    if kind.startswith("rel"):
        d = float(kind[3:]) / 100
        return v * (1 + d * random.choice((-1, 1)))
    if kind == "x10":
        return v * 10
    if kind == "/10":
        return v / 10
    if kind == "x100":
        return v * 100
    if kind == "swap":
        s = f"{v:g}"
        digs = [i for i, ch in enumerate(s) if ch.isdigit()]
        for _ in range(10):
            i, j = random.sample(digs, 2) if len(digs) >= 2 else (0, 0)
            t = list(s); t[i], t[j] = t[j], t[i]
            t = "".join(t)
            if t != s and not t.startswith("0"):
                return float(t)
        return None
    raise KeyError(kind)


KINDS = ["rel1", "rel3", "rel5", "rel10", "rel20", "rel50", "x10", "/10", "x100", "swap"]
res = {v: {k: [0, 0] for k in ["correct", "round1", "round0"] + KINDS} for v in V}
samples = 0
for code in CODES:
    try:
        src = public_text(code)
    except Exception as e:
        print(code, "skip", e); continue
    vals = [(float(m.group(1)), m.group(2)) for m in UNIT_RE.finditer(src) if float(m.group(1)) >= 1]
    random.shuffle(vals)
    for v, u in vals[:40]:
        samples += 1
        fmt = (lambda x: f"{x:.2f}".rstrip("0").rstrip("."))
        for ver, fn in V.items():
            for tag, txt in (("correct", fmt(v)), ("round1", f"{v:.1f}"), ("round0", f"{v:.0f}")):
                ok = not fn(f"该指标为 {txt}{u}。", src)
                res[ver][tag][0] += ok; res[ver][tag][1] += 1
        for k in KINDS:
            w = perturb(v, k)
            if w is None or w < 1 or abs(w - v) < 1e-9:
                continue
            for ver, fn in V.items():
                flagged = fn(f"该指标为 {fmt(w)}{u}。", src)
                res[ver][k][0] += flagged; res[ver][k][1] += 1
    print(code, "累计样本", samples)

out = {"samples": samples, "codes": len(CODES)}
for ver in V:
    r = res[ver]
    out[ver] = {"false_positive_rate": 1 - r["correct"][0] / r["correct"][1],
                "fp_round1": 1 - r["round1"][0] / r["round1"][1], "fp_round0": 1 - r["round0"][0] / r["round0"][1],
                "detect": {k: r[k][0] / r[k][1] for k in KINDS if r[k][1]},
                "n": {k: r[k][1] for k in KINDS}}
print(json.dumps(out, ensure_ascii=False, indent=1))
json.dump(out, open(sys.argv[1], "w"), ensure_ascii=False, indent=1)
