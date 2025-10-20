from fastmcp import FastMCP  # 导入 FastMCP 框架，用于快速封装工具接口
import chromadb  # 导入 chromadb，用于知识库向量存储与检索
import requests  # 导入 requests，用于 HTTP 请求（如调用 Ollama、本地 Dify 工作流）
import os  # 导入 os，用于操作系统相关功能
import json  # 导入 json，用于数据序列化和反序列化
import threading  # 导入 threading，用于多线程（如 MQTT 监听）
from datetime import datetime, timedelta, timezone  # 导入 datetime，用于时间戳生成
import paho.mqtt.client as mqtt  # 导入 paho-mqtt，用于 MQTT 消息通信
from config import *  # 导入所有配置项（如 MQTT、Dify、日志等参数）
import hashlib
import time

# =============================
# 配置区
# =============================
OLLAMA_URL = "http://localhost:11434/api/embeddings"
MODEL = "bge-m3"
DB_PATH = "./knowledge_db"

# =============================
# 初始化 FastMCP
# =============================
mcp = FastMCP("KnowledgeBaseMCP")

# =============================
# 初始化 Chroma 数据库
# =============================
client = chromadb.PersistentClient(path=DB_PATH)
collection = client.get_or_create_collection(name="knowledge_base")

# =============================
# 日志存储配置
# =============================
control_log = []  # 存储所有运行记录
latest_sensor_data = None  # 缓存最近一次传感器数据
LOG_FILE = os.path.abspath(LOG_FILE)

# 去重缓存：用于记录 workflow 成功保存过的 control hash，防止随后收到相同 MQTT 再写入重复记录
_recent_success_hashes = {}  # hash -> timestamp (epoch seconds)
_recent_lock = threading.Lock()
DUPLICATE_WINDOW_SECONDS = 120  # 在此时间窗口内认为是重复（可调整）

# 加载已有日志（如果存在）
if os.path.exists(LOG_FILE):
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            control_log = json.load(f)
            if DEBUG:
                print(f"[LOG] 已加载 {len(control_log)} 条历史记录")
    except Exception as e:
        print("[WARN] 加载日志失败:", e)


def _cleanup_recent_hashes():
    """清理过期的 recent hashes（避免内存无限增长）"""
    now = time.time()
    with _recent_lock:
        keys = list(_recent_success_hashes.keys())
        for k in keys:
            if now - _recent_success_hashes[k] > DUPLICATE_WINDOW_SECONDS:
                del _recent_success_hashes[k]


def _make_record_hash(sensor_data: dict, control_data: dict):
    """对 sensor+control 生成稳定的 hash（JSON 序列化后 MD5）"""
    try:
        combined = {
            "sensor": sensor_data,
            "control": control_data
        }
        s = json.dumps(combined, sort_keys=True, ensure_ascii=False)
        return hashlib.md5(s.encode("utf-8")).hexdigest()
    except Exception:
        s = json.dumps(control_data, sort_keys=True, ensure_ascii=False)
        return hashlib.md5(s.encode("utf-8")).hexdigest()


def save_log_file():
    """保存日志并清理 7 天前记录，同时控制最大条数"""
    try:
        now = datetime.utcnow().replace(tzinfo=timezone.utc)
        seven_days_ago = now - timedelta(days=7)
        filtered_log = []
        for entry in control_log:
            ts = entry.get("timestamp")
            if not ts:
                continue
            try:
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                dt_utc = dt.astimezone(timezone.utc)
            except Exception:
                filtered_log.append(entry)
                continue
            if dt_utc >= seven_days_ago:
                filtered_log.append(entry)

        max_h = MAX_HISTORY if MAX_HISTORY else 1000
        to_write = filtered_log[-max_h:]
        dirpath = os.path.dirname(LOG_FILE)
        if dirpath:
            os.makedirs(dirpath, exist_ok=True)
        tmp_path = LOG_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(to_write, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp_path, LOG_FILE)
        if DEBUG:
            print(f"[LOG] 保存 {len(to_write)} 条日志 -> {LOG_FILE}")
    except Exception as e:
        if DEBUG:
            print("[WARN] save_log_file failed:", e)


def record_combined(sensor_data: dict, control_data: dict, source: str = "workflow"):
    """拼接传感器与控制消息并保存"""
    if sensor_data is None or control_data is None:
        if DEBUG:
            print("[WARN] sensor_data 或 control_data 为 None，跳过保存")
        return

    beijing_tz = timezone(timedelta(hours=8))
    entry = {
        "timestamp": datetime.now(beijing_tz).isoformat(),
        "sensor_data": sensor_data,
        "control_command": control_data,
        "source": source
    }
    control_log.append(entry)
    save_log_file()
    if DEBUG:
        print("[LOG] 新增运行记录：", json.dumps(entry, ensure_ascii=False, indent=2))

    if source == "workflow":
        try:
            h = _make_record_hash(sensor_data, control_data)
            with _recent_lock:
                _recent_success_hashes[h] = time.time()
            _cleanup_recent_hashes()
            if DEBUG:
                print(f"[LOG] 已缓存 workflow 记录哈希，供后续去重：{h}")
        except Exception:
            pass


# =============================
# 向量化函数
# =============================
def embed_text(text: str):
    response = requests.post(
        OLLAMA_URL,
        json={"model": MODEL, "prompt": text}
    )
    data = response.json()
    return data["embedding"]


# =============================
# 工具1：知识检索
# =============================
@mcp.tool("search_knowledge", description="检索知识库中与查询最相关的知识")
def search_knowledge(query: str, top_k: int = 3):
    query_emb = embed_text(query)
    results = collection.query(query_embeddings=[query_emb], n_results=top_k)
    matches = []
    for i in range(len(results["ids"][0])):
        matches.append({
            "id": results["ids"][0][i],
            "text": results["documents"][0][i],
            "score": results["distances"][0][i],
            "metadata": results["metadatas"][0][i]
        })
    return {"query": query, "results": matches}


# =============================
# 工具2：查看传感器历史数据
# =============================
@mcp.tool("get_history", description="查看传感器历史数据")
def get_history():
    return [log["sensor_data"] for log in control_log[-MAX_HISTORY:] if "sensor_data" in log]


# =============================
# 工具3：查看控制指令日志
# =============================
@mcp.tool("get_control_log", description="查看控制日志")
def get_control_log():
    return control_log[-MAX_HISTORY:]


# =============================
# ✅ 改进后的 Dify 工作流触发逻辑
# =============================
def trigger_dify_workflow(sensor_data: dict):
    """触发 Dify 工作流进行数据处理
    
    改进：
    - 优化错误处理，使用更简洁的错误提示
    - 统一错误提示格式
    - 增加请求超时设置为 30s
    - 增加重试机制（最多重试 2 次）
    """
    headers = {"Authorization": f"Bearer {DIFY_API_KEY}"}
    payload = {"inputs": sensor_data, "user": "abc-123"}
    max_retries = 2
    retry_count = 0

    while retry_count <= max_retries:
        try:
            r = requests.post(DIFY_WORKFLOW_RUN_URL, 
                            json=payload, 
                            headers=headers, 
                            timeout=30)
            
            if r.status_code == 200:
                result = r.json()
                control_output = None
                
                if isinstance(result, dict):
                    if "outputs" in result and isinstance(result["outputs"], dict):
                        control_output = result["outputs"].get("output") or result["outputs"].get("control")
                    else:
                        control_output = result.get("control") or result.get("output")

                if control_output:
                    record_combined(sensor_data, control_output, source="workflow")
                    return
                elif DEBUG:
                    print("[DIFY] ℹ️ Workflow 返回无控制输出，跳过记录")
                return
            
            elif r.status_code == 429:  # 限流错误
                if retry_count < max_retries:
                    retry_count += 1
                    time.sleep(2 ** retry_count)  # 指数退避
                    continue
                print("[DIFY] ⚠️ 服务限流，请稍后重试")
                return
                
            else:
                print(f"[DIFY] ❌ HTTP {r.status_code}")
                return

        except requests.exceptions.ReadTimeout:
            if retry_count < max_retries:
                retry_count += 1
                continue
            print("[DIFY] ⚠️ 请求超时 (30s)")
            return
            
        except requests.exceptions.ConnectionError:
            print("[DIFY] ❌ 连接失败，请检查服务状态")
            return
            
        except Exception as e:
            error_msg = str(e)
            if len(error_msg) > 100:  # 截断过长的错误消息
                error_msg = error_msg[:100] + "..."
            print(f"[DIFY] ⚠️ {error_msg}")


# =============================
# MQTT 相关逻辑
# =============================
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        if DEBUG:
            print("[MQTT] Connected")
        client.subscribe(TOPIC_SENSOR)
        client.subscribe(TOPIC_CONTROL)
    else:
        print("[MQTT] Connect failed, rc=", rc)


def on_message(client, userdata, msg):
    global latest_sensor_data
    try:
        payload = json.loads(msg.payload.decode())
    except Exception:
        payload = {"raw": msg.payload.decode()}

    if msg.topic == TOPIC_SENSOR:
        latest_sensor_data = payload
        if DEBUG:
            print("[MQTT] Received sensor data:", payload)
        threading.Thread(target=trigger_dify_workflow, args=(payload,), daemon=True).start()

    elif msg.topic == TOPIC_CONTROL:
        if DEBUG:
            print("[MQTT] Received control command:", payload)
        h = _make_record_hash(latest_sensor_data or {}, payload)
        skip = False
        with _recent_lock:
            ts = _recent_success_hashes.get(h)
            if ts and (time.time() - ts) <= DUPLICATE_WINDOW_SECONDS:
                skip = True
        if skip:
            if DEBUG:
                print(f"[MQTT] 跳过重复控制（hash: {h}）")
        else:
            if latest_sensor_data:
                record_combined(latest_sensor_data, payload, source="mqtt")
            else:
                record_combined({}, payload, source="mqtt")


def mqtt_start():
    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_start()
        if DEBUG:
            print(f"[MQTT] Started client and subscribed to {TOPIC_SENSOR} & {TOPIC_CONTROL}")
    except Exception as e:
        print("[MQTT] Failed to start MQTT client:", e)


def ensure_mqtt_started():
    if not getattr(ensure_mqtt_started, "started", False):
        threading.Thread(target=mqtt_start, daemon=True).start()
        ensure_mqtt_started.started = True


ensure_mqtt_started()

# =============================
# 主入口
# =============================
if __name__ == "__main__":
    mcp.run()
