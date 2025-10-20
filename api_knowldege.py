from fastapi import FastAPI, UploadFile, Form
from fastapi.responses import JSONResponse
import chromadb
import requests
import uuid
import re
import os

# ==========================================
# 全局配置
# ==========================================
os.environ["ANONYMIZED_TELEMETRY"] = "false"
OLLAMA_URL = "http://localhost:11434/api/embeddings"
MODEL = "bge-m3"
DB_PATH = "./knowledge_db"
DEFAULT_SIMILARITY_THRESHOLD = 0.8  # 默认相似度阈值

app = FastAPI(title="Dify MCP Knowledge Service")

# ==========================================
# 初始化 Chroma
# ==========================================
client = chromadb.PersistentClient(path=DB_PATH)
collection = client.get_or_create_collection(name="knowledge_base")

# ==========================================
# 向量生成
# ==========================================
def embed_text(text: str):
    response = requests.post(OLLAMA_URL, json={"model": MODEL, "prompt": text})
    data = response.json()
    return data["embedding"]

# ==========================================
# 自动识别作物和阶段
# ==========================================
def detect_crop_and_stage(line: str):
    pattern = r"^([\u4e00-\u9fa5A-Za-z0-9（）()·\-\s]+?)\s+([\u4e00-\u9fa5A-Za-z0-9\-（）()]+期?)"
    match = re.match(pattern, line.strip())
    if match:
        crop = match.group(1)
        stage = match.group(2)
        return crop, stage
    else:
        return None, None

# ==========================================
# 元数据提取
# ==========================================
def extract_metadata(text: str):
    lines = text.strip().split("\n")
    first_line = lines[0] if lines else ""
    crop_found, stage_found = detect_crop_and_stage(first_line)

    metadata = {"作物": crop_found, "生长阶段": stage_found}

    def get_range(pattern):
        match = re.search(pattern, text)
        if match:
            v1 = float(match.group(1))
            v2 = float(match.group(2)) if match.group(2) else v1
            return v1, v2
        return -1.0, -1.0

    def get_num(regex):
        m = re.search(regex, text)
        return float(m.group(1)) if m else -1.0

    min_temp, max_temp = get_range(r"温度[：: ]*([0-9]+)[～\-–]?([0-9]+)?")
    min_hum, max_hum = get_range(r"湿度[：: ]*[^0-9]*([0-9]+)[～\-–]?([0-9]+)?")
    min_soil, max_soil = get_range(r"土壤含水量[：: ]*([0-9]+)[～\-–]?([0-9]+)?")

    metadata.update({
        "最低温度": min_temp, "最高温度": max_temp,
        "最低湿度": min_hum, "最高湿度": max_hum,
        "最低土壤含水量": min_soil, "最高土壤含水量": max_soil,
        "氮肥量": get_num(r"氮\s*([0-9]+)kg"),
        "磷肥量": get_num(r"磷\s*([0-9]+)kg"),
        "钾肥量": get_num(r"钾\s*([0-9]+)kg"),
        "光照时长": get_num(r"光照[：: ]*≥?([0-9]+)h"),
    })
    return metadata

# ==========================================
# 查找重复知识
# ==========================================
def find_similar_docs(embedding):
    results = collection.query(query_embeddings=[embedding], n_results=3)
    if not results["ids"] or not results["ids"][0]:
        return []
    duplicates = []
    for i, dist in enumerate(results["distances"][0]):
        similarity = 1 - float(dist)
        duplicates.append({
            "id": results["ids"][0][i],
            "similarity": round(similarity, 3),
            "text": results["documents"][0][i],
        })
    return duplicates

# ==========================================
# 上传知识（自动分块 + 相似度检测）
# ==========================================
@app.post("/upload")
async def upload_knowledge(
    text: str = Form(None),
    file: UploadFile = None,
    threshold: float = Form(DEFAULT_SIMILARITY_THRESHOLD)
):
    if file:
        content = (await file.read()).decode("utf-8")
    elif text:
        content = text
    else:
        return JSONResponse({"error": "必须提供 text 或 file"}, status_code=400)

    blocks = [b.strip() for b in re.split(r"\n\s*\n", content) if b.strip()]
    added_count = 0

    print(f"\n🧠 当前相似度阈值：{threshold}\n")

    for block in blocks:
        lines = block.split("\n")
        crop, stage = detect_crop_and_stage(lines[0])
        if not crop or not stage:
            print(f"⚠️ 无法识别作物或阶段：{lines[0]}")
            continue

        emb = embed_text(block)
        duplicates = find_similar_docs(emb)
        high_sim = [d for d in duplicates if d["similarity"] >= threshold]

        # === ✅ 新增逻辑 ===
        auto_skip = False
        for d in high_sim:
            if d["similarity"] == 1.0:  # 完全相同，自动跳过
                print(f"\n⏭️ 检测到完全相同的知识（{crop} {stage}），已自动跳过：\n{d['text']}\n")
                auto_skip = True
                break
        if auto_skip:
            continue
        # ====================

        if high_sim:
            print(f"\n⚠️ 检测到相似知识片段（{crop} {stage}）:")
            for d in high_sim:
                print(f"相似度: {d['similarity']} | 已存在内容:\n{d['text']}\n")
            print(f"🆕 新上传内容:\n{block}\n")
            choice = input("是否仍然上传此内容？(y/n): ").strip().lower()
            if choice != "y":
                print("⏹️ 已跳过此片段。\n")
                continue

        doc_id = str(uuid.uuid4())
        metadata = extract_metadata(block)
        metadata["唯一编号"] = doc_id
        collection.add(ids=[doc_id], embeddings=[emb], documents=[block], metadatas=[metadata])
        print(f"✅ 成功添加知识 [{doc_id}]（{crop} {stage}）")
        added_count += 1

    return {"status": "success", "message": f"成功添加 {added_count} 条知识"}

# ==========================================
# 搜索接口
# ==========================================
@app.post("/search")
async def search_knowledge(query: str = Form(...), top_k: int = Form(3)):
    query_emb = embed_text(query)
    results = collection.query(query_embeddings=[query_emb], n_results=top_k)
    data = []
    for i in range(len(results["ids"][0])):
        data.append({
            "id": results["ids"][0][i],
            "text": results["documents"][0][i],
            "metadata": results["metadatas"][0][i],
            "score": float(results["distances"][0][i])
        })
    return {"count": len(data), "data": data}

# ==========================================
# 查看所有知识
# ==========================================
@app.get("/list")
async def list_knowledge():
    results = collection.get(include=["documents", "metadatas"])
    data = [
        {"id": results["ids"][i], "text": results["documents"][i], "metadata": results["metadatas"][i]}
        for i in range(len(results["documents"]))
    ]
    return {"count": len(data), "data": data}

# ==========================================
# 按 ID 查询单条知识
# ==========================================
@app.get("/get/{doc_id}")
async def get_doc(doc_id: str):
    """
    根据唯一编号查询单条知识
    """
    try:
        raw = collection.get(ids=[doc_id], include=["documents", "metadatas"])
        if not raw["ids"]:
            return JSONResponse({"status": "not_found", "error": f"ID {doc_id} 不存在"}, status_code=404)

        return {
            "status": "success",
            "id": raw["ids"][0],
            "text": raw["documents"][0],
            "metadata": raw["metadatas"][0]
        }
    except Exception as e:
        return JSONResponse({"status": "failed", "error": str(e)}, status_code=500)

# ==========================================
# 删除
# ==========================================
@app.delete("/delete")
async def delete_doc(doc_id: str):
    try:
        collection.delete(ids=[doc_id])
        return {"status": "success", "deleted_id": doc_id}
    except Exception as e:
        return {"status": "failed", "error": str(e)}

# ==========================================
# 清空知识库
# ==========================================
@app.delete("/clear")
async def clear_knowledge():
    try:
        all_ids = collection.get()["ids"]
        if all_ids:
            collection.delete(ids=all_ids)
            return {"status": "success", "message": f"已删除 {len(all_ids)} 条知识"}
        else:
            return {"status": "success", "message": "知识库为空"}
    except Exception as e:
        return {"status": "failed", "error": str(e)}

# ==========================================
# 健康检查
# ==========================================
@app.get("/health")
async def health_check():
    return {"status": "ok"}

# ==========================================
# 启动
# ==========================================
if __name__ == "__main__":
    import uvicorn
    print("🚀 MCP 知识库服务已启动 (http://localhost:8450)")
    uvicorn.run(app, host="0.0.0.0", port=8450)