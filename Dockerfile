FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

WORKDIR /app

# 设置时区与环境变量
ENV TZ=America/New_York
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN ln -fs /usr/share/zoneinfo/America/New_York /etc/localtime && \
    apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tzdata chrony curl && \
    dpkg-reconfigure --frontend noninteractive tzdata && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8899

# 启动入口脚本 (带 Xvfb 虚拟显卡环境)
RUN chmod +x entrypoint.sh 2>/dev/null || true
CMD ["./entrypoint.sh"]
