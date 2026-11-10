#!/bin/bash
# Wait for Render deploy (health gains "transcription":"groq"), then run a live clips job as admin and time it.
BASE=https://clipper-marketingcaribe.onrender.com
echo "== waiting for deploy (up to 14 min) =="
for i in $(seq 1 14); do
  H=$(curl -s -m 30 $BASE/api/health)
  echo "try $i: $H"
  echo "$H" | grep -q '"transcription": "groq"' && break
  sleep 60
done
echo "== admin login =="
TOKEN=$(curl -s -m 30 -X POST $BASE/api/auth/login -H 'Content-Type: application/json' -d '{"email":"admin@marketingcaribe.com","password":"Admin1234!"}' | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token','NO_TOKEN'))")
echo "token: ${TOKEN:0:12}..."
echo "== create clips job (Bulin 50-110s) =="
START=$(date +%s)
JOB=$(curl -s -m 60 -X POST $BASE/api/clips -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"url":"https://www.youtube.com/watch?v=lvPIofmDIGk","start":50,"end":110}' | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('job_id') or d)")
echo "job: $JOB"
for i in $(seq 1 30); do
  sleep 20
  S=$(curl -s -m 30 $BASE/api/jobs/$JOB)
  echo "poll $i ($(($(date +%s)-START))s): $(echo "$S" | head -c 300)"
  echo "$S" | grep -q '"status": "done"\|"status":"done"' && break
  echo "$S" | grep -q '"status": "failed"\|"status":"failed"\|"status": "error"' && break
done
echo "== done, total $(( $(date +%s) - START ))s =="
