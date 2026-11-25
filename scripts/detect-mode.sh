#!/usr/bin/env bash
# Prints "kind" if the kind ingress answers on http://localhost:80, else "compose".
# The Makefile uses it to pick host-side URLs; override with `make <target> MODE=kind|compose`.
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 1 http://localhost/healthz 2>/dev/null || true)
case "$code" in
  2??|3??|502|503|504) echo kind ;;
  *) echo compose ;;
esac
