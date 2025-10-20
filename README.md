        MCP Adapter (FastAPI) - Dify MCP HTTP 示例


        Quick start:

1. Configure config.py if needed (MQTT broker, ports).
2. Install dependencies: pip install -r requirements.txt
3. Run: python mcp_server.py
4. In Dify console, register a new MCP HTTP service with Base URL: http://host.docker.internal:9000
5. In Dify workflow, add an HTTP tool node that POSTs to /run (or use MCP integration if available).

Endpoints:
- POST /run: Dify calls this with {"inputs": {...}} and receives {"control": {...}, "reason": "..."}
- GET /history: recent sensor records
- POST /control: external control command to publish to MQTT
- GET /health: service health


# MCP Knowledge Service

MCP Knowledge Service 是一个基于 Python 3.10 的本地知识库管理服务，它结合 Ollama bge-m3 模型对文本进行向量化，并存储到本地 Chroma 向量数据库中。该服务专为 Dify 大模型设计，使其能够将生成的知识直接上传到本地知识库，并在后续决策和回答生成时进行查询。

服务提供 HTTP 接口，包括知识上传、知识检索、查看知识库内容、删除知识条目和健康检查功能。上传知识时，可以通过文本或文件方式发送内容，服务会调用 Ollama bge-m3 模型生成向量并存储到 Chroma 知识库。查询知识时，输入查询文本，服务会先向量化查询内容，然后在 Chroma 中检索最相似的知识段落返回给调用方。可以通过查看接口获取当前知识库中所有条目及其唯一 ID，也可以通过删除接口删除指定知识条目。健康检查接口用于确认服务是否正常。

安装服务的步骤首先是确保本地安装 Python 3.10，然后将 `mcp_knowledge_service.py` 文件下载或克隆到本地项目目录中。接着使用 pip 安装依赖库，包括 fastapi、chromadb、requests 和 uvicorn。启动服务前，需要确保本地 Ollama 已经启动，并且包含 bge-m3 模型。启动服务可以使用命令 `python mcp_knowledge_service.py`，服务默认监听在 0.0.0.0 的 8439 端口。

上传知识可以通过 POST 请求调用 `/upload` 接口，传入文本参数或文件参数。上传文本示例命令为 `curl -X POST http://localhost:8439/upload -F "text=温室湿度过高时应增加通风。"`，上传文件示例命令为 `curl -X POST http://localhost:8439/upload -F "file=@knowledge.txt"`。接口返回上传状态以及添加的段落数量。查询知识可以通过 POST 请求调用 `/search` 接口，传入查询文本和可选的 top_k 参数表示返回最相关的前几个知识条目，例如 `curl -X POST http://localhost:8439/search -F "query=湿度太高怎么办？" -F "top_k=3"`，返回最相关的知识内容及其相似度分数。查看知识库中的所有内容可以通过 GET 请求调用 `/list` 接口，例如 `curl http://localhost:8439/list`，返回包括每条知识的唯一 ID 和文本内容。删除指定知识可以通过 DELETE 请求调用 `/delete` 接口，例如 `curl -X DELETE "http://localhost:8439/delete?doc_id=<知识ID>"`，返回删除状态和对应的 ID。健康检查接口可以通过 GET 请求调用 `/health` 来确认服务是否正常，例如 `curl http://localhost:8439/health` 返回服务状态。

MCP 服务可以直接嵌入到其他 Python 项目中。可以在项目中导入 `embed_text` 函数和 Chroma collection 对象，通过 `embed_text(text)` 获取向量，然后使用 `collection.add(ids=[doc_id], documents=[text], embeddings=[embedding])` 存入知识库，并调用 `client.persist()` 保存到本地。也可以通过 HTTP 请求方式调用 MCP 接口，将服务作为远程知识库管理接口使用，方便与 Dify 大模型结合，实现知识生成和查询的完整闭环。通过在 Dify 工作流中添加 HTTP 节点，调用 `/upload` 接口上传大模型生成的知识，后续在处理传感器数据或其他输入时，通过 `/search` 接口获取相关知识辅助决策和回答生成。

使用该服务时，需要确保 Ollama 模型本地已启动，知识库路径默认在 `./knowledge_db`，可根据项目需求修改配置。对于较长文本，建议分段上传，以保证向量化效率和检索准确性。服务支持扩展功能，如 PDF 或 Word 文件解析、文本分段摘要和批量上传删除操作，也可以进行 Docker 容器化部署，以便与 Dify 集成和自动化运行。


API文件用于对知识库进行操作 以下是相关操作
# 上传文本
curl -X POST http://localhost:8450/upload -F "text=这是测试知识"

# 上传文件
curl -X POST http://localhost:8450/upload -F "file=@knowledge.txt"

# 查看知识库内容
curl http://localhost:8450/list

# 搜索知识
curl -X POST http://localhost:8450/search -F "query=测试"

# 删除指定知识
curl -X DELETE "http://localhost:8450/delete?doc_id=xxxx-xxxx"

# 清空知识库
curl -X DELETE http://localhost:8450/clear

# 导出知识库
curl -O http://localhost:8450/export



此条指令在MCP中使用
fastmcp run mcp_knowledge_service.py:mcp --transport http --port 8439
fastmcp run mcp_knowledge_service.py:mcp --transport sse --port 8439