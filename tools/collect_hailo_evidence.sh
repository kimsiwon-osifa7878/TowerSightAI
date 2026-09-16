#!/usr/bin/env bash
# Collect Hailo-8 hang evidence into one folder BEFORE any recovery (re-enumerate/reboot zero the
# AER counters). Read-only: nothing here restarts, removes, or rescans anything.
# Usage: tools/collect_hailo_evidence.sh [output-dir]   → prints the folder; send it via NAS 파일 전송.
set -u
OUT="${1:-$HOME/hailo-hang-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$OUT"
cd "$(dirname "$0")/.." 2>/dev/null || true
run() { local name="$1"; shift; { echo "\$ $*"; "$@"; echo "[exit=$?]"; } >"$OUT/$name" 2>&1; }

DEV=$(for d in /sys/bus/pci/devices/*; do grep -q 0x1e60 "$d/vendor" 2>/dev/null && basename "$d"; done | head -1)
PORT=""; [ -n "$DEV" ] && PORT=$(basename "$(readlink -f "/sys/bus/pci/devices/$DEV/..")")
{
  echo "collected_at=$(date -Is) host=$(hostname) uptime=$(uptime -p)"
  echo "hailo_pci_device=$DEV upstream_port=$PORT"
} > "$OUT/00-summary.txt"

# 1. PCIe link + AER (the decisive fields: LnkSta speed, RxErr count, endpoint vs port)
{
  for d in "$DEV" "$PORT"; do
    [ -z "$d" ] && continue
    echo "== $d"; for f in current_link_speed current_link_width max_link_speed max_link_width aer_dev_correctable aer_dev_nonfatal aer_dev_fatal; do
      printf '%s: ' "$f"; cat "/sys/bus/pci/devices/$d/$f" 2>/dev/null | tr '\n' ' '; echo; done
  done
  echo "== lspci -vvv (needs sudo for full AER/LnkSta)"; (sudo -n lspci -vvv -s "${DEV#0000:}" 2>/dev/null || lspci -vvv -s "${DEV#0000:}") 2>&1
} > "$OUT/01-pcie-aer.txt" 2>&1
# 2. Driver / node / kernel messages
run 02-driver.txt sh -c "lsmod | grep -i hailo; cat /sys/module/hailo_pci/version; ls -la /dev/hailo*; dmesg 2>/dev/null | grep -iE 'hailo|pcieport|AER|02:00' | tail -80 || journalctl -k --no-pager -n 3000 2>/dev/null | grep -iE 'hailo|pcieport|AER' | tail -80"
# 3. Who holds the device / cameras (orphans starve the chip without any hardware fault)
run 03-processes.txt sh -c "fuser -v /dev/hailo0; ss -tnp | grep ':554'; pgrep -af 'operator_ui|Multisour|hailo_apps_detection|event_video_recorder|hailortcli'"
# 4. Does the chip answer? (short timeout so a hung chip does not block collection)
run 04-identify.txt timeout 20 hailortcli fw-control identify
run 04b-scan.txt timeout 20 hailortcli scan
# 5. Temperature / power / thermal throttling of the host
run 05-thermal.txt sh -c "cat /sys/class/thermal/thermal_zone*/type; cat /sys/class/thermal/thermal_zone*/temp; sensors 2>/dev/null; cat /proc/loadavg; free -m"
# 6. Application evidence: health-monitor rows, fatal lines, last child log
{
  echo "== towersightai.hailo.health (last 40)"; grep -h "hailo-health" artifacts/runtime/towersightai.log* 2>/dev/null | tail -40
  echo "== ai-fatal / restart / OUT_OF (last 60)"; grep -hE "ai-fatal|ai-process-restart-request|ai-process-recovery-incomplete|OUT_OF_PHYSICAL|DRIVER_OPERATION_FAILED|CHECK_SUCCESS" artifacts/runtime/towersightai.log* 2>/dev/null | tail -60
  echo "== newest child log tail"; f=$(ls -t artifacts/runtime/purpose-ai/*/*.gst.log 2>/dev/null | head -1); echo "$f"; tail -60 "$f" 2>/dev/null | grep -v PIPELINE_DIAGNOSTIC
} > "$OUT/06-app-logs.txt" 2>&1
cp artifacts/runtime/towersightai.log "$OUT/towersightai.log" 2>/dev/null
sed -i -E 's#(rtsp://)[^@ ]+@#\1***:***@#g' "$OUT"/*.txt "$OUT"/towersightai.log 2>/dev/null
echo "$OUT"
