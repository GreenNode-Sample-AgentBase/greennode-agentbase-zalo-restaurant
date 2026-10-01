FROM python:3.12-slim
WORKDIR /app
COPY src/mcp-server/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY src/mcp-server/main.py ./main.py
EXPOSE 8080
CMD ["python", "main.py"]
