FROM python:3.11-slim
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir -r requirements.txt
EXPOSE 8439
CMD ["uvicorn", "mcp_server:app", "--host", "0.0.0.0", "--port", "8439"]
