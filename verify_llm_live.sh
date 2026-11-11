#!/bin/bash
# E2E: clips job (Bulin 50-110) as admin on the VPS, check selector==llm, then render clip 1 and check saved title/score.
BASE=http://38.92.48.152
echo "== health =="; curl -s -m 30 $BASE/api/health; echo
TOKEN=$(curl -s -m 30 -X POST $BASE/api/auth/login -H 'Content-Type: application/json' -d '{"email":"admin@marketingcaribe.com","password":"Admin1234!"}' | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token','NO_TOKEN'))")
echo "== clips job =="
START=$(date +%s)
JOB=$(curl -s -m 60 -X POST $BASE/api/clips -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"url":"https://www.youtube.com/watch?v=lvPIofmDIGk","start":50,"end":110}' | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('job_id') or d)")
echo "job: $JOB"
for i in $(seq 1 30); do
  sleep 15
  S=$(curl -s -m 30 $BASE/api/jobs/$JOB)
  echo "poll $i ($(($(date +%s)-START))s): $(echo "$S" | head -c 400)"
  echo "$S" | grep -q '"status": *"done"' && break
  echo "$S" | grep -q '"status": *"error"' && break
done
echo "== result (selector + candidates) =="
curl -s -m 30 $BASE/api/jobs/$JOB | python3 -c "
import sys,json
d=json.load(sys.stdin); r=d.get('result') or {}
print('status:', d.get('status'), '| selector:', r.get('selector'), '| error:', d.get('error'))
for c in r.get('candidates', []):
    print(f\"- {c.get('start')}->{c.get('end')} ({c.get('duration')}s) score={c.get('score')} title={c.get('title')!r} reason={str(c.get('reason'))[:80]!r}\")
print('MODEL:', (r.get('transcript') or {}).get('model'))
"
echo "== render candidate 1 =="
CAND=$(curl -s -m 30 $BASE/api/jobs/$JOB | python3 -c "import sys,json; c=json.load(sys.stdin)['result']['candidates'][0]; print(c['abs_start'], c['abs_end'])")
echo "candidate 1 abs range: $CAND"
CS=$(echo $CAND | cut -d' ' -f1); CE=$(echo $CAND | cut -d' ' -f2)
RJ=$(curl -s -m 60 -X POST $BASE/api/render -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d "{\"job_id\":\"$JOB\",\"start\":$CS,\"end\":$CE}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('job_id') or d)")
echo "render job: $RJ"
for i in $(seq 1 20); do
  sleep 10
  S=$(curl -s -m 30 $BASE/api/jobs/$RJ)
  echo "render poll $i: $(echo "$S" | head -c 200)"
  echo "$S" | grep -q '"status": *"done"' && break
  echo "$S" | grep -q '"status": *"error"' && break
done
echo "== my clips (saved title/score) =="
curl -s -m 30 $BASE/api/me/clips -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys,json
for c in json.load(sys.stdin)[:3]:
    print(c.get('id'), '|', c.get('title'), '| score=', c.get('score'), '| file=', c.get('file'), '|', c.get('start'), '->', c.get('end'))
"
echo "== done in $(($(date +%s)-START))s =="
