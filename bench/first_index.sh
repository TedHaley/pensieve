#!/bin/zsh
# First index of one repo by one daemon, on a scratch home folder, with the energy sampler on the daemon.
#
#   bench/first_index.sh lite|021 REPO WORK
#
# REPO: the repo to index (copied into WORK/home/code). WORK: a scratch folder for the home, the data and the samples.
# 021 needs PENSIEVE_021, the folder of a Pensieve 0.21 checkout with its .venv.
# The run stops when the daemon has been idle for 120 s after its first code chunks, or after 60 minutes.
# Then: python3 -I bench/energy_summary.py WORK/WHICH.jsonl
set -e
WHICH=$1 REPO=$2 WORK=$3
HERE=${0:A:h}
DATA=$WORK/data-$WHICH
mkdir -p $WORK/home/code
[[ -d $WORK/home/code/${REPO:t} ]] || git clone -q --no-hardlinks $REPO $WORK/home/code/${REPO:t}
rm -rf $DATA && mkdir -p $DATA
export HF_HUB_CACHE=${HF_HUB_CACHE:-$HOME/.cache/huggingface/hub}
export HOME=$WORK/home
if [[ $WHICH == 021 ]]; then
  PORT=8799 URL=http://127.0.0.1:8799/api/status
  # The text engine points at a closed port, so that only indexing runs.
  print -r -- '{"setup_done": true, "mode": "developer", "text_engine": "server", "llm_url": "http://127.0.0.1:9/v1", "insights_auto": false, "auto_update_check": false}' > $DATA/settings.json
  (cd $PENSIEVE_021 && PENSIEVE_DATA=$DATA PENSIEVE_PORT=$PORT .venv/bin/python -m pensieve.cli serve --no-open --port $PORT) > $WORK/$WHICH.log 2>&1 &
else
  PORT=8797 URL=http://127.0.0.1:8797/status
  PENSIEVE_DATA=$DATA $HERE/../target/release/pensieve serve --port $PORT > $WORK/$WHICH.log 2>&1 &
fi
P=$!
rm -f $WORK/$WHICH.jsonl
python3 -I $HERE/energy_sample.py $WORK/$WHICH.jsonl 3600 5 $URL -- $P &
SAMPLER=$!
START=$(date +%s) IDLE_SINCE=0 CHUNKS=0
set +e
while kill -0 $P 2>/dev/null; do
  sleep 5
  J=$(curl -s -m 3 $URL)
  read CHUNKS BUSY <<< $(print -r -- "$J" | python3 -I -c 'import json, sys
try:
    s = json.load(sys.stdin)
    print(s.get("code_chunks") or 0, int(bool(s.get("indexing") or s.get("busy") or s.get("opening") or s.get("syncing"))))
except Exception:
    print(0, 1)')
  NOW=$(date +%s)
  if [[ $CHUNKS -gt 0 && $BUSY == 0 ]]; then
    (( IDLE_SINCE == 0 )) && IDLE_SINCE=$NOW
    (( NOW - IDLE_SINCE >= 120 )) && break
  else
    IDLE_SINCE=0
  fi
  (( NOW - START >= 3600 )) && break
done
print -r -- "$J" > $WORK/$WHICH.final.json
kill $P 2>/dev/null; sleep 6; kill $SAMPLER 2>/dev/null
echo "done $WHICH chunks=$CHUNKS secs=$(( $(date +%s) - START ))"
