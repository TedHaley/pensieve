# Baseline

This file gives the energy and time numbers that each change of Pensieve must beat.

All numbers come from one Mac: Apple M4 Max, 64 GB, on AC power, on 2026-11-12. Each number comes from one run.

## How the numbers were measured

- **CPU seconds:** the change in `ps` CPU time of the daemon and its child processes. GPU time is not in this number.
- **Energy Impact:** the `POWER` column of `top`, sampled each 5 or 10 seconds over 1 second. It is a relative score,
  not watts.
- **Memory:** the `MEM` column of `top`. It includes GPU memory. `ps` RSS does not, and for an MLX process it shows
  less than 3% of the real memory.
- `powermetrics` was not used, because it needs root. Thus there is no number in mWh yet.
- The idle-wakeup column of `top` gave values that did not change for minutes. These values are not used.

## Pensieve 0.21 in normal use

One hour of the installed 0.21 on a Mac in daily use. The server indexed repos in 75% of the samples.

| Process | CPU seconds per hour | Share of one core | Energy Impact (mean) | Memory |
|---|---|---|---|---|
| Server (Python) | 1,198 | 33% | 29.4 | 2.7 GB median, 4.2 GB peak |
| Text engine (Qwen 3.5 9B on MLX) | 434 | 12% | 10.4 | 6.3 GB |
| Mac app | 85 | 2% | 2.1 | 67 MB |
| **Total** | **1,721** | **48%** | **41.9** | about 9 GB |

Over 34.9 hours since the last start, the server used 257 CPU minutes and the text engine used 212 CPU minutes.
Lite has no text engine and no app, so two of these three rows are not in Lite.

## First index of a fixed repo: 0.21 and Lite

The Pensieve repo at commit `45b55f6` (238 tracked files) is the only content of a scratch home folder. Each daemon
indexes it into an empty data folder. Both daemons made 2,821 code chunks. The installed 0.21 ran on the same Mac
during both runs.

| | 0.21 | Lite |
|---|---|---|
| Time to the full index | 115 s | 100 s |
| CPU seconds | 64.1 | 5.2 |
| Energy Impact, mean until the full index | 24.8 | 3.8 |
| Energy Impact × seconds until the full index | 2,730 | 396 |
| Peak memory | 4.1 GB | 2.3 GB |
| Energy Impact when idle after the index | 9.4 | 0.02 |

Lite uses about 1/7 of the Energy Impact of 0.21 for the same index. When idle, 0.21 continues to work (map
layouts, threads, insights). Lite does nothing when idle.

These are not verified energy numbers. Lite embeds on the GPU through Metal, and CPU seconds do not include GPU time.
It is not known if the `POWER` column includes all of the GPU energy. A `powermetrics` run gives the answer.

## Scripts

- `bench/energy_sample.py` samples a list of processes and their child processes. It writes one JSON line for each
  sample.
- `bench/energy_summary.py` gives the totals for each process from that file.
- `bench/first_index.sh` does the first-index run for `lite` or `021`.

The one-hour run used `energy_sample.py` on the processes of the installed 0.21, at 10-second intervals.
