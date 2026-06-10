#!/usr/bin/env bash

# === USER CONFIGURATION ===
SAMPLE_FILE="./example-empty-route.geojson"   # path to your sample file
TARGET_DIR="./"     # folder where you want copies to go

# === LIST OF PROJECT IDs ===
PROJECT_IDS=(
P7987
P7988
P7989
P7990
P7992
P7993
)

# === CREATE TARGET DIR IF NOT EXISTS ===
mkdir -p "$TARGET_DIR"

# === LOOP AND COPY ===
for id in "${PROJECT_IDS[@]}"; do
    cp "$SAMPLE_FILE" "${TARGET_DIR}/${id}.geojson"
    echo "Created ${TARGET_DIR}/${id}.geojson"
done

echo "✅ All files created."