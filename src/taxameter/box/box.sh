#!/usr/bin/env bash
# On-box helper for taxameter. Uploaded to /root/.taxameter/box.sh (temp name + mv, so a running copy is never
# overwritten in place) and called as `bash /root/.taxameter/box.sh <command> ...`. All pgrep patterns live in a
# file (this one or the env file), so they can never match the ssh command line that invoked them.
# Project settings come from /root/.taxameter/env (TAXAMETER_* variables).
#   health                     key=value lines: GPU, busy processes, batch log progress, watchdog, disk, RAM, DNS
#   ensure-watchdog            start idle_autostop.sh unless running (supervisor if available, else nohup)
#                              and register it to come back after a container restart (supervisor + onstart.sh)
#   install-push               move a pushed tree from the staging dir into the project dir file by file
#                              (rename = new inode, so a script bash is executing is never rewritten in place)
#   provision <models...>      provision hook + cuda check + models + watchdog + inventory; run detached,
#                              logs PROVISION_DONE
#   verify <models...>         GPU visible, CUDA initialises, models present, verify hook (replacement acceptance)
#   models <org/name:file>...  download model files from ModelScope or Hugging Face (the faster) into
#                              <models dir>/<org>__<name>/, verify sizes; file lists are joined with + (a.gguf+b.gguf)
#   run <script> [args...]     start a project script detached (it guards itself with flock); log <logs dir>/<name>.log
#   pack <dirs...>             pack result files into /root/results.tgz (pack_results.sh)
#   inventory                  models and result directories on the box (for re-provisioning a replacement)
#   logs [n]                   tail the batch log and the watchdog log
set -uo pipefail
TAXAMETER_HOME="${TAXAMETER_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
# shellcheck disable=SC1091
[ -f "$TAXAMETER_HOME/env" ] && . "$TAXAMETER_HOME/env"
export TAXAMETER_HOME
TAXAMETER_DIR="${TAXAMETER_DIR:-/root/project}"; export TAXAMETER_DIR
TAXAMETER_STAGING="${TAXAMETER_STAGING:-$TAXAMETER_HOME/push}"
TAXAMETER_LOGS_DIR="${TAXAMETER_LOGS_DIR:-runs/taxameter}"
TAXAMETER_MODELS_DIR="${TAXAMETER_MODELS_DIR:-.hf_cache}"
TAXAMETER_DONE_MARKER="${TAXAMETER_DONE_MARKER:-DONE}"
export PATH="/opt/cargo/bin:$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
cmd="${1:-health}"; shift || true

count_procs() {  # number of processes matching any non-empty pattern; 0 when there is none
  # An empty pattern must never reach pgrep: `pgrep -f ''` matches every process and the box would never go idle.
  local joined="" p
  for p in "$@"; do [ -n "$p" ] && joined="${joined:+$joined|}($p)"; done
  [ -n "$joined" ] || { echo 0; return; }
  pgrep -f "$joined" | wc -l
}

disk_probe() {  # sequential read bandwidth of /root in MB/s; measured once, cached (health runs every few minutes)
  local cache=/root/.disk_probe f=/root/.disk_probe.dat mbs=""
  [ -s "$cache" ] && { cat "$cache"; return; }
  if command -v fio > /dev/null 2>&1; then
    # terse v3 field 7 = read bandwidth in KiB/s
    mbs=$(timeout 40 fio --name=probe --filename="$f" --rw=read --bs=1M --size=512M --direct=1 --minimal 2>/dev/null \
          | awk -F';' 'NR==1 {printf "%d", $7 / 1024}')
  fi
  if [ -z "$mbs" ] || [ "$mbs" = 0 ]; then  # dd fallback: write 512 MB, read it back without the page cache
    dd if=/dev/zero of="$f" bs=1M count=512 oflag=direct status=none 2> /dev/null
    mbs=$(timeout 40 dd if="$f" of=/dev/null bs=1M iflag=direct 2>&1 | tail -1 \
          | awk '{for (i = 1; i < NF; i++) if ($(i + 1) == "MB/s") printf "%d", $i; else if ($(i + 1) == "GB/s") printf "%d", $i * 1000}')
  fi
  rm -f /root/.disk_probe.dat
  echo "${mbs:-0}" | tee "$cache"
}

health() {
  echo "now=$(date +%s)"
  local q
  q=$(nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits 2>/dev/null \
      | head -1 | tr -d ' ')
  echo "gpu_util=$(echo "$q" | cut -d, -f1)"
  echo "gpu_mem_used=$(echo "$q" | cut -d, -f2)"
  echo "gpu_mem_total=$(echo "$q" | cut -d, -f3)"
  # busy = the project's TAXAMETER_BUSY pattern (anchored: a bare project name would match this very script's
  # command line) or a provision/models run (this script's own `health` call is excluded by the subcommand)
  echo "exp_procs=$(count_procs "${TAXAMETER_BUSY:-}" 'box[.]sh (provision|models)')"
  echo "batch_alive=$(count_procs "${TAXAMETER_BATCH:-}")"
  local log="$TAXAMETER_DIR/${TAXAMETER_BATCH_LOG:-}"
  if [ -n "${TAXAMETER_BATCH_LOG:-}" ] && [ -f "$log" ]; then
    echo "batch_log_age_s=$(( $(date +%s) - $(stat -c %Y "$log") ))"
    echo "batch_started=$(grep -c '] start' "$log")"
    echo "batch_done=$(grep -c '] done' "$log")"
    echo "batch_failed=$(grep '] done' "$log" | grep -vc 'rc=0$')"
    if [ -n "${TAXAMETER_BATCH_DONE:-}" ]; then echo "batch_all_done=$(grep -cE "$TAXAMETER_BATCH_DONE" "$log")"; else echo "batch_all_done=0"; fi
    echo "batch_last=$(tail -1 "$log" | tr -d '\r' | cut -c1-160)"
  else
    echo "batch_log_age_s="
  fi
  # progress = newest write to any log under the logs dir (the batch log only changes per stage)
  local newest
  newest=$(find "$TAXAMETER_DIR/$TAXAMETER_LOGS_DIR" -maxdepth 2 -name '*.log' -printf '%T@\n' 2>/dev/null | sort -n | tail -1 | cut -d. -f1)
  [ -n "$newest" ] && echo "progress_age_s=$(( $(date +%s) - newest ))"
  echo "watchdog=$(pgrep -f 'idle_autost[o]p[.]sh' | wc -l)"
  echo "disk_free_gb=$(df -BG --output=avail /root 2>/dev/null | tail -1 | tr -dc '0-9')"
  echo "ram_gb=$(awk '/MemTotal/ {printf "%d", $2 / 1048576}' /proc/meminfo 2>/dev/null)"
  echo "disk_read_mbs=$(disk_probe)"
  if [ -z "${TAXAMETER_DNS_HOST:-}" ] || getent hosts "$TAXAMETER_DNS_HOST" > /dev/null 2>&1; then echo "dns_ok=1"; else echo "dns_ok=0"; fi
}

ensure_watchdog() {
  local wrapper="$TAXAMETER_HOME/autostop_run.sh"
  # supervisord may not pass the provider-injected variables through; fall back to /etc/environment
  cat > "$wrapper.tmp" <<EOS
#!/usr/bin/env bash
if [ -z "\${CONTAINER_API_KEY:-}" ] && [ -f /etc/environment ]; then set -a; . /etc/environment; set +a; fi
exec bash $TAXAMETER_HOME/idle_autostop.sh
EOS
  mv "$wrapper.tmp" "$wrapper"
  local mode=nohup
  if [ -d /etc/supervisor/conf.d ] && command -v supervisorctl > /dev/null 2>&1; then
    cat > /etc/supervisor/conf.d/taxameter-autostop.conf <<EOS
[program:taxameter-autostop]
command=/bin/bash $wrapper
autostart=true
autorestart=true
startsecs=5
stdout_logfile=/root/autostop.log
redirect_stderr=true
EOS
    supervisorctl reread > /dev/null 2>&1; supervisorctl update > /dev/null 2>&1
    mode=supervisor
  fi
  # also relaunch from the provider's on-start hook (runs on every container start), idempotently
  local line="bash $TAXAMETER_HOME/box.sh ensure-watchdog >> /root/autostop.log 2>&1 || true"
  touch /root/onstart.sh
  grep -qF "$line" /root/onstart.sh || echo "$line" >> /root/onstart.sh
  sleep 3
  if [ "$(pgrep -f 'idle_autost[o]p[.]sh' | wc -l)" -eq 0 ]; then
    [ "$mode" = supervisor ] && supervisorctl start taxameter-autostop > /dev/null 2>&1 && sleep 3
  fi
  if [ "$(pgrep -f 'idle_autost[o]p[.]sh' | wc -l)" -eq 0 ]; then
    nohup setsid bash "$wrapper" >> /root/autostop.log 2>&1 < /dev/null &
    mode="$mode+nohup"
    sleep 2
  fi
  echo "watchdog=$(pgrep -f 'idle_autost[o]p[.]sh' | wc -l) mode=$mode"
}

install_push() {
  local src="$TAXAMETER_STAGING" n=0 failed=0 f
  [ -d "$src" ] || { echo "no push dir"; return 1; }
  # normalise line endings in the staging copy, before anything is moved next to running scripts
  find "$src" -type f \( -name '*.sh' -o -name '*.py' -o -name '*.toml' -o -name '*.yaml' -o -name '*.md' \) \
      -not -path "$src/vendor/*" -print0 | xargs -0 -r sed -i 's/\r$//'
  cd "$src" || return 1
  while IFS= read -r -d '' f; do
    mkdir -p "$TAXAMETER_DIR/$(dirname "$f")"
    if mv -f "$f" "$TAXAMETER_DIR/$f"; then n=$((n + 1)); else echo "mv failed: $f"; failed=1; fi
  done < <(find . -type f -print0)
  cd / || return 1
  # keep the staging dir when a move failed, so the push can be inspected or repeated
  if [ "$failed" != 0 ]; then echo "install-push: $src kept (mv failures)"; return 1; fi
  rm -rf "${src:?}"
  echo "installed $n files into $TAXAMETER_DIR"
}

provision() {
  # a repeated launch (the controller re-runs ssh commands after a dropped connection) must not start a second provisioning
  exec 8> "$TAXAMETER_HOME/provision.lock"; flock -n 8 || { echo "provision already running"; return 0; }
  local rc=0
  echo "$(date +%T) provision start"
  # cuda_env.sh probes libcuda through ctypes
  command -v python3 > /dev/null 2>&1 || DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 > /dev/null 2>&1 || true
  if [ -n "${TAXAMETER_PROVISION_HOOK:-}" ]; then
    # fd 8 closed: the hook's children must not hold the provision lock
    (cd "$TAXAMETER_DIR" && bash "$TAXAMETER_PROVISION_HOOK") 8>&- || { echo "provision hook FAILED"; rc=1; }
  fi
  # shellcheck disable=SC1091
  . "$TAXAMETER_HOME/cuda_env.sh"
  echo "cuda: $CUDA_ENV_STATUS${LD_PRELOAD:+ (LD_PRELOAD=$LD_PRELOAD)}"
  [ "$CUDA_ENV_STATUS" = fail ] && { echo "CUDA FAILED: libcuda cannot initialise the GPU"; rc=1; }
  if [ "$#" -gt 0 ]; then models "$@" || rc=1; fi
  disk_probe > /dev/null  # cache the disk bandwidth now so `health` never pays for it
  ensure_watchdog 8>&-  # the long-lived watchdog must not inherit (and hold) the provision lock
  inventory
  echo "$(date +%T) PROVISION_DONE rc=$rc"
}

verify() {  # replacement acceptance test: GPU visible, CUDA initialises, models present, project hook
  local ok=1
  nvidia-smi -L > /dev/null 2>&1 && echo "verify_gpu=1" || { echo "verify_gpu=0"; ok=0; }
  # shellcheck disable=SC1091
  . "$TAXAMETER_HOME/cuda_env.sh"
  [ "$CUDA_ENV_STATUS" = fail ] && { echo "verify_cuda=0"; ok=0; } || echo "verify_cuda=1"
  local m dir file
  for m in "$@"; do
    dir="$TAXAMETER_DIR/$TAXAMETER_MODELS_DIR/$(model_dir "${m%%:*}")"
    for file in $(echo "${m#*:}" | tr '+' ' '); do
      [ -s "$dir/$file" ] || { echo "verify_missing=$m/$file"; ok=0; }
    done
  done
  if [ -n "${TAXAMETER_VERIFY_HOOK:-}" ]; then
    if (cd "$TAXAMETER_DIR" && bash "$TAXAMETER_VERIFY_HOOK"); then echo "verify_hook=1"; else echo "verify_hook=0"; ok=0; fi
  fi
  echo "verify_ok=$ok"
}

model_dir() { echo "${1//\//__}"; }

remote_size() {  # content length of the final response after redirects
  curl -sIL --max-time 60 "$1" | tr -d '\r' | grep -i '^content-length:' | tail -1 | awk '{print $2}'
}

probe_speed() {  # bytes/s for the first 8 MB of a URL: best of 2 tries of 10 s (single probes were noisy)
  local s best=0
  for _ in 1 2; do
    s=$(curl -sL -r 0-8000000 -o /dev/null -w "%{speed_download}" --max-time 10 "$1" 2>/dev/null || echo 0)
    s=${s%.*}; [ "${s:-0}" -gt "$best" ] && best=$s
  done
  echo "$best"
}

pfetch() {  # pfetch URL OUT SIZE [N]: N parallel ranged requests (hosts throttle per connection: 8 x ~0.6 MB/s)
  local url="$1" out="$2" size="$3" n="${4:-16}" chunk i start end pids=()
  chunk=$(( (size + n - 1) / n ))
  for ((i = 0; i < n; i++)); do
    start=$(( i * chunk )); end=$(( start + chunk - 1 )); [ "$end" -ge "$size" ] && end=$(( size - 1 ))
    curl -sSL --retry 5 --max-time 3600 -r "$start-$end" -o "$out.part$i" "$url" &
    pids+=($!)
  done
  for p in "${pids[@]}"; do wait "$p"; done
  : > "$out"
  for ((i = 0; i < n; i++)); do cat "$out.part$i" >> "$out" && rm -f "$out.part$i"; done
}

models() {  # models org/name:file.gguf [org/name:a.gguf+b.gguf ...]
  local ok=1 spec
  for spec in "$@"; do
    local model="${spec%%:*}" files="" file
    if [ "$model" = "$spec" ]; then echo "FAIL  $spec: expected org/name:file.gguf"; ok=0; continue; fi
    files=$(echo "${spec#*:}" | tr '+' ' ')
    local dir
    dir="$TAXAMETER_DIR/$TAXAMETER_MODELS_DIR/$(model_dir "$model")"
    mkdir -p "$dir"
    # one downloader per model directory: a second `models`/provision run waits instead of writing the same files
    # (two curls once appended to one shard and corrupted it)
    exec 9> "$dir/.download.lock"
    flock 9
    # ModelScope is the default (Hugging Face was throttled on earlier hosts), but some hosts get ~10-50 KB/s from it:
    # probe both on the first file and take Hugging Face when it is 1.5x faster
    local base_ms="https://modelscope.cn/models/$model/resolve/master" base_hf="https://huggingface.co/$model/resolve/main"
    local big="${files%% *}" sp_ms sp_hf base="$base_ms"
    sp_ms=$(probe_speed "$base_ms/$big"); sp_hf=$(probe_speed "$base_hf/$big")
    if [ $(( ${sp_hf:-0} * 2 )) -gt $(( ${sp_ms:-0} * 3 )) ]; then base="$base_hf"; fi
    echo "source $model: $base (modelscope $(( ${sp_ms:-0} / 1024 )) KB/s, huggingface $(( ${sp_hf:-0} / 1024 )) KB/s)"
    for file in $files; do
      local url="$base/$file" want have
      mkdir -p "$(dirname "$dir/$file")"  # specs like org/x:1_Pooling/config.json
      want=$(remote_size "$url")
      have=$(stat -c %s "$dir/$file" 2>/dev/null || echo 0)
      if [ -n "$want" ] && [ "$have" = "$want" ]; then echo "ok    $model/$file ($have bytes)"; continue; fi
      for attempt in 1 2 3; do
        if [ -n "$want" ] && [ "$want" -gt 50000000 ]; then
          pfetch "$url" "$dir/$file" "$want"
        else
          # resume only when the size is known (servers without byte ranges fail with curl error 33 on -C -)
          if [ -n "$want" ]; then
            curl -sSL --retry 3 --max-time 3600 -C - -o "$dir/$file" "$url" || curl -sSL --max-time 3600 -o "$dir/$file" "$url"
          else
            curl -sSL --retry 3 --max-time 3600 -o "$dir/$file" "$url"
          fi
        fi
        have=$(stat -c %s "$dir/$file" 2>/dev/null || echo 0)
        { [ -z "$want" ] && [ "$have" -gt 0 ]; } || [ "$have" = "$want" ] && break
        echo "retry $model/$file attempt $attempt (have $have want $want)"; rm -f "${dir:?}/${file:?}"
      done
      if [ -n "$want" ] && [ "$have" != "$want" ]; then echo "FAIL  $model/$file ($have != $want)"; ok=0
      else echo "got   $model/$file ($have bytes)"; fi
    done
    flock -u 9
  done
  [ "$ok" = 1 ] && echo "models_ok=1" || { echo "models_ok=0"; return 1; }
}

run_job() {  # run <script> [args...]: detached; the script itself takes a flock, so a repeated launch is harmless
  local script="$1" log; shift
  cd "$TAXAMETER_DIR" || exit 1
  [ -f "$script" ] || { echo "no such script: $script"; exit 2; }
  mkdir -p "$TAXAMETER_LOGS_DIR"
  log="$TAXAMETER_LOGS_DIR/$(basename "$script" .sh).log"
  nohup setsid bash "$script" "$@" >> "$log" 2>&1 < /dev/null &
  sleep 3
  echo "launched $script at $(date +%T)"
  tail -n 5 "$log" 2>/dev/null
}

pack() {
  cd "$TAXAMETER_DIR" || exit 1
  local dirs=() d
  for d in "$@" ${TAXAMETER_PACK_EXTRA:-}; do [ -e "$d" ] && dirs+=("$d"); done
  [ "${#dirs[@]}" -gt 0 ] || { echo "nothing to pack"; exit 2; }
  bash "$TAXAMETER_HOME/pack_results.sh" "${dirs[@]}" && echo "md5=$(md5sum /root/results.tgz | cut -d' ' -f1)"
}

inventory() {
  cd "$TAXAMETER_DIR" 2>/dev/null || { echo "no_repo=1"; return; }
  local d r
  for d in "$TAXAMETER_MODELS_DIR"/*/; do [ -d "$d" ] && echo "model=$(basename "$d") $(du -sm "$d" | cut -f1)MB"; done
  for r in ${TAXAMETER_RESULTS:-}; do
    for d in "$r"/*/; do [ -d "$d" ] && echo "run=${d%/} done=$(find "$d" -name "$TAXAMETER_DONE_MARKER" | wc -l)"; done
  done
}

logs() {
  local n="${1:-30}"
  if [ -n "${TAXAMETER_BATCH_LOG:-}" ]; then
    echo "== $TAXAMETER_DIR/$TAXAMETER_BATCH_LOG"; tail -n "$n" "$TAXAMETER_DIR/$TAXAMETER_BATCH_LOG" 2>/dev/null
  fi
  echo "== /root/autostop.log"; tail -n "$n" /root/autostop.log 2>/dev/null
}

# Sourcing (tests) defines the functions only.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  case "$cmd" in
    health) health ;;
    ensure-watchdog) ensure_watchdog ;;
    install-push) install_push ;;
    provision) provision "$@" ;;
    verify) verify "$@" ;;
    models) models "$@" ;;
    run) run_job "$@" ;;
    pack) pack "$@" ;;
    inventory) inventory ;;
    logs) logs "$@" ;;
    *) echo "unknown command: $cmd" >&2; exit 2 ;;
  esac
fi
