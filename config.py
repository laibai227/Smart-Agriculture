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
# 可配置项：
# in-flight TTL（秒）——当客户端超时后，在本进程视为该请求仍在进行，不再重复触发（默认 300s）
DIFY_INFLIGHT_TTL = 300
# requests.post 超时时间（秒），可调整以减少客户端提前超时（默认 30s）
DIFY_REQUEST_TIMEOUT = 60
# 当 workflow 返回 200 但输出为空时，最大重试次数（默认 3 次）
DIFY_EMPTY_RETRIES = 3
