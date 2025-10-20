# ==========================================
# config.py - global configuration for MCP Adapter (FastAPI)
# ==========================================

# MQTT broker configuration
MQTT_BROKER = "broker.emqx.io"
MQTT_PORT = 1883
TOPIC_SENSOR = "plc/sensordata"
TOPIC_CONTROL = "plc/control_cmd"

# MCP HTTP service settings
MCP_HOST = "0.0.0.0"
MCP_PORT = 8439

# Simulation
SIMULATOR_ENABLED = False
SIMULATOR_INTERVAL = 5  # seconds

# History & Logging
HISTORY_FILE = "data_history.json"
LOG_FILE = "control_log.json"
MAX_HISTORY = 1000

# Debug
DEBUG = True

# Dify workflow settings
DIFY_URL = "http://docker-api-1:5001"
DIFY_WORKFLOW_RUN_URL = "http://localhost:5001/v1/workflows/run"
DIFY_API_KEY = "app-pe3zGlbWrSJWRyPFoNEL5jfO"
