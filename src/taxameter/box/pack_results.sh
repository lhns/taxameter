#!/usr/bin/env bash
# Pack the result files of the given directories into one archive (run on the GPU box), so a flaky SSH
# connection only has to stay up for one short copy. Run from the project dir:
#   bash pack_results.sh results runs/foo   -> ${OUT:-/root/results.tgz}
# Per directory: listed in (or under) TAXAMETER_PACK_WHOLE -> every file; otherwise files whose basename matches a
# TAXAMETER_PACK_INCLUDE glob. Paths matching a TAXAMETER_PACK_EXCLUDE glob are always dropped.
set -euo pipefail
TAXAMETER_HOME="${TAXAMETER_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
# shellcheck disable=SC1091
[ -f "$TAXAMETER_HOME/env" ] && . "$TAXAMETER_HOME/env"
out="${OUT:-/root/results.tgz}"
list="$(mktemp)"
trap 'rm -f "$list"' EXIT
set -f  # the globs are patterns, never expanded against the filesystem
# shellcheck disable=SC2206
include=(${TAXAMETER_PACK_INCLUDE:-}) whole=(${TAXAMETER_PACK_WHOLE:-}) exclude=(${TAXAMETER_PACK_EXCLUDE:-})
for d in "$@"; do
  d="${d%/}"
  all=0
  for w in "${whole[@]}"; do
    w="${w%/}"
    # shellcheck disable=SC2053
    if [[ "$d" == "$w" || "$d" == "$w"/* ]]; then all=1; fi
  done
  while IFS= read -r -d '' f; do
    if [ "$all" = 0 ]; then
      keep=0
      for g in "${include[@]}"; do
        # shellcheck disable=SC2053
        if [[ "${f##*/}" == $g ]]; then keep=1; break; fi
      done
      [ "$keep" = 1 ] || continue
    fi
    skip=0
    for g in "${exclude[@]}"; do
      # shellcheck disable=SC2053
      if [[ "$f" == $g ]]; then skip=1; break; fi
    done
    [ "$skip" = 1 ] || printf '%s\n' "$f"
  done < <(find "$d" -type f -print0)
done | sort -u > "$list"
tar -czf "$out" -T "$list"
echo "$(wc -l < "$list") files, $(du -h "$out" | cut -f1) -> $out"
