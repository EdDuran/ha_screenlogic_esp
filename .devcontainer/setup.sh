#!/bin/bash

# Create config structure
mkdir -p /config/custom_components
mkdir -p /config/.storage

# Symlink integration
ln -sf /workspaces/ha_screenlogic_esp /config/custom_components/ha_screenlogic_esp

# Copy config files
cp /workspaces/ha_screenlogic_esp/.devcontainer/config/configuration.yaml /config/configuration.yaml

# Copy rate viewer
mkdir -p /config/www/ha_screenlogic_esp
cp /workspaces/ha_screenlogic_esp/www/esp_rate_viewer.html \
   /config/www/ha_screenlogic_esp/esp_rate_viewer.html

# Restore dashboard
cp /workspaces/ha_screenlogic_esp/.devcontainer/config/lovelace /config/.storage/lovelace

pip install homeassistant

### Enable stopping startup until debugger is started
#export HA_DEBUG=true

