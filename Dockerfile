FROM python:3.12-slim

# 设置工作目录
WORKDIR /app

# 安装Python依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY ./app /app

# 默认命令
CMD ["python", "/app/main.py"]