FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

WORKDIR /app

# 设置时区与环境变量
ENV TZ=America/New_York
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y tzdata chrony curl && \
    ln -fs /usr/share/zoneinfo/America/New_York /etc/localtime && \
    dpkg-reconfigure --frontend noninteractive tzdata

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8899

# 启动 Web 控制台服务
CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8899"]
