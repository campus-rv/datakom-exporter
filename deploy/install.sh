#!/bin/sh
# Install or upgrade datakom-exporter on a Raspberry Pi (Raspberry Pi OS / Debian).
# Run from the repository root:  sudo ./deploy/install.sh
set -eu

PREFIX=/opt/datakom-exporter
ENV_FILE=/etc/default/datakom-exporter
UNIT=/etc/systemd/system/datakom-exporter.service

if ! python3 -c 'import venv' 2>/dev/null; then
    apt-get install -y python3-venv
fi

install -d "$PREFIX"
[ -x "$PREFIX/venv/bin/python" ] || python3 -m venv "$PREFIX/venv"
# --no-cache-dir: no pip cache on the SD card. pip byte-compiles at install time,
# so the service never has to write __pycache__.
"$PREFIX/venv/bin/pip" install --no-cache-dir --disable-pip-version-check --upgrade .
install -m 0644 README.md "$PREFIX/README.md"

if [ ! -f "$ENV_FILE" ]; then
    install -m 0644 deploy/datakom-exporter.env "$ENV_FILE"
    echo "created $ENV_FILE - set DATAKOM_HOST there"
fi
install -m 0644 deploy/datakom-exporter.service "$UNIT"
systemctl daemon-reload
systemctl enable datakom-exporter.service

if grep -q '^DATAKOM_HOST=.\+' "$ENV_FILE"; then
    systemctl restart datakom-exporter.service
    systemctl --no-pager status datakom-exporter.service || true
else
    echo "set DATAKOM_HOST in $ENV_FILE, then: systemctl start datakom-exporter"
fi
