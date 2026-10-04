"""Constants for the DJI Power local BLE integration."""

DOMAIN = "dji_power_ble"

CONF_ADDRESS = "address"
CONF_PAIR_KEY = "pair_key"
CONF_NAME = "name"
CONF_MODEL = "model"
CONF_SERIAL_NUMBER = "serial_number"
CONF_UPDATE_INTERVAL = "update_interval"
CONF_CONNECTION_SOURCE = "connection_source"
CONF_KEEP_CONNECTION = "keep_connection"
# Unsupported: lets manual recharge power go below the station's minimum.
CONF_RECHARGE_POWER_MINIMUM = "recharge_power_minimum"

CONNECTION_SOURCE_AUTOMATIC = "automatic"

DEFAULT_UPDATE_INTERVAL = 5
MIN_UPDATE_INTERVAL = 0
MAX_UPDATE_INTERVAL = 60
MANUFACTURER_ID = 0x08AA
