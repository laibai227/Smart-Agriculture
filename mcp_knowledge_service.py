from fastmcp import FastMCP  # 导入 FastMCP 框架，用于快速封装工具接口，使其能被 MCP 客户端（如 Claude Desktop）调用
import chromadb  # 导入 chromadb，用于知识库向量存储与检索，实现语义搜索功能
import requests  # 导入 requests，用于 HTTP 请求（如调用 Ollama 嵌入服务、本地 Dify 工作流）
import os  # 导入 os，用于操作系统相关功能，如文件路径操作
import json  # 导入 json，用于数据序列化和反序列化（Python对象与JSON字符串互转）
import threading  # 导入 threading，用于多线程处理（如 MQTT 监听与 Dify 触发分离）
from datetime import datetime, timedelta, timezone  # 导入 datetime，用于时间戳生成和时区转换
import paho.mqtt.client as mqtt  # 导入 paho-mqtt，用于 MQTT 消息通信（订阅传感器数据、发布控制指令）
from config import *  # 从 config.py 导入所有配置项（如 MQTT、Dify、日志等参数，DEBUG开关）
import hashlib  # 导入 hashlib，用于生成数据的 MD5 指纹（去重和缓存键）
import time  # 导入 time，用于时间戳获取和休眠延迟

# =============================
# 配置区：定义全局常量参数
# =============================
OLLAMA_URL = "http://localhost:11434/api/embeddings"  # Ollama 嵌入服务地址，用于将文本转为向量
MODEL = "bge-m3"  # 指定 Ollama 使用的嵌入模型名称
DB_PATH = "./knowledge_db"  # ChromaDB 持久化存储路径，向量数据库文件存放位置

# =============================
# 初始化 FastMCP：创建 MCP 服务器实例
# =============================
mcp = FastMCP("KnowledgeBaseMCP")  # 创建名为 "KnowledgeBaseMCP" 的 FastMCP 服务器实例

# =============================
# 初始化 Chroma 数据库：连接到本地向量数据库
# =============================
client = chromadb.PersistentClient(path=DB_PATH)  # 创建 Chroma 持久化客户端，连接到 DB_PATH 路径的数据库
collection = client.get_or_create_collection(name="knowledge_base")  # 获取或创建名为 "knowledge_base" 的集合（collection），用于存储知识向量

# =============================
# 日志存储配置：定义全局变量用于日志管理
# =============================
control_log = []  # 定义空列表，用于存储所有运行记录（传感器+控制指令的组合记录）
latest_sensor_data = None  # 定义全局变量，用于缓存最近一次接收到的传感器数据，供后续控制指令关联
LOG_FILE = os.path.abspath(LOG_FILE)  # 将 LOG_FILE 转换为绝对路径，确保跨平台路径正确性

# 去重缓存：用于记录 workflow 成功保存过的 control hash，防止随后收到相同 MQTT 再写入重复记录
_recent_success_hashes = {}  # 创建空字典，存储已记录的控制指令哈希值，键为 hash，值为时间戳
_recent_lock = threading.Lock()  # 创建线程锁，保护 _recent_success_hashes 的并发读写安全
DUPLICATE_WINDOW_SECONDS = 120  # 定义重复窗口期（秒），在此时间内相同控制指令视为重复并跳过

# in-flight 缓存：记录已经触发但尚未确认完成的 workflow，避免重复触发
# key: record hash -> timestamp (epoch seconds)
_inflight_workflows = {}  # 创建空字典，存储正在执行中的工作流指纹，键为传感器数据 hash，值为触发时间戳
_inflight_lock = threading.Lock()  # 创建线程锁，保护 _inflight_workflows 的并发读写安全

# in-flight 默认 TTL（秒），可以在 config.py 中定义 DIFY_INFLIGHT_TTL 来覆盖
INFLIGHT_TTL = globals().get("DIFY_INFLIGHT_TTL", 300)  # 从全局变量获取 in-flight 保护时长，默认 300 秒（5 分钟）

# 加载已有日志（如果存在文件）
if os.path.exists(LOG_FILE):  # 检查日志文件是否存在
    try:  # 尝试执行以下代码块
        with open(LOG_FILE, "r", encoding="utf-8") as f:  # 以 UTF-8 编码只读模式打开日志文件
            control_log = json.load(f)  # 将 JSON 文件内容反序列化为 Python 列表，赋值给 control_log
            if DEBUG:  # 如果 DEBUG 模式开启
                print(f"[LOG] 已加载 {len(control_log)} 条历史记录")  # 打印加载的历史记录数量
    except Exception as e:  # 捕获加载过程中的任何异常
        print("[WARN] 加载日志失败:", e)  # 打印警告信息和异常详情


def _cleanup_recent_hashes():
    """清理过期的 recent hashes（避免内存无限增长）"""
    now = time.time()  # 获取当前时间戳（秒）
    with _recent_lock:  # 获取锁，确保线程安全
        keys = list(_recent_success_hashes.keys())  # 获取所有已存储的 hash 键列表
        for k in keys:  # 遍历每个 hash 键
            if now - _recent_success_hashes[k] > DUPLICATE_WINDOW_SECONDS:  # 如果记录时间超过重复窗口期
                del _recent_success_hashes[k]  # 删除过期的 hash 记录


def _make_record_hash(sensor_data: dict, control_data: dict):
    """对 sensor+control 生成稳定的 hash（JSON 序列化后 MD5）"""
    try:  # 优先尝试包含 sensor 和 control 的组合哈希
        combined = {  # 创建字典，包含传感器数据和控制指令
            "sensor": sensor_data,
            "control": control_data
        }
        s = json.dumps(combined, sort_keys=True, ensure_ascii=False)  # 将字典序列化为 JSON 字符串，排序键并禁用 ASCII 转义
        return hashlib.md5(s.encode("utf-8")).hexdigest()  # 对 JSON 字符串进行 MD5 哈希，返回 32 位十六进制字符串
    except Exception:  # 如果 sensor_data 序列化失败（如包含不可序列化对象）
        s = json.dumps(control_data, sort_keys=True, ensure_ascii=False)  # 仅对 control_data 序列化
        return hashlib.md5(s.encode("utf-8")).hexdigest()  # 返回 control_data 的 MD5 哈希值


def save_log_file():
    """保存日志并清理 7 天前记录，同时控制最大条数"""
    try:  # 尝试执行保存操作
        now = datetime.utcnow().replace(tzinfo=timezone.utc)  # 获取当前 UTC 时间并设置时区信息
        seven_days_ago = now - timedelta(days=7)  # 计算 7 天前的时间点
        filtered_log = []  # 创建空列表，用于存储过滤后的日志条目
        for entry in control_log:  # 遍历所有日志条目
            ts = entry.get("timestamp")  # 获取条目的时间戳字符串
            if not ts:  # 如果时间戳不存在
                continue  # 跳过该条目
            try:  # 尝试解析时间戳
                dt = datetime.fromisoformat(ts)  # 从 ISO 格式字符串解析为 datetime 对象
                if dt.tzinfo is None:  # 如果解析后的时区信息为 None
                    dt = dt.replace(tzinfo=timezone.utc)  # 设置为 UTC 时区
                dt_utc = dt.astimezone(timezone.utc)  # 转换为 UTC 时区（标准化）
            except Exception:  # 如果解析失败
                filtered_log.append(entry)  # 保留该条目（容错处理）
                continue  # 跳过后续过滤逻辑
            if dt_utc >= seven_days_ago:  # 如果时间戳在 7 天内
                filtered_log.append(entry)  # 保留该条目

        max_h = MAX_HISTORY if MAX_HISTORY else 1000  # 获取最大历史记录数，默认 1000
        to_write = filtered_log[-max_h:]  # 取最后 max_h 条记录（保留最新数据）
        dirpath = os.path.dirname(LOG_FILE)  # 获取日志文件所在目录路径
        if dirpath:  # 如果目录路径不为空
            os.makedirs(dirpath, exist_ok=True)  # 递归创建目录（如果不存在）
        tmp_path = LOG_FILE + ".tmp"  # 创建临时文件路径（原子写入）
        with open(tmp_path, "w", encoding="utf-8") as f:  # 以 UTF-8 编码写入模式打开临时文件
            json.dump(to_write, f, ensure_ascii=False, indent=2)  # 将日志列表序列化为 JSON，写入文件（禁用 ASCII 转义，格式化缩进）
            f.flush()  # 强制刷新缓冲区到操作系统
            try:
                os.fsync(f.fileno())  # 强制同步到磁盘（确保数据持久化）
            except Exception:
                pass  # 忽略同步失败（某些文件系统不支持）
        os.replace(tmp_path, LOG_FILE)  # 原子替换：将临时文件重命名为正式日志文件（避免写入中断导致文件损坏）
        if DEBUG:  # 如果 DEBUG 模式开启
            print(f"[LOG] 保存 {len(to_write)} 条日志 -> {LOG_FILE}")  # 打印保存的日志数量和路径
    except Exception as e:  # 捕获保存过程中的任何异常
        if DEBUG:  # 如果 DEBUG 模式开启
            print("[WARN] save_log_file failed:", e)  # 打印警告信息和异常详情


def record_combined(sensor_data: dict, control_data: dict, source: str = "workflow"):
    """拼接传感器与控制消息并保存"""
    if sensor_data is None or control_data is None:  # 如果传感器数据或控制数据为 None
        if DEBUG:  # 如果 DEBUG 模式开启
            print("[WARN] sensor_data 或 control_data 为 None，跳过保存")  # 打印警告信息
        return  # 直接返回，不执行后续记录逻辑

    beijing_tz = timezone(timedelta(hours=8))  # 定义北京时区（UTC+8）
    entry = {  # 创建字典，存储单条运行记录
        "timestamp": datetime.now(beijing_tz).isoformat(),  # 记录当前北京时间的时间戳（ISO 格式）
        "sensor_data": sensor_data,  # 存储传感器数据
        "control_command": control_data,  # 存储控制指令
        "source": source  # 记录来源（workflow 或 mqtt）
    }
    control_log.append(entry)  # 将新记录追加到全局日志列表
    save_log_file()  # 调用函数保存日志到文件
    if DEBUG:  # 如果 DEBUG 模式开启
        print("[LOG] 新增运行记录：", json.dumps(entry, ensure_ascii=False, indent=2))  # 打印新增的记录内容（格式化）

    if source == "workflow":  # 如果来源是 workflow（Dify 工作流触发）
        try:  # 尝试记录去重哈希
            h = _make_record_hash(sensor_data, control_data)  # 生成传感器+控制指令的组合哈希
            with _recent_lock:  # 获取锁，确保线程安全
                _recent_success_hashes[h] = time.time()  # 将哈希和当前时间存入去重缓存
            _cleanup_recent_hashes()  # 清理过期的哈希记录
            if DEBUG:  # 如果 DEBUG 模式开启
                print(f"[LOG] 已缓存 workflow 记录哈希，供后续去重：{h}")  # 打印缓存的哈希值
        except Exception:  # 如果哈希生成失败
            pass  # 忽略错误（不影响主流程）


# =============================
# 向量化函数：将文本转换为嵌入向量
# =============================
def embed_text(text: str):
    response = requests.post(  # 发送 POST 请求到 Ollama 嵌入服务
        OLLAMA_URL,  # 请求地址
        json={"model": MODEL, "prompt": text}  # JSON 请求体：指定模型和待嵌入文本
    )
    data = response.json()  # 解析响应的 JSON 数据
    return data["embedding"]  # 返回嵌入向量列表


# =============================
# 工具1：知识检索（MCP 工具）
# =============================
@mcp.tool("search_knowledge", description="检索知识库中与查询最相关的知识")  # 使用装饰器注册 MCP 工具，定义名称和描述
def search_knowledge(query: str, top_k: int = 3):  # 定义工具函数，接受查询字符串和返回结果数量（默认3条）
    query_emb = embed_text(query)  # 调用 embed_text 将查询文本转换为向量
    results = collection.query(query_embeddings=[query_emb], n_results=top_k)  # 在 ChromaDB 中执行向量相似度查询，返回 top_k 条结果
    matches = []  # 创建空列表，存储格式化后的匹配结果
    for i in range(len(results["ids"][0])):  # 遍历查询结果（按索引）
        matches.append({  # 将每条结果格式化为字典
            "id": results["ids"][0][i],  # 文档 ID
            "text": results["documents"][0][i],  # 文档内容
            "score": results["distances"][0][i],  # 相似度分数（距离越小越相似）
            "metadata": results["metadatas"][0][i]  # 文档元数据
        })
    return {"query": query, "results": matches}  # 返回包含查询和结果的字典


# =============================
# ✅ 改进后的 Dify 工作流触发逻辑（核心防护机制）
# =============================
def trigger_dify_workflow(sensor_data: dict):
    """触发 Dify 工作流进行数据处理（带 in-flight 去重）

    主要策略：
    - 在发送请求前基于 sensor_data 生成 hash，若该 hash 在 in-flight 缓存且未过期则跳过触发
    - 请求超时时（ReadTimeout）不立即重试，而是假定 workflow 已被服务端接收并保留 in-flight 标记一段时间（INFLIGHT_TTL）
    - 非超时的失败（连接错误、HTTP 非 200 等）会释放 in-flight 标记，允许后续重试
    """
    headers = {"Authorization": f"Bearer {DIFY_API_KEY}"}  # 构造请求头，携带 Dify API 密钥
    payload = {"inputs": sensor_data, "user": "abc-123"}  # 构造请求体，传递传感器数据和用户标识

    # 生成记录哈希作为去重 key（仅基于传感器数据）
    try:
        record_hash = _make_record_hash(sensor_data, {})  # 尝试生成传感器数据的哈希（control 为空）
    except Exception:  # 如果组合哈希失败
        record_hash = hashlib.md5(json.dumps(sensor_data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()  # 仅对传感器数据生成 MD5 哈希

    now = time.time()  # 获取当前时间戳
    ttl = globals().get("DIFY_INFLIGHT_TTL", INFLIGHT_TTL)  # 从全局配置获取 in-flight 保护时长，默认 300 秒

    # 清理过期 in-flight 项并检查是否已经存在
    with _inflight_lock:  # 获取 in-flight 锁，确保线程安全
        expired = [k for k, ts in _inflight_workflows.items() if now - ts > ttl]  # 找出所有过期的 in-flight 条目
        for k in expired:  # 遍历过期条目
            del _inflight_workflows[k]  # 从字典中删除过期条目

        if record_hash in _inflight_workflows:  # 如果当前传感器数据的哈希已存在于 in-flight 缓存
            if DEBUG:  # 如果 DEBUG 模式开启
                print(f"[DIFY] 跳过重复触发（in-flight，hash={record_hash}）")  # 打印跳过信息
            return  # 直接返回，不触发工作流（防重复触发）

        # 标记为 in-flight（表示已触发但未确认完成）
        _inflight_workflows[record_hash] = now  # 将哈希和当前时间存入 in-flight 字典

    timed_out = False  # 初始化超时标志为 False
    timeout_seconds = globals().get("DIFY_REQUEST_TIMEOUT", 30)  # 从全局配置获取请求超时时间，默认 30 秒

    # 当模型返回空/无效输出时，重试次数（仅对 200 响应且输出为空的情况有效）
    max_empty_retries = globals().get("DIFY_EMPTY_RETRIES", 3)  # 获取空输出重试次数配置，默认 3 次
    empty_attempt = 0  # 初始化空输出重试计数器

    while True:  # 进入循环，处理重试逻辑
        try:  # 尝试执行 HTTP 请求
            r = requests.post(DIFY_WORKFLOW_RUN_URL, json=payload, headers=headers, timeout=timeout_seconds)  # 发送 POST 请求到 Dify 工作流

            if r.status_code != 200:  # 如果 HTTP 状态码不是 200（成功）
                # 非 200：认为触发失败，移除 in-flight 以便后续重试
                with _inflight_lock:  # 获取锁
                    _inflight_workflows.pop(record_hash, None)  # 从 in-flight 字典中移除当前哈希（允许后续触发）
                if r.status_code == 429:  # 如果是 429（限流错误）
                    print("[DIFY] ⚠️ 服务限流（429），请稍后重试")  # 打印限流警告
                else:  # 其他错误状态码
                    print(f"[DIFY] ❌ Workflow 调用失败: HTTP {r.status_code}")  # 打印错误信息
                return  # 返回，结束函数

            # 成功返回（HTTP 200），解析可能的控制输出
            result = r.json()  # 解析响应 JSON
            control_output = None  # 初始化控制输出为 None
            if isinstance(result, dict):  # 如果解析结果是字典
                if "outputs" in result and isinstance(result["outputs"], dict):  # 如果存在 outputs 字段且是字典
                    control_output = result["outputs"].get("output") or result["outputs"].get("control")  # 尝试获取 output 或 control 字段
                else:  # 如果没有 outputs 字段
                    control_output = result.get("control") or result.get("output")  # 直接从顶层获取 control 或 output

            # 如果拿到有效输出 -> 记录并返回
            if control_output:  # 如果控制输出不为空
                record_combined(sensor_data, control_output, source="workflow")  # 调用函数记录传感器+控制指令到日志
                # 成功，清理 in-flight
                with _inflight_lock:  # 获取锁
                    _inflight_workflows.pop(record_hash, None)  # 从 in-flight 字典中移除（已完成）
                return  # 返回，结束函数

            # 输出为空：根据重试策略决定是否重试
            empty_attempt += 1  # 空输出尝试计数器加 1
            if empty_attempt <= max_empty_retries:  # 如果未达到最大空输出重试次数
                backoff = min(2 ** empty_attempt, 30)  # 计算指数退避延迟时间（最大 30 秒）
                if DEBUG:  # 如果 DEBUG 模式开启
                    print(f"[DIFY] ℹ️ Workflow 返回空输出，尝试第 {empty_attempt}/{max_empty_retries} 次重试（等待 {backoff}s）")  # 打印重试信息
                time.sleep(backoff)  # 休眠等待（指数退避）
                continue  # 继续循环，重新请求
            else:  # 达到最大重试次数
                # 达到最大重试仍无有效输出：不记录日志，释放 in-flight 并返回警告
                with _inflight_lock:  # 获取锁
                    _inflight_workflows.pop(record_hash, None)  # 移除 in-flight（允许后续重试）
                print("[DIFY] ⚠️ Workflow 多次返回空输出，已停止重试")  # 打印警告
                return  # 返回，结束函数

        except requests.exceptions.ReadTimeout:  # 捕获请求超时异常
            # 超时：不立即重试，保持 in-flight 标记，避免重复触发
            timed_out = True  # 设置超时标志为 True
            # 打短信息即可，不打印 traceback
            print(f"[DIFY] ⚠️ 请求超时（{timeout_seconds}s），已将该请求视为已提交；在 {ttl}s 内将不会再次触发。")  # 打印超时信息
            return  # 返回，结束函数（in-flight 标记保留，阻止重复触发）

        except requests.exceptions.ConnectionError:  # 捕获连接错误异常
            # 连接失败：释放 in-flight，允许后续重试
            with _inflight_lock:  # 获取锁
                _inflight_workflows.pop(record_hash, None)  # 移除 in-flight（允许后续触发）
            print("[DIFY] ❌ 无法连接到 Dify Workflow（请检查服务状态）")  # 打印连接失败信息
            return  # 返回，结束函数

        except Exception as e:  # 捕获其他所有异常
            # 其他异常：释放 in-flight，以免永久阻塞重试
            with _inflight_lock:  # 获取锁
                _inflight_workflows.pop(record_hash, None)  # 移除 in-flight（允许后续触发）
            error_msg = str(e)  # 将异常转为字符串
            if len(error_msg) > 200:  # 如果错误消息超过 200 字符
                error_msg = error_msg[:200] + "..."  # 截取前 200 字符并添加省略号
            print(f"[DIFY] ⚠️ Workflow 异常: {error_msg}")  # 打印异常信息
            return  # 返回，结束函数


# =============================
# MQTT 相关逻辑：处理 MQTT 连接和消息
# =============================
def on_connect(client, userdata, flags, rc):
    """MQTT 连接成功/失败回调函数"""
    if rc == 0:  # 如果返回码为 0（连接成功）
        if DEBUG:  # 如果 DEBUG 模式开启
            print("[MQTT] Connected")  # 打印连接成功信息
        client.subscribe(TOPIC_SENSOR)  # 订阅传感器数据主题（从 config.py 获取）
        client.subscribe(TOPIC_CONTROL)  # 订阅控制指令主题（从 config.py 获取）
    else:  # 如果返回码不为 0（连接失败）
        print("[MQTT] Connect failed, rc=", rc)  # 打印连接失败信息和错误码


def on_message(client, userdata, msg):
    """MQTT 消息到达回调函数"""
    global latest_sensor_data  # 声明使用全局变量 latest_sensor_data
    try:  # 尝试解析消息负载
        payload = json.loads(msg.payload.decode())  # 将 MQTT 消息字节码解码为字符串，再反序列化为 Python 对象
    except Exception:  # 如果 JSON 解析失败（非 JSON 格式）
        payload = {"raw": msg.payload.decode()}  # 将原始字符串包装在字典中

    if msg.topic == TOPIC_SENSOR:  # 如果消息主题是传感器数据主题
        latest_sensor_data = payload  # 将 payload 存入全局变量，供后续控制指令关联
        if DEBUG:  # 如果 DEBUG 模式开启
            print("[MQTT] Received sensor data:", payload)  # 打印接收到的传感器数据
        threading.Thread(target=trigger_dify_workflow, args=(payload,), daemon=True).start()  # 创建守护线程异步触发 Dify 工作流，避免阻塞 MQTT 回调

    elif msg.topic == TOPIC_CONTROL:  # 如果消息主题是控制指令主题
        if DEBUG:  # 如果 DEBUG 模式开启
            print("[MQTT] Received control command:", payload)  # 打印接收到的控制指令

        # 定义内部函数：判断控制消息是否为空/无效
        def _is_empty_control(p):
            """检查控制消息是否有效"""
            if not p:  # 如果 payload 为 None 或空
                return True  # 视为无效
            if isinstance(p, dict):  # 如果是字典
                # 当所有字段为空或仅有 raw 且为空字符串时，视为无效
                if all((not v and v != 0) for v in p.values()):  # 如果所有值都为空（0 除外）
                    return True  # 视为无效
                if list(p.keys()) == ["raw"] and isinstance(p.get("raw"), str) and p.get("raw").strip() == "":  # 如果只有 raw 字段且为空字符串
                    return True  # 视为无效
            return False  # 否则视为有效

        if _is_empty_control(payload):  # 如果控制消息无效
            if DEBUG:  # 如果 DEBUG 模式开启
                print("[MQTT] 收到空控制消息，跳过记录并尝试重新触发 workflow")  # 打印提示
            if latest_sensor_data:  # 如果存在缓存的传感器数据
                threading.Thread(target=trigger_dify_workflow, args=(latest_sensor_data,), daemon=True).start()  # 用缓存的传感器数据重新触发工作流
            return  # 返回，不记录空控制指令

        h = _make_record_hash(latest_sensor_data or {}, payload)  # 生成传感器+控制指令的组合哈希（如果传感器数据无则传空字典）
        skip = False  # 初始化跳过标志为 False
        with _recent_lock:  # 获取去重锁
            ts = _recent_success_hashes.get(h)  # 查询该哈希是否已记录
            if ts and (time.time() - ts) <= DUPLICATE_WINDOW_SECONDS:  # 如果哈希存在且未超过重复窗口期
                skip = True  # 设置跳过标志为 True
        if skip:  # 如果需要跳过
            if DEBUG:  # 如果 DEBUG 模式开启
                print(f"[MQTT] 跳过重复控制（hash: {h}）")  # 打印跳过信息
        else:  # 如果不跳过
            if latest_sensor_data:  # 如果存在传感器数据
                record_combined(latest_sensor_data, payload, source="mqtt")  # 记录传感器+控制指令（来源 mqtt）
            else:  # 如果没有传感器数据
                record_combined({}, payload, source="mqtt")  # 仅记录控制指令（空传感器字典）


def mqtt_start():
    """启动 MQTT 客户端"""
    client = mqtt.Client()  # 创建新的 MQTT 客户端实例
    client.on_connect = on_connect  # 设置连接回调函数
    client.on_message = on_message  # 设置消息到达回调函数
    try:  # 尝试连接
        client.connect(MQTT_BROKER, MQTT_PORT, 60)  # 连接到 MQTT 服务器（地址和端口从 config.py 获取），心跳 60 秒
        client.loop_start()  # 启动网络循环线程，处理消息收发
        if DEBUG:  # 如果 DEBUG 模式开启
            print(f"[MQTT] Started client and subscribed to {TOPIC_SENSOR} & {TOPIC_CONTROL}")  # 打印启动和订阅信息
    except Exception as e:  # 如果连接失败
        print("[MQTT] Failed to start MQTT client:", e)  # 打印错误信息


def ensure_mqtt_started():
    """确保 MQTT 只启动一次（单例模式）"""
    if not getattr(ensure_mqtt_started, "started", False):  # 检查 started 属性是否存在且为 False
        threading.Thread(target=mqtt_start, daemon=True).start()  # 创建守护线程启动 MQTT，不阻塞主程序
        ensure_mqtt_started.started = True  # 设置 started 属性为 True，标记已启动


ensure_mqtt_started()  # 调用函数，确保 MQTT 客户端启动

# =============================
# 主入口：程序入口点
# =============================
if __name__ == "__main__":  # 如果直接运行此脚本（而非被导入）
    mcp.run()  # 启动 FastMCP 服务器，开始监听 MCP 客户端连接（阻塞运行）