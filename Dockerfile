FROM nvidia/cuda:12.2.2-cudnn8-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-dev \
    python-is-python3 \
    ffmpeg \
    libsndfile1 \
    libssl-dev \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt

# 先安装 CUDA 版 torch/torchaudio，避免被 CPU 版覆盖
RUN python3 -m pip install --upgrade pip setuptools wheel && \
    python3 -m pip install --index-url https://download.pytorch.org/whl/cu121 \
    torch==2.4.1 torchaudio==2.4.1 && \
    python3 -m pip install -r /app/requirements.txt

COPY . /app

RUN chmod +x /app/scripts/start.sh /app/scripts/build_runtime.sh
RUN mkdir -p /app/logs /app/data/models /app/data/uploads /app/data/temp /app/data/meetings /app/data/users

EXPOSE 8080 10095

# 默认实时模式 + GPU。2pass 默认使用官方兼容行为（ORT CPU EP）
CMD ["bash", "-lc", "./scripts/start.sh start --2pass --gpu && tail -F logs/asr.log logs/web.log"]
