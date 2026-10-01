FROM python:3.12-slim
WORKDIR /app
# Build context là src/mcp-server/ (xem README: docker build ... src/mcp-server/)
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py ./main.py
# SQLite data dir (MCP_DB_PATH mặc định: data/restaurant.db)
RUN mkdir -p /app/data
EXPOSE 8080
CMD ["python", "main.py"]
