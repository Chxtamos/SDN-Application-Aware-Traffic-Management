#!/usr/bin/env bash
set -euo pipefail
LAB=$1
cd "$LAB/repo"
bash "$LAB/state/preflight.sh" | tee "$LAB/evidence/preflight-before-live.txt"
/usr/bin/python3 -B "$LAB/state/teammate-manifest.py" > "$LAB/state/manifest-before.txt"
controller_pid=
on_exit() {
    local result=$?
    trap - EXIT INT TERM
    set +e
    if [[ -n "$controller_pid" ]]; then
        if jobs -pr | grep -Fxq "$controller_pid"; then
            kill -TERM "$controller_pid"
        fi
        wait "$controller_pid"
    fi
    bash "$LAB/state/preflight.sh" > "$LAB/evidence/post-cleanup.txt" 2>&1
    local resources=$?
    /usr/bin/python3 -B "$LAB/state/teammate-manifest.py" > "$LAB/state/manifest-after.txt"
    local generated=$?
    cmp -s "$LAB/state/manifest-before.txt" "$LAB/state/manifest-after.txt"
    local comparison=$?
    {
        printf 'before_entries: '; wc -l < "$LAB/state/manifest-before.txt"
        printf 'after_entries: '; wc -l < "$LAB/state/manifest-after.txt"
        sha256sum "$LAB/state/manifest-before.txt" "$LAB/state/manifest-after.txt" | awk '{print $1}'
        printf 'post_cleanup_status=%d\nintegrity_manifest_status=%d\nintegrity_comparison_status=%d\n' \
          "$resources" "$generated" "$comparison"
    } | tee "$LAB/evidence/cleanup-and-integrity.txt"
    if (( result == 0 && (resources || generated || comparison) )); then result=4; fi
    exit "$result"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
export PYTHONDONTWRITEBYTECODE=1
SDN_RUN_DIR="$LAB/evidence" SDN_QOS=1 SDN_ADAPTIVE=0 "$HOME/ryu-env/bin/ryu-manager" \
    --ofp-listen-host 127.0.0.1 --ofp-tcp-listen-port 17655 \
    controller/ryu_controller.py > "$LAB/evidence/controller.txt" 2>&1 &
controller_pid=$!
ready=
for attempt in $(seq 1 40); do
    kill -0 "$controller_pid"
    if ss -H -lnt 'sport = :17655' | grep -Fq '127.0.0.1:17655'; then ready=1; break; fi
    sleep 0.25
done
test "$ready" = 1
sudo -n timeout --signal=TERM --kill-after=16s 180s /usr/bin/python3 -B \
    "$LAB/evidence/validation-driver.py" "$LAB" 2>&1 | tee "$LAB/evidence/live-validation.txt"
