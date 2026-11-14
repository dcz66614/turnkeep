#!/bin/bash
# T042 agy confirmation re-run: wait for the agy quota reset, then --resume
# the 63 deleted contaminated attempts (369 kept). Log: agy-confirmation-rerun.log
cd ~/workspace/turnkeep-work || exit 1
exec > agy-confirmation-rerun.log 2>&1
echo "rerun launcher started $(date -u +%FT%TZ)"
probe_ok() {
  timeout 90 ~/.local/bin/agy -p "Reply with exactly the word OK and nothing else." \
    --output-format stream-json 2>/dev/null | grep -q '"status":"SUCCESS"'
}
ok=0
for i in 1 2 3 4 5 6; do
  if probe_ok; then ok=1; echo "quota probe $i: OK $(date -u +%FT%TZ)"; break; fi
  echo "quota probe $i: still exhausted $(date -u +%FT%TZ), sleeping 180s"
  sleep 180
done
if [ "$ok" -ne 1 ]; then echo "QUOTA STILL DOWN after 6 probes, aborting"; exit 1; fi
.venv/bin/python benchmarks/live/run_live.py --driver agy --suite confirmation \
  --parallel 3 --resume \
  --output evidence/live-evals/campaign-20261109-T042-agy
echo "rerun rc=$? $(date -u +%FT%TZ)"
