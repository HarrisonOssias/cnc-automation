#!/usr/bin/env bash
# First-time setup of a Raspberry Pi (Raspberry Pi OS Lite, 64-bit). Copy source/edge/pi/ to the Pi, then:
#   sudo ./install.sh --machine-id haas-vf2-01 --token <machine token> \
#                     --backend https://forge-backend.example --mqtt forge-mqtt.example
# After this, the Pi updates itself (forge-update.timer), so this script is only run once.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }

MQTT_PORT=1883 MQTT_USER=forge MQTT_PASSWORD=forge
while [[ $# -gt 0 ]]; do
  case $1 in
    --machine-id) MACHINE_ID=$2 ;;
    --token) MACHINE_TOKEN=$2 ;;
    --backend) BACKEND_URL=$2 ;;
    --mqtt) MQTT_HOST=$2 ;;
    --mqtt-user) MQTT_USER=$2 ;;
    --mqtt-password) MQTT_PASSWORD=$2 ;;
    *) echo "unknown option $1"; exit 1 ;;
  esac
  shift 2
done
: "${MACHINE_ID:?--machine-id is required}" "${MACHINE_TOKEN:?--token is required}"
: "${BACKEND_URL:?--backend is required}" "${MQTT_HOST:?--mqtt is required}"

echo "== packages"
apt-get update
apt-get install -y --no-install-recommends cage chromium python3-venv curl ca-certificates

echo "== users"
id forge &>/dev/null || useradd --system --home-dir /var/lib/forge --shell /usr/sbin/nologin forge
id kiosk &>/dev/null || useradd --create-home --groups video,render,input kiosk

echo "== config"
mkdir -p /etc/forge /opt/forge/releases
install -m 640 -g forge /dev/stdin /etc/forge/agent.env <<EOF
MACHINE_ID=$MACHINE_ID
MACHINE_TOKEN=$MACHINE_TOKEN
BACKEND_URL=$BACKEND_URL
MQTT_HOST=$MQTT_HOST
MQTT_PORT=$MQTT_PORT
MQTT_USER=$MQTT_USER
MQTT_PASSWORD=$MQTT_PASSWORD
EOF
# Power cuts are normal on a shop floor: keep logs in RAM to spare the SD card.
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=volatile\n' > /etc/systemd/journald.conf.d/forge.conf

echo "== first release"
"$(dirname "$0")/update.sh"  # downloads the latest release, installs its systemd units, starts the agent

echo "== services"
systemctl enable forge-agent.service forge-kiosk.service forge-update.timer
systemctl start forge-update.timer
systemctl set-default graphical.target
echo "done: $MACHINE_ID is running $(readlink /opt/forge/current)"
