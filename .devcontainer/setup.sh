#!/bin/bash
set -xe

# Create config structure
mkdir -p /config/custom_components
mkdir -p /config/.storage

# Symlink integration
ln -sf /workspaces/pool_esp/custom_components/pool_esp /config/custom_components/pool_esp

# Copy config files
cp /workspaces/pool_esp/.devcontainer/config/configuration.yaml /config/configuration.yaml

# Copy rate viewer
mkdir -p /config/www/pool_esp
cp /workspaces/pool_esp/www/esp_rate_viewer.html \
   /config/www/pool_esp/esp_rate_viewer.html

# Restore dashboard
cp /workspaces/pool_esp/.devcontainer/config/lovelace /config/.storage/lovelace

pip install homeassistant

### Enable stopping startup until debugger is started
#export HA_DEBUG=true

