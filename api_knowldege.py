from fastapi import FastAPI, UploadFile, Form  # 从 fastapi 模块导入 FastAPI 应用类、文件上传类和表单参数类
from fastapi.responses import JSONResponse  # 从 fastapi.responses 模块导入 JSONResponse 类，用于返回 JSON 格式的 HTTP 响应
import chromadb  # 导入 chromadb 向量数据库库，用于知识库存储和检索
import requests  # 导入 requests 库，用于发送 HTTP 请求（调用 Ollama 嵌入服务）
import uuid  # 导入 uuid 库，用于生成全局唯一的文档 ID
import re  # 导入 re 正则表达式库，用于文本模式匹配和提取
import os  # 导入 os 操作系统接口库，用于环境变量操作

# ==========================================
# 全局配置：定义服务运行常量参数
# ==========================================
os.environ["ANONYMIZED_TELEMETRY"] = "false"  # 设置环境变量，禁用 ChromaDB 的匿名遥测数据收集
OLLAMA_URL = "http://localhost:11434/api/embeddings"  # 定义 Ollama 嵌入服务的 API 地址
MODEL = "bge-m3"  # 定义使用的嵌入模型名称
DB_PATH = "./knowledge_db"  # 定义 ChromaDB 数据库文件存储路径
DEFAULT_SIMILARITY_THRESHOLD = 0.8  # 定义默认相似度阈值（0.8），用于判断知识是否重复

app = FastAPI(title="Dify MCP Knowledge Service")  # 创建 FastAPI 应用实例，设置服务标题为"Dify MCP Knowledge Service"

# ==========================================
# 初始化 Chroma：连接或创建向量数据库
# ==========================================
client = chromadb.PersistentClient(path=DB_PATH)  # 创建 Chroma 持久化客户端，连接到指定路径的向量数据库
collection = client.get_or_create_collection(name="knowledge_base")  # 获取或创建名为"knowledge_base"的知识库集合（collection）

# ==========================================
# 向量生成：将文本转换为嵌入向量的函数
# ==========================================
def embed_text(text: str):
    """接收文本字符串，返回其嵌入向量"""
    response = requests.post(OLLAMA_URL, json={"model": MODEL, "prompt": text})  # 向 Ollama 服务发送 POST 请求，携带模型名称和待转换文本
    data = response.json()  # 解析响应的 JSON 数据
    return data["embedding"]  # 从响应中提取并返回嵌入向量列表

# ==========================================
# 自动识别作物和阶段：从文本中提取作物名称和生长阶段
# ==========================================
def detect_crop_and_stage(line: str):
    """接收文本行，返回识别出的作物名称和生长阶段"""
    pattern = r"^([\u4e00-\u9fa5A-Za-z0-9（）()·\-\s]+?)\s+([\u4e00-\u9fa5A-Za-z0-9\-（）()]+期?)"  # 定义正则模式：匹配作物名称（中文/英文/数字/括号等）+ 空白字符 + 生长阶段（以"期"结尾）
    match = re.match(pattern, line.strip())  # 去除首尾空白后，从行首开始匹配模式
    if match:  # 如果匹配成功
        crop = match.group(1)  # 提取第一组：作物名称
        stage = match.group(2)  # 提取第二组：生长阶段
        return crop, stage  # 返回作物和阶段的元组
    else:  # 如果匹配失败
        return None, None  # 返回 None 元组

# ==========================================
# 元数据提取：从知识文本中提取结构化元数据
# ==========================================
def extract_metadata(text: str):
    """接收知识文本，返回提取的元数据字典"""
    lines = text.strip().split("\n")  # 去除首尾空白，按换行符分割成列表
    first_line = lines[0] if lines else ""  # 获取第一行（标题行），如果列表为空则为空字符串
    crop_found, stage_found = detect_crop_and_stage(first_line)  # 调用函数识别作物和阶段

    metadata = {"作物": crop_found, "生长阶段": stage_found}  # 创建元数据字典，初始化作物和阶段

    def get_range(pattern):  # 定义内部函数：从文本中提取数值范围
        match = re.search(pattern, text)  # 搜索匹配模式
        if match:  # 如果找到匹配
            v1 = float(match.group(1))  # 提取第一组数值并转为浮点数
            v2 = float(match.group(2)) if match.group(2) else v1  # 提取第二组数值（如果存在），否则使用第一组值
            return v1, v2  # 返回最小值和最大值
        return -1.0, -1.0  # 如果未找到匹配，返回 -1.0, -1.0

    def get_num(regex):  # 定义内部函数：从文本中提取单个数值
        m = re.search(regex, text)  # 搜索匹配模式
        return float(m.group(1)) if m else -1.0  # 如果找到则转为浮点数返回，否则返回 -1.0

    min_temp, max_temp = get_range(r"温度[：: ]*([0-9]+)[～\-–]?([0-9]+)?")  # 提取温度范围
    min_hum, max_hum = get_range(r"湿度[：: ]*[^0-9]*([0-9]+)[～\-–]?([0-9]+)?")  # 提取湿度范围
    min_soil, max_soil = get_range(r"土壤含水量[：: ]*([0-9]+)[～\-–]?([0-9]+)?")  # 提取土壤含水量范围

    metadata.update({  # 更新元数据字典，添加所有提取的数值
        "最低温度": min_temp, "最高温度": max_temp,
        "最低湿度": min_hum, "最高湿度": max_hum,
        "最低土壤含水量": min_soil, "最高土壤含水量": max_soil,
        "氮肥量": get_num(r"氮\s*([0-9]+)kg"),  # 提取氮肥量（kg）
        "磷肥量": get_num(r"磷\s*([0-9]+)kg"),  # 提取磷肥量（kg）
        "钾肥量": get_num(r"钾\s*([0-9]+)kg"),  # 提取钾肥量（kg）
        "光照时长": get_num(r"光照[：: ]*≥?([0-9]+)h"),  # 提取光照时长（小时）
    })
    return metadata  # 返回完整的元数据字典

# ==========================================
# 查找重复知识：查询与给定向量最相似的文档
# ==========================================
def find_similar_docs(embedding):
    """接收嵌入向量，返回最相似的 3 个文档"""
    results = collection.query(query_embeddings=[embedding], n_results=3)  # 在 ChromaDB 中查询最相似的 3 条记录
    if not results["ids"] or not results["ids"][0]:  # 如果没有返回结果
        return []  # 返回空列表
    duplicates = []  # 创建空列表，存储相似文档信息
    for i, dist in enumerate(results["distances"][0]):  # 遍历距离列表（距离越小越相似）
        similarity = 1 - float(dist)  # 将距离转换为相似度（0-1，越大越相似）
        duplicates.append({  # 将相似文档信息添加到列表
            "id": results["ids"][0][i],  # 文档 ID
            "similarity": round(similarity, 3),  # 相似度（保留 3 位小数）
            "text": results["documents"][0][i],  # 文档内容
        })
    return duplicates  # 返回相似文档列表

# ==========================================
# 上传知识（自动分块 + 相似度检测）：HTTP POST 接口
# ==========================================
@app.post("/upload")  # 注册 POST 路由 /upload
async def upload_knowledge(
    text: str = Form(None),  # 接收表单字段 text（可选，字符串类型）
    file: UploadFile = None,  # 接收表单文件 file（可选，UploadFile 类型）
    threshold: float = Form(DEFAULT_SIMILARITY_THRESHOLD)  # 接收表单字段 threshold（可选，浮点数，默认 0.8）
):
    """处理知识上传请求，支持文本或文件，自动检测重复"""
    if file:  # 如果提供了文件
        content = (await file.read()).decode("utf-8")  # 异步读取文件字节码，解码为 UTF-8 字符串
    elif text:  # 如果提供了文本
        content = text  # 直接使用文本内容
    else:  # 如果两者都未提供
        return JSONResponse({"error": "必须提供 text 或 file"}, status_code=400)  # 返回 400 错误响应

    blocks = [b.strip() for b in re.split(r"\n\s*\n", content) if b.strip()]  # 按双换行符分割内容成段落块，去除首尾空白，过滤空块
    added_count = 0  # 初始化成功添加计数器为 0

    print(f"\n🧠 当前相似度阈值：{threshold}\n")  # 打印当前使用的相似度阈值

    for block in blocks:  # 遍历每个知识段落块
        lines = block.split("\n")  # 将块按换行符分割成行列表
        crop, stage = detect_crop_and_stage(lines[0])  # 从第一行识别作物和阶段
        if not crop or not stage:  # 如果作物或阶段未识别成功
            print(f"⚠️ 无法识别作物或阶段：{lines[0]}")  # 打印警告信息
            continue  # 跳过该块，继续处理下一个

        emb = embed_text(block)  # 将当前段落块转换为嵌入向量
        duplicates = find_similar_docs(emb)  # 查找相似文档
        high_sim = [d for d in duplicates if d["similarity"] >= threshold]  # 筛选相似度大于等于阈值的文档

        # === ✅ 新增逻辑：自动跳过完全相同的内容 ===
        auto_skip = False  # 初始化自动跳过标志为 False
        for d in high_sim:  # 遍历高相似度文档
            if d["similarity"] == 1.0:  # 如果相似度为 1.0（完全相同）
                print(f"\n⏭️ 检测到完全相同的知识（{crop} {stage}），已自动跳过：\n{d['text']}\n")  # 打印自动跳过信息
                auto_skip = True  # 设置自动跳过标志为 True
                break  # 跳出循环
        if auto_skip:  # 如果需要自动跳过
            continue  # 跳过当前块，继续处理下一个
        # ==================== 自动跳过逻辑结束

        if high_sim:  # 如果存在高相似度文档（但非完全相同）
            print(f"\n⚠️ 检测到相似知识片段（{crop} {stage}）:")  # 打印相似检测警告
            for d in high_sim:  # 遍历高相似度文档
                print(f"相似度: {d['similarity']} | 已存在内容:\n{d['text']}\n")  # 打印相似度和已存在内容
            print(f"🆕 新上传内容:\n{block}\n")  # 打印新内容
            choice = input("是否仍然上传此内容？(y/n): ").strip().lower()  # 交互式询问用户是否继续上传
            if choice != "y":  # 如果用户输入不是 y
                print("⏹️ 已跳过此片段。\n")  # 打印跳过信息
                continue  # 跳过当前块，继续处理下一个

        doc_id = str(uuid.uuid4())  # 生成全局唯一的 UUID 作为文档 ID
        metadata = extract_metadata(block)  # 从段落块提取元数据
        metadata["唯一编号"] = doc_id  # 将 UUID 添加到元数据
        collection.add(ids=[doc_id], embeddings=[emb], documents=[block], metadatas=[metadata])  # 将文档添加到 ChromaDB（包含 ID、向量、原文、元数据）
        print(f"✅ 成功添加知识 [{doc_id}]（{crop} {stage}）")  # 打印成功添加信息
        added_count += 1  # 成功计数器加 1

    return {"status": "success", "message": f"成功添加 {added_count} 条知识"}  # 返回 JSON 响应，包含成功状态和添加数量

# ==========================================
# 搜索接口：HTTP POST 接口
# ==========================================
@app.post("/search")  # 注册 POST 路由 /search
async def search_knowledge(query: str = Form(...), top_k: int = Form(3)):  # 接收必填的查询字符串和可选的返回数量（默认3）
    """知识搜索接口，接收查询，返回最相似的 top_k 条知识"""
    query_emb = embed_text(query)  # 将查询文本转换为嵌入向量
    results = collection.query(query_embeddings=[query_emb], n_results=top_k)  # 查询最相似的 top_k 条记录
    data = []  # 创建空列表，存储格式化结果
    for i in range(len(results["ids"][0])):  # 遍历查询结果
        data.append({  # 格式化每条结果
            "id": results["ids"][0][i],  # 文档 ID
            "text": results["documents"][0][i],  # 文档内容
            "metadata": results["metadatas"][0][i],  # 元数据
            "score": float(results["distances"][0][i])  # 相似度分数（距离）
        })
    return {"count": len(data), "data": data}  # 返回包含数量和数据的 JSON 响应

# ==========================================
# 查看所有知识：HTTP GET 接口
# ==========================================
@app.get("/list")  # 注册 GET 路由 /list
async def list_knowledge():
    """返回知识库中所有知识条目"""
    results = collection.get(include=["documents", "metadatas"])  # 获取集合中所有文档和元数据
    data = [  # 列表推导式，格式化所有文档
        {"id": results["ids"][i], "text": results["documents"][i], "metadata": results["metadatas"][i]}  # 每条包含 ID、内容、元数据
        for i in range(len(results["documents"]))  # 遍历所有文档
    ]
    return {"count": len(data), "data": data}  # 返回包含数量和完整数据的 JSON 响应

# ==========================================
# 按 ID 查询单条知识：HTTP GET 接口
# ==========================================
@app.get("/get/{doc_id}")  # 注册 GET 路由 /get/{doc_id}，doc_id 是路径参数
async def get_doc(doc_id: str):  # 接收文档 ID 字符串参数
    """
    根据唯一编号查询单条知识
    """
    try:  # 尝试查询文档
        raw = collection.get(ids=[doc_id], include=["documents", "metadatas"])  # 根据 ID 查询文档
        if not raw["ids"]:  # 如果返回的 ID 列表为空（文档不存在）
            return JSONResponse({"status": "not_found", "error": f"ID {doc_id} 不存在"}, status_code=404)  # 返回 404 错误响应

        return {  # 返回成功响应和文档内容
            "status": "success",
            "id": raw["ids"][0],  # 文档 ID
            "text": raw["documents"][0],  # 文档内容
            "metadata": raw["metadatas"][0]  # 元数据
        }
    except Exception as e:  # 如果查询过程中出现异常
        return JSONResponse({"status": "failed", "error": str(e)}, status_code=500)  # 返回 500 错误响应

# ==========================================
# 删除：HTTP DELETE 接口
# ==========================================
@app.delete("/delete")  # 注册 DELETE 路由 /delete
async def delete_doc(doc_id: str):  # 接收查询参数 doc_id
    """根据 ID 删除单条知识"""
    try:  # 尝试删除
        collection.delete(ids=[doc_id])  # 从 ChromaDB 中删除指定 ID 的文档
        return {"status": "success", "deleted_id": doc_id}  # 返回成功状态和删除的 ID
    except Exception as e:  # 如果删除失败
        return {"status": "failed", "error": str(e)}  # 返回失败状态和错误信息

# ==========================================
# 清空知识库：HTTP DELETE 接口
# ==========================================
@app.delete("/clear")  # 注册 DELETE 路由 /clear
async def clear_knowledge():
    """清空知识库中所有知识"""
    try:  # 尝试清空
        all_ids = collection.get()["ids"]  # 获取所有文档 ID
        if all_ids:  # 如果 ID 列表不为空（知识库有内容）
            collection.delete(ids=all_ids)  # 删除所有 ID 对应的文档
            return {"status": "success", "message": f"已删除 {len(all_ids)} 条知识"}  # 返回成功和删除数量
        else:  # 如果 ID 列表为空
            return {"status": "success", "message": "知识库为空"}  # 返回成功和空库提示
    except Exception as e:  # 如果清空失败
        return {"status": "failed", "error": str(e)}  # 返回失败状态和错误信息

# ==========================================
# 健康检查：HTTP GET 接口
# ==========================================
@app.get("/health")  # 注册 GET 路由 /health
async def health_check():
    """服务健康检查接口，用于监控"""
    return {"status": "ok"}  # 返回健康状态

# ==========================================
# 启动：主入口
# ==========================================
if __name__ == "__main__":  # 如果直接运行此脚本（而非被导入）
    import uvicorn  # 导入 uvicorn ASGI 服务器
    print("🚀 MCP 知识库服务已启动 (http://localhost:8450)")  # 打印服务启动提示
    uvicorn.run(app, host="0.0.0.0", port=8450)  # 启动 uvicorn 服务器，监听所有网络接口的 8450 端口，运行 FastAPI 应用