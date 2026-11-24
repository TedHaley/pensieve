"""Sample the CPU time, Energy Impact, idle wakeups and memory of Pensieve processes without root.

Usage: python3 -I energy_sample.py OUT.jsonl SECONDS INTERVAL [STATUS_URL] -- PID [PID ...]
Children of each PID are included (git, workers). Each line of OUT is one sample.
"""
import json, subprocess, sys, time, urllib.request

out, secs, step = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
rest = sys.argv[4:]
url = rest[0] if rest and rest[0] != "--" else None
roots = [int(p) for p in rest[rest.index("--") + 1:]]


def tree(pids):
    found, todo = set(), list(pids)
    while todo:
        p = todo.pop()
        if p in found:
            continue
        found.add(p)
        kids = subprocess.run(["pgrep", "-P", str(p)], capture_output=True, text=True).stdout.split()
        todo += [int(k) for k in kids]
    return sorted(found)


def cpu_seconds(t):
    # ps TIME: [[dd-]hh:]mm:ss.ss
    days = 0
    if "-" in t:
        d, t = t.split("-")
        days = int(d)
    parts = [float(x) for x in t.split(":")]
    s = 0.0
    for x in parts:
        s = s * 60 + x
    return days * 86400 + s


end = time.time() + secs
with open(out, "a") as f:
    while time.time() < end:
        t0 = time.time()
        pids = tree(roots)
        arg = ",".join(map(str, pids))
        ps = subprocess.run(["ps", "-o", "pid=,time=,rss=,comm=", "-p", arg], capture_output=True, text=True).stdout
        procs = {}
        if not ps.strip():  # every process has exited
            break
        for line in ps.splitlines():
            pid, tm, rss, comm = line.split(None, 3)
            procs[pid] = {"cpu_s": cpu_seconds(tm), "rss_kb": int(rss), "comm": comm.rsplit("/", 1)[-1]}
        cmd = ["top", "-l", "2", "-s", "1", "-stats", "pid,power,idlew,mem"]
        for p in pids:
            cmd += ["-pid", str(p)]
        top = subprocess.run(cmd, capture_output=True, text=True).stdout.split("PID")[-1].splitlines()[1:]
        for line in top:  # the second sample: power and wakeups over 1 s
            v = line.split()
            if len(v) >= 3 and v[0] in procs:
                procs[v[0]].update(power=float(v[1]), idlew=int(v[2].rstrip("+")))
                if len(v) >= 4:  # top's MEM includes GPU memory, which ps RSS leaves out
                    m = v[3].rstrip("+-")
                    mult = {"K": 1 / 1024, "M": 1, "G": 1024}.get(m[-1:], 1 / 1048576)
                    procs[v[0]]["top_mb"] = float(m.rstrip("KMGB") or 0) * mult
        status = None
        if url:
            try:
                status = json.loads(urllib.request.urlopen(url, timeout=3).read())
                status = {k: status.get(k) for k in ("indexing", "message", "repo_busy", "chunks", "code_chunks")}
            except Exception as e:
                status = {"error": repr(e)}
        f.write(json.dumps({"t": t0, "procs": procs, "status": status}) + "\n")
        f.flush()
        time.sleep(max(0.0, step - (time.time() - t0)))
