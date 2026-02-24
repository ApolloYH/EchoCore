#!/bin/bash

# EchoCore runtime 一键编译脚本（完全使用本项目内源码）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
RUNTIME_DIR="${ROOT_DIR}/runtime"
BUILD_DIR="${RUNTIME_DIR}/build"
ONNX_SRC_DIR="${ROOT_DIR}/onnxruntime"
ONNX_LIB_DIR="${RUNTIME_DIR}/onnxruntime-linux-x64-1.14.0"
FFMPEG_DIR="${RUNTIME_DIR}/ffmpeg-master-latest-linux64-gpl-shared"

GPU="ON"
BUILD_TYPE="Release"
CLEAN=0
JOBS="$(nproc)"

usage() {
    cat <<'EOF'
用法:
  ./scripts/build_runtime.sh [--gpu|--cpu] [--clean] [--debug] [--jobs N]

选项:
  --gpu        启用 GPU 构建（默认）
  --cpu        CPU 构建（关闭 USE_GPU）
  --clean      先清理 runtime/build 再重新配置
  --debug      使用 Debug 构建类型（默认 Release）
  --jobs N     并行编译线程数（默认 nproc）
  -h, --help   显示帮助
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --gpu)
            GPU="ON"
            ;;
        --cpu)
            GPU="OFF"
            ;;
        --clean)
            CLEAN=1
            ;;
        --debug)
            BUILD_TYPE="Debug"
            ;;
        --jobs)
            shift
            JOBS="${1:-}"
            if [[ -z "$JOBS" ]]; then
                echo "[ERROR] --jobs 需要传入数字"
                exit 1
            fi
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "[ERROR] 未知参数: $1"
            usage
            exit 1
            ;;
    esac
    shift
done

if [[ ! -d "$ONNX_SRC_DIR/src" ]]; then
    echo "[ERROR] 缺少源码目录: $ONNX_SRC_DIR/src"
    echo "请确认项目根目录下存在 onnxruntime 源码（不是 runtime/onnxruntime-linux-x64-1.14.0）。"
    exit 1
fi

if [[ ! -d "$ONNX_LIB_DIR/lib" ]]; then
    echo "[ERROR] 缺少 ONNX Runtime 依赖目录: $ONNX_LIB_DIR/lib"
    exit 1
fi

if [[ ! -d "$FFMPEG_DIR/lib" ]]; then
    echo "[ERROR] 缺少 ffmpeg 依赖目录: $FFMPEG_DIR/lib"
    exit 1
fi

if [[ "$CLEAN" == "1" ]]; then
    echo "[INFO] 清理构建目录: $BUILD_DIR"
    cmake -E remove_directory "$BUILD_DIR"
fi

echo "[INFO] 配置 CMake (GPU=${GPU}, BUILD_TYPE=${BUILD_TYPE})"
cmake -S "$RUNTIME_DIR" -B "$BUILD_DIR" \
    -DCMAKE_BUILD_TYPE="$BUILD_TYPE" \
    -DCMAKE_CXX_STANDARD=17 \
    -DONNXRUNTIME_DIR="$ONNX_LIB_DIR" \
    -DFFMPEG_DIR="$FFMPEG_DIR" \
    -DGPU="$GPU"

echo "[INFO] 开始编译 runtime ..."
cmake --build "$BUILD_DIR" -j"$JOBS"

echo "[INFO] 编译完成:"
echo "  - $BUILD_DIR/bin/funasr-wss-server"
echo "  - $BUILD_DIR/bin/funasr-wss-server-2pass"
