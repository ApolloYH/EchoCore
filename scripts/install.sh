#!/bin/bash

# EchoCore - 安装脚本
# 安装依赖和编译ASR服务

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "========================================"
echo "EchoCore - 安装脚本"
echo "========================================"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 检查Python版本
check_python() {
    log_info "检查 Python 版本..."
    if ! command -v python3 &> /dev/null; then
        log_error "Python 3 未安装，请先安装 Python 3.8+"
        exit 1
    fi

    PYTHON_VERSION=$(python3 -c 'import sys; print(sys.version_info.major, sys.version_info.minor)')
    log_info "Python 版本: $PYTHON_VERSION"
}

# 安装系统依赖
install_system_deps() {
    log_info "安装系统依赖..."

    if command -v apt-get &> /dev/null; then
        sudo apt-get update
        sudo apt-get install -y \
            build-essential \
            cmake \
            libopenblas-dev \
            libssl-dev \
            wget \
            git
    elif command -v yum &> /dev/null; then
        sudo yum install -y \
            gcc-c++ \
            cmake \
            openblas-devel \
            openssl-devel \
            wget
    else
        log_warn "未检测到包管理器，请手动安装依赖：build-essential, cmake, libopenblas-dev, libssl-dev"
    fi
}

# 下载运行时依赖
download_deps() {
    log_info "下载运行时依赖..."

    if [ ! -d "onnxruntime-linux-x64-1.14.0" ]; then
        log_info "下载 onnxruntime..."
        wget -q https://isv-data.oss-cn-hangzhou.aliyuncs.com/ics/MaaS/ASR/dep_libs/onnxruntime-linux-x64-1.14.0.tgz
        tar -zxvf onnxruntime-linux-x64-1.14.0.tgz
        rm -f onnxruntime-linux-x64-1.14.0.tgz
    else
        log_info "onnxruntime 已存在，跳过下载"
    fi

    if [ ! -d "ffmpeg-master-latest-linux64-gpl-shared" ]; then
        log_info "下载 ffmpeg..."
        wget -q https://isv-data.oss-cn-hangzhou.aliyuncs.com/ics/MaaS/ASR/dep_libs/ffmpeg-master-latest-linux64-gpl-shared.tar.xz
        tar -xf ffmpeg-master-latest-linux64-gpl-shared.tar.xz
        rm -f ffmpeg-master-latest-linux64-gpl-shared.tar.xz
    else
        log_info "ffmpeg 已存在，跳过下载"
    fi
}

# 安装Python依赖
install_python_deps() {
    log_info "安装 Python 依赖..."
    cd "$SCRIPT_DIR/backend"

    pip install --upgrade pip
    pip install -r "$SCRIPT_DIR/requirements.txt"

    cd "$SCRIPT_DIR"
    log_info "Python 依赖安装完成"
}

# 编译ASR服务
compile_asr() {
    log_info "编译 ASR 服务..."

    cd "$SCRIPT_DIR/../runtime/websocket"

    if [ ! -d "build" ]; then
        mkdir -p build
    fi

    cd build

    cmake -DCMAKE_BUILD_TYPE=release .. \
        -DONNXRUNTIME_DIR="$SCRIPT_DIR/onnxruntime-linux-x64-1.14.0" \
        -DFFMPEG_DIR="$SCRIPT_DIR/ffmpeg-master-latest-linux64-gpl-shared"

    make -j$(nproc)

    cd "$SCRIPT_DIR"
    log_info "ASR 服务编译完成"
}

# 创建必要目录
create_dirs() {
    log_info "创建必要目录..."
    mkdir -p "$SCRIPT_DIR/logs"
    mkdir -p "$SCRIPT_DIR/data/meetings"
    mkdir -p "$SCRIPT_DIR/ssl"
}

# 主函数
main() {
    log_info "开始安装..."

    check_python
    install_system_deps
    download_deps
    create_dirs
    install_python_deps
    compile_asr

    log_info "安装完成！"
    echo ""
    echo "========================================"
    echo "下一步操作："
    echo "1. 启动 ASR 服务（确保已编译 runtime）："
    echo "   cd runtime && ./run_server.sh --port 10095 --certfile 0 --keyfile 0"
    echo "   或使用: ./scripts/start.sh start"
    echo ""
    echo "2. 启动 Web 服务："
    echo "   cd backend && python main.py"
    echo "   或使用: ./scripts/start.sh start"
    echo ""
    echo "3. 访问界面：http://localhost:8080"
    echo "========================================"
}

main "$@"
