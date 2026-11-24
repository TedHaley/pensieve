"""Summarize an energy_sample.py file: per process and in total. Usage: python3 -I energy_summary.py FILE.jsonl"""
import json, statistics, sys
from collections import Counter

rows = [json.loads(l) for l in open(sys.argv[1])]
hours = (rows[-1]["t"] - rows[0]["t"]) / 3600
print(f"samples {len(rows)}  span {hours * 60:.1f} min")
first, last = {}, {}
power, rss, names = {}, {}, {}
for r in rows:
    for pid, p in r["procs"].items():
        first.setdefault(pid, p)
        last[pid] = p
        names[pid] = p["comm"]
        power.setdefault(pid, []).append(p.get("power", 0.0))
        rss.setdefault(pid, []).append(p.get("top_mb", 0) * 1024 or p["rss_kb"])
tot_cpu = tot_pow = 0.0
for pid in sorted(last, key=lambda k: -(last[k]["cpu_s"] - first[k]["cpu_s"])):
    cpu = last[pid]["cpu_s"] - first[pid]["cpu_s"]
    wk = last[pid].get("idlew", 0) - first[pid].get("idlew", 0)
    pw = statistics.mean(power[pid])
    tot_cpu += cpu
    tot_pow += pw
    if cpu < 0.5 and pw < 0.05:
        continue
    print(f"{pid:>6} {names[pid]:12s} cpu {cpu:8.1f}s ({cpu / hours / 36:5.2f}% of a core)  "
          f"power mean {pw:6.2f}  wakeups {wk:>8}  rss med {statistics.median(rss[pid]) / 1024:7.0f} MB "
          f"peak {max(rss[pid]) / 1024:7.0f} MB")
print(f"total cpu {tot_cpu:.1f}s = {tot_cpu / hours:.0f} s/h  power mean (sum) {tot_pow:.2f}")
st = [r["status"] for r in rows if r.get("status")]
if st:
    busy = sum(1 for s in st if s.get("indexing"))
    print(f"server busy indexing in {busy} of {len(st)} samples; messages {Counter(s.get('message') for s in st).most_common(4)}")
