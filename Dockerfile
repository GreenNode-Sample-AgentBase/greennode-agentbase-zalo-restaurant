FROM python:3.12-slim
WORKDIR /app
COPY src/backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY src/backend ./backend
COPY src/frontend ./frontend
WORKDIR /app/backend
EXPOSE 8080
CMD ["python", "main.py"]
