#!/usr/bin/env bash
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
  echo "Uruchom jako root lub przez sudo: sudo $0"
  exit 1
fi

DB_NAME="autocore"
DB_USER="postgres"

if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1; then
  echo "Tworzę bazę danych $DB_NAME"
  sudo -u postgres psql -c "CREATE DATABASE $DB_NAME"
fi

echo "Tworzę tabele AutoCore w bazie $DB_NAME..."
sudo -u postgres psql -d "$DB_NAME" <<'EOF'
BEGIN;

CREATE TABLE IF NOT EXISTS products (
    id SERIAL PRIMARY KEY,
    barcode TEXT UNIQUE,
    product_code TEXT,
    oe_code TEXT,
    name TEXT,
    models TEXT,
    category TEXT,
    product_type TEXT,
    side TEXT,
    position TEXT,
    description TEXT,
    price REAL DEFAULT 0,
    stock INTEGER DEFAULT 0,
    signeda_stock TEXT DEFAULT '0',
    condition_rating INTEGER DEFAULT 0,
    damage_description TEXT,
    source TEXT DEFAULT 'manual',
    external_id TEXT,
    last_sync TIMESTAMP,
    force_price INTEGER DEFAULT 0,
    custom_price REAL DEFAULT 0,
    olx_offer_id TEXT,
    photos_folder TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS archived_products (
    id SERIAL PRIMARY KEY,
    barcode TEXT UNIQUE,
    product_code TEXT,
    oe_code TEXT,
    name TEXT,
    models TEXT,
    category TEXT,
    product_type TEXT,
    side TEXT,
    position TEXT,
    description TEXT,
    price REAL DEFAULT 0,
    stock INTEGER DEFAULT 0,
    signeda_stock TEXT DEFAULT '0',
    condition_rating INTEGER DEFAULT 0,
    damage_description TEXT,
    source TEXT DEFAULT 'manual',
    external_id TEXT,
    last_sync TIMESTAMP,
    force_price INTEGER DEFAULT 0,
    custom_price REAL DEFAULT 0,
    olx_offer_id TEXT,
    photos_folder TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    archived_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,
    customer_name TEXT,
    phone TEXT,
    address TEXT,
    nip TEXT,
    document_type TEXT,
    delivery_type TEXT,
    status TEXT,
    package_weight REAL,
    package_length REAL,
    package_width REAL,
    package_height REAL,
    shipping_free INTEGER DEFAULT 0,
    discount REAL DEFAULT 0,
    total_price REAL DEFAULT 0,
    salesperson_id INTEGER,
    warehouse_worker_id INTEGER,
    extra_info TEXT,
    email TEXT,
    cod_type TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS packages (
    id SERIAL PRIMARY KEY,
    order_id INTEGER NOT NULL,
    type TEXT DEFAULT 'PACZKA',
    weight REAL,
    length REAL,
    width REAL,
    height REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS order_items (
    id SERIAL PRIMARY KEY,
    order_id INTEGER,
    barcode TEXT,
    product_name TEXT,
    side TEXT,
    position TEXT,
    picked INTEGER DEFAULT 0,
    to_order INTEGER DEFAULT 0,
    unit_price REAL DEFAULT NULL,
    custom_name TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS salespersons (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS warehouse_workers (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS salesperson_ips (
    id SERIAL PRIMARY KEY,
    salesperson_id INTEGER NOT NULL,
    ip_address TEXT NOT NULL,
    note TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS customers (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT,
    address TEXT,
    nip TEXT,
    email TEXT,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS order_templates (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    model TEXT,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS order_template_items (
    id SERIAL PRIMARY KEY,
    template_id INTEGER NOT NULL,
    barcode TEXT NOT NULL,
    quantity INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS inventory_reservations (
    id SERIAL PRIMARY KEY,
    order_id INTEGER NOT NULL,
    order_item_id INTEGER,
    product_id INTEGER NOT NULL,
    barcode TEXT NOT NULL,
    qty INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    consumed_at TIMESTAMP,
    released_at TIMESTAMP,
    created_by INTEGER,
    note TEXT
);

CREATE TABLE IF NOT EXISTS inventory_movements (
    id SERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL,
    barcode TEXT NOT NULL,
    delta INTEGER NOT NULL,
    reason TEXT NOT NULL,
    order_id INTEGER,
    order_item_id INTEGER,
    reservation_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_by INTEGER,
    details JSONB
);

CREATE TABLE IF NOT EXISTS audit_log (
    id SERIAL PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id INTEGER,
    action TEXT NOT NULL,
    actor_id INTEGER,
    details JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS favorite_products (
    id SERIAL PRIMARY KEY,
    barcode TEXT NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    note TEXT
);

CREATE INDEX IF NOT EXISTS idx_inventory_reservations_order ON inventory_reservations(order_id);
CREATE INDEX IF NOT EXISTS idx_inventory_reservations_product_status ON inventory_reservations(product_id, status);
CREATE INDEX IF NOT EXISTS idx_inventory_movements_product_time ON inventory_movements(product_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_log_entity_time ON audit_log(entity_type, entity_id, created_at);

COMMIT;
EOF

echo "Gotowe. Sprawdź tabele:
  sudo -u postgres psql -d $DB_NAME -c '\dt'"
