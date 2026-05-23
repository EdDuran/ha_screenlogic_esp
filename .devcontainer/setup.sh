#!/bin/bash
set -xe

# Create config structure
mkdir -p /config/custom_components
mkdir -p /config/.storage
mkdir -p /config/test

# Symlink integration
ln -sf /workspaces/ha_screenlogic_esp/custom_components/pool_esp /config/custom_components/pool_esp

# Copy config files
cp /workspaces/ha_screenlogic_esp/.devcontainer/config/configuration.yaml /config/configuration.yaml

# Copy rate viewer
mkdir -p /config/www/pool_esp
cp /workspaces/ha_screenlogic_esp/www/esp_rate_viewer.html \
   /config/www/pool_esp/esp_rate_viewer.html

# Restore dashboard
cp /workspaces/ha_screenlogic_esp/.devcontainer/config/lovelace_dashboards /config/.storage/lovelace
cp /workspaces/ha_screenlogic_esp/.devcontainer/config/lovelace.pool_esp /config/.storage/lovelace.pool_esp

# Copy test scenarios and data
cp /workspaces/ha_screenlogic_esp/test/* /config/test

pip install homeassistant

### Enable stopping startup until debugger is started
#export HA_DEBUG=true

