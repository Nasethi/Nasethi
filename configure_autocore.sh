#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"

PG_HOST="${1:-$(hostname -I | awk '{print $1}' | sed 's/\s.*//') }"
if [ -z "$PG_HOST" ]; then
  PG_HOST="127.0.0.1"
fi
PG_PORT="5432"
PG_DBNAME="autocore"
PG_USER="postgres"
PG_PASSWORD="admin"

CONFIG_FILE="$PROJECT_DIR/config.ini"

cat > "$CONFIG_FILE" <<EOF
[PostgreSQL]
host = $PG_HOST
port = $PG_PORT
dbname = $PG_DBNAME
user = $PG_USER
password = $PG_PASSWORD
EOF

PHOTOS_DIR="$PROJECT_DIR/photos"
SHARE_DIR="/srv/autocore_photos"

if [ -e "$SHARE_DIR" ] || [ -L "$SHARE_DIR" ]; then
  echo "Udział zdjęć istnieje: $SHARE_DIR"
else
  if [ "$(id -u)" -ne 0 ]; then
    echo "Uwaga: nie masz uprawnień do utworzenia $SHARE_DIR. Uruchom jako root, jeśli chcesz utworzyć udział sieciowy." >&2
  else
    mkdir -p "$SHARE_DIR"
    chown nobody:nogroup "$SHARE_DIR" || true
    chmod 2775 "$SHARE_DIR" || true
    echo "Utworzono katalog współdzielony: $SHARE_DIR"
  fi
fi

if [ -e "$PHOTOS_DIR" ] && [ ! -L "$PHOTOS_DIR" ]; then
  echo "Uwaga: katalog $PHOTOS_DIR już istnieje i nie jest symlinkiem. Nie nadpisano go."
else
  rm -f "$PHOTOS_DIR" || true
  ln -s "$SHARE_DIR" "$PHOTOS_DIR"
  echo "Utworzono symlink: $PHOTOS_DIR -> $SHARE_DIR"
fi

echo ""
echo "Konfiguracja programu AutoCore została przygotowana."
echo "Plik konfiguracyjny: $CONFIG_FILE"
echo "Ścieżka zdjęć: $PHOTOS_DIR"
echo "Domyślny host PostgreSQL: $PG_HOST"

echo "Zainstalowane programy (ścieżki):"
echo "  psql: $(command -v psql || echo 'brak')"
echo "  postgres: $(command -v postgres || echo 'brak')"
echo "  smbd: $(command -v smbd || echo 'brak')"
echo "  python3: $(command -v python3 || echo 'brak')"
echo "  Samba config: /etc/samba/smb.conf"
echo "  AutoCore program folder: $PROJECT_DIR"

echo ""
echo "Jeśli chcesz użyć innego hosta PostgreSQL, uruchom skrypt jako:"
echo "  ./configure_autocore.sh 192.168.x.y"
