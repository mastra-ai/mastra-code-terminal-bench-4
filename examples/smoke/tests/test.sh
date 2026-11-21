#!/bin/bash
set -eu
mkdir -p /logs/verifier
if [ -f /app/hello.txt ] && [ "$(cat /app/hello.txt)" = 'hello harbor' ] && [ "$(wc -c < /app/hello.txt)" -eq 13 ]; then
  printf '1\n' > /logs/verifier/reward.txt
else
  printf '0\n' > /logs/verifier/reward.txt
fi
