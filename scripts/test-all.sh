#!/usr/bin/env bash
# Run pytest for every package that has tests: sdk, cli, services/*.
# Uses the package's own .venv if it has one, else the repo .venv (make venv), else system python.
set -uo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
failed=()
ran=0
for dir in sdk cli services/*; do
  d="$root/$dir"
  [ -d "$d/tests" ] || continue
  py=python3
  if [ -x "$d/.venv/bin/python" ]; then py="$d/.venv/bin/python"
  elif [ -x "$root/.venv/bin/python" ]; then py="$root/.venv/bin/python"; fi
  echo "=== $dir ($py) ==="
  ran=$((ran + 1))
  ( cd "$d" && "$py" -m pytest -q ); rc=$?
  # 5 = no tests collected: not a failure
  if [ "$rc" -ne 0 ] && [ "$rc" -ne 5 ]; then failed+=("$dir"); fi
done

[ "$ran" -eq 0 ] && { echo "no tests found yet"; exit 0; }
if [ "${#failed[@]}" -gt 0 ]; then
  echo "FAILED: ${failed[*]}"
  exit 1
fi
echo "all $ran test suites passed"
