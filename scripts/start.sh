#!/bin/bash

# EchoCore - 启动脚本
# 启动所有服务
# 支持 --2pass 参数启动实时模式（2pass），默认离线模式
# 支持 --gpu/--cpu 参数选择推理设备（默认 GPU）

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

# 颜色输出
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# ASR 服务类型: offline 或 2pass
ASR_SERVER_TYPE="offline"
# 推理设备: gpu 或 cpu
ASR_DEVICE="gpu"
for arg in "$@"; do
    case "$arg" in
        --2pass)
            ASR_SERVER_TYPE="2pass"
            ;;
        --2pass=*)
            ASR_SERVER_TYPE="${arg#*=}"
            ;;
        --gpu)
            ASR_DEVICE="gpu"
            ;;
        --cpu)
            ASR_DEVICE="cpu"
            ;;
    esac
done

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 收集 Python nvidia 包中的 CUDA11 依赖库（供 ORT CUDA EP 使用）
collect_cuda11_nvidia_libs() {
    python3 - <<'PY'
import os
import site

required = ("cublas/lib", "cuda_runtime/lib", "cudnn/lib", "cufft/lib", "curand/lib")
seen = set()
ordered = []

for sp in site.getsitepackages() + [site.getusersitepackages()]:
    if not os.path.isdir(sp):
        continue
    nvidia_root = os.path.join(sp, "nvidia")
    for rel in required:
        path = os.path.join(nvidia_root, rel)
        if os.path.isdir(path) and path not in seen:
            seen.add(path)
            ordered.append(path)

print(":".join(ordered))
PY
}

is_truthy() {
    local value="${1:-}"
    value="${value,,}"
    case "$value" in
        1|true|on|yes)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

# 检查端口是否被占用
check_port() {
    local port=$1
    if command -v lsof >/dev/null 2>&1; then
        if lsof -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
            return 1
        fi
        return 0
    fi

    if command -v ss >/dev/null 2>&1; then
        if ss -ltn "( sport = :$port )" 2>/dev/null | tail -n +2 | grep -q .; then
            return 1
        fi
        return 0
    fi

    # Fallback: treat a successful local TCP connect as "port is in use".
    if python3 - "$port" >/dev/null 2>&1 <<'PY'
import socket
import sys

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(0.3)
try:
    sock.connect(("127.0.0.1", int(sys.argv[1])))
except Exception:
    sys.exit(0)
else:
    sys.exit(1)
finally:
    sock.close()
PY
    then
        return 0
    fi
    return 1
}

# 启动ASR服务
start_asr() {
    log_info "启动 ASR 服务 (模式: ${ASR_SERVER_TYPE}, 设备: ${ASR_DEVICE})..."

    if ! check_port 10095; then
        log_warn "端口 10095 已被占用，ASR 服务可能已在运行"
        return 0
    fi

    local runtime_dir="${ASR_DIR:-./runtime}"
    local model_root="${MODEL_DIR:-./data/models}"
    if [ ! -d "$runtime_dir" ]; then
        log_error "Runtime 目录不存在: ${runtime_dir}"
        return 1
    fi
    if [ ! -d "$model_root" ]; then
        log_error "模型目录不存在: ${model_root}"
        return 1
    fi
    runtime_dir="$(cd "$runtime_dir" && pwd)"
    model_root="$(cd "$model_root" && pwd)"
    local asr_bin

    if [ "$ASR_SERVER_TYPE" = "2pass" ]; then
        asr_bin="${runtime_dir}/build/bin/funasr-wss-server-2pass"
    else
        asr_bin="${runtime_dir}/build/bin/funasr-wss-server"
    fi

    if [ ! -f "$asr_bin" ]; then
        log_error "ASR 服务不存在: ${asr_bin}"
        return 1
    fi

    local torch_dir
    torch_dir=$(python3 -c "import torch, os; print(os.path.dirname(torch.__file__))" 2>/dev/null || true)
    if [ -z "$torch_dir" ] && [ "$ASR_SERVER_TYPE" != "2pass" ]; then
        log_error "无法导入 torch，请先安装可用的 PyTorch 环境"
        return 1
    fi

    local torch_lib=""
    if [ -n "$torch_dir" ] && [ -d "${torch_dir}/lib" ]; then
        torch_lib="${torch_dir}/lib"
    fi

    local ort_cuda_enabled=0
    if [ "$ASR_SERVER_TYPE" = "2pass" ] && [ "$ASR_DEVICE" = "gpu" ] && is_truthy "${FUNASR_ORT_USE_CUDA:-0}"; then
        ort_cuda_enabled=1
    fi

    local cuda11_nvidia_libs=""
    if [ "$ort_cuda_enabled" = "1" ]; then
        cuda11_nvidia_libs=$(collect_cuda11_nvidia_libs 2>/dev/null || true)
    fi

    local ort_lib="${runtime_dir}/onnxruntime-linux-x64-1.14.0/lib"
    local runtime_build_src="${runtime_dir}/build/src"
    local runtime_yaml_lib="${runtime_dir}/build/yaml-cpp"
    local runtime_fst_lib="${runtime_dir}/build/openfst/src/lib"
    local runtime_glog_lib="${runtime_dir}/build/glog"
    local runtime_ffmpeg_lib="${runtime_dir}/ffmpeg-master-latest-linux64-gpl-shared/lib"
    local ld_parts=()
    if [ -n "$torch_lib" ]; then
        ld_parts+=("$torch_lib")
    fi
    if [ -d "$runtime_build_src" ]; then
        ld_parts+=("$runtime_build_src")
    fi
    if [ -d "$runtime_yaml_lib" ]; then
        ld_parts+=("$runtime_yaml_lib")
    fi
    if [ -d "$runtime_fst_lib" ]; then
        ld_parts+=("$runtime_fst_lib")
    fi
    if [ -d "$runtime_glog_lib" ]; then
        ld_parts+=("$runtime_glog_lib")
    fi
    if [ -d "$ort_lib" ]; then
        ld_parts+=("$ort_lib")
    fi
    if [ -d "$runtime_ffmpeg_lib" ]; then
        ld_parts+=("$runtime_ffmpeg_lib")
    fi
    if [ -n "$cuda11_nvidia_libs" ]; then
        ld_parts+=("$cuda11_nvidia_libs")
    fi
    if [ -d "/usr/local/cuda/lib64" ]; then
        ld_parts+=("/usr/local/cuda/lib64")
    fi
    if [ -n "${LD_LIBRARY_PATH:-}" ]; then
        ld_parts+=("${LD_LIBRARY_PATH}")
    fi

    local asr_ld_library_path=""
    if [ "${#ld_parts[@]}" -gt 0 ]; then
        asr_ld_library_path="$(IFS=:; echo "${ld_parts[*]}")"
    fi

    local asr_env=()
    if [ -n "$asr_ld_library_path" ]; then
        asr_env+=(LD_LIBRARY_PATH="$asr_ld_library_path")
    fi

    if [ "$ASR_SERVER_TYPE" = "2pass" ]; then
        if [ "$ASR_DEVICE" != "gpu" ]; then
            asr_env+=(FUNASR_ORT_USE_CUDA=0)
            log_info "2pass 实时模式使用 CPU 推理"
        elif [ "$ort_cuda_enabled" = "1" ]; then
            asr_env+=(FUNASR_ORT_USE_CUDA=1)
            log_warn "2pass ORT CUDA 已开启（显存占用和GPU利用率会显著提升）"
        else
            # 默认保持官方兼容行为：2pass 走 CPU EP，更稳且延迟通常更低
            asr_env+=(FUNASR_ORT_USE_CUDA=0)
            log_info "2pass 使用官方兼容模式（ORT走CPU；如需强制ORT-GPU请设置 FUNASR_ORT_USE_CUDA=1）"
        fi
    fi

    if [ "$ort_cuda_enabled" = "1" ] && [ -z "$cuda11_nvidia_libs" ]; then
        log_warn "未检测到 CUDA11 依赖库路径，2pass 可能回退到 CPUExecutionProvider"
    fi

    local asr_log="$(pwd)/logs/asr.log"
    mkdir -p "$(dirname "$asr_log")"
    : > "$asr_log"

    if [ "$ASR_SERVER_TYPE" = "2pass" ]; then
        # 2pass 模式 - 需要在线和离线模型
        local online_model="${model_root}/iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online-onnx"
        local offline_model="${model_root}/iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-onnx"
        local vad_model="${model_root}/iic/speech_fsmn_vad_zh-cn-16k-common-onnx"
        local punc_model="${model_root}/iic/punc_ct-transformer_zh-cn-common-vad_realtime-vocab272727-onnx"
        local lm_model="${model_root}/damo/speech_ngram_lm_zh-cn-ai-wesp-fst"
        local itn_model="${model_root}/thuduj12/fst_itn_zh"

        nohup env "${asr_env[@]}" "$asr_bin" \
            --download-model-dir "${model_root}" \
            --model-dir "${offline_model}" \
            --online-model-dir "${online_model}" \
            --vad-dir "${vad_model}" \
            --punc-dir "${punc_model}" \
            --lm-dir "${lm_model}" \
            --itn-dir "${itn_model}" \
            --decoder-thread-num 20 \
            --model-thread-num 1 \
            --io-thread-num 2 \
            --port 10095 \
            --certfile "" \
            --keyfile "" \
            > "$asr_log" 2>&1 &
    else
        # 离线模式
        local gpu_args=()
        if [ "$ASR_DEVICE" = "gpu" ]; then
            gpu_args+=(--gpu)
        else
            log_info "ASR 使用 CPU 推理（速度可能较慢）"
        fi

        nohup env "${asr_env[@]}" "$asr_bin" \
            --download-model-dir "${model_root}" \
            --model-dir "${model_root}/iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch" \
            --vad-dir "${model_root}/iic/speech_fsmn_vad_zh-cn-16k-common-onnx" \
            --punc-dir "${model_root}/iic/punc_ct-transformer_cn-en-common-vocab471067-large-onnx" \
            --itn-dir "${model_root}/thuduj12/fst_itn_zh" \
            --lm-dir "${model_root}/iic/speech_ngram_lm_zh-cn-ai-wesp-fst" \
            --decoder-thread-num 20 \
            --model-thread-num 1 \
            --io-thread-num 2 \
            --port 10095 \
            --certfile "" \
            --keyfile "" \
            --hotword "${model_root}/hotwords.txt" \
            --bladedisc false \
            --batch-size 20 \
            "${gpu_args[@]}" \
            > "$asr_log" 2>&1 &
    fi
    local asr_pid=$!

    # 模型加载较慢，等待日志中出现 listen on port
    for _ in {1..120}; do
        if ! kill -0 "$asr_pid" 2>/dev/null; then
            log_error "ASR 服务启动失败，进程已退出（查看 EchoCore/logs/asr.log）"
            tail -n 20 "$asr_log" || true
            return 1
        fi

        if grep -q "listen on port:10095" "$asr_log" 2>/dev/null; then
            log_info "ASR 服务已启动 (端口 10095)"
            return 0
        fi

        sleep 1
    done

    log_warn "ASR 仍在加载模型，请稍后再试（查看 EchoCore/logs/asr.log）"
}

# 启动Web服务
start_web() {
    log_info "启动 Web 服务..."

    if ! check_port 8080; then
        log_warn "端口 8080 已被占用，Web 服务可能已在运行"
        return 0
    fi

    cd "$SCRIPT_DIR/../backend"

    # 检查依赖
    python3 -c "import fastapi" 2>/dev/null || {
        log_error "FastAPI 未安装，请先运行 install.sh"
        exit 1
    }

    # 启动服务
    local web_log="../logs/web.log"
    local modelscope_cache="${MODELSCOPE_CACHE:-$(pwd)/../data/modelscope_cache}"
    mkdir -p "$(dirname "$web_log")"
    mkdir -p "$modelscope_cache"
    : > "$web_log"
    nohup env MODELSCOPE_CACHE="$modelscope_cache" python3 main.py > "$web_log" 2>&1 &
    local web_pid=$!

    for _ in {1..30}; do
        if ! kill -0 "$web_pid" 2>/dev/null; then
            log_error "Web 服务启动失败，进程已退出（查看 EchoCore/logs/web.log）"
            tail -n 20 "$web_log" || true
            return 1
        fi

        if ! check_port 8080; then
            log_info "Web 服务已启动 (端口 8080)"
            return 0
        fi
        sleep 1
    done

    log_warn "Web 服务正在启动中，请稍后再试（查看 EchoCore/logs/web.log）"
}

# 停止所有服务
stop_all() {
    log_info "停止所有服务..."

    # 停止 ASR 服务（包括 2pass 和离线版本）
    pkill -f "funasr-wss-server-2pass" 2>/dev/null || true
    pkill -f "funasr-wss-server($|[[:space:]])" 2>/dev/null || true
    log_info "ASR 服务已停止"

    # 停止 Web 服务
    pkill -f "main.py" 2>/dev/null || true
    log_info "Web 服务已停止"
}

# 查看状态
status() {
    echo ""
    echo "========================================"
    echo "服务状态"
    echo "========================================"

    # ASR 服务
    if check_port 10095; then
        echo -e "ASR 服务: ${RED}未运行${NC}"
    else
        echo -e "ASR 服务: ${GREEN}运行中 (${ASR_SERVER_TYPE} 模式, 设备 ${ASR_DEVICE}, 端口 10095)${NC}"
    fi

    # Web 服务
    if check_port 8080; then
        echo -e "Web 服务: ${RED}未运行${NC}"
    else
        echo -e "Web 服务: ${GREEN}运行中 (端口 8080)${NC}"
    fi

    echo ""
    echo "日志文件:"
    echo "  - ASR: EchoCore/logs/asr.log"
    echo "  - Web: EchoCore/logs/web.log"
    echo ""
}

# 显示帮助
show_help() {
    echo ""
    echo "用法: $0 [命令] [--2pass] [--gpu|--cpu]"
    echo ""
    echo "命令:"
    echo "  start     启动所有服务"
    echo "  stop      停止所有服务"
    echo "  restart   重启所有服务"
    echo "  status    查看服务状态"
    echo "  help      显示帮助"
    echo ""
    echo "选项:"
    echo "  --2pass   启用 2pass 实时模式（默认离线模式）"
    echo "            2pass 模式支持实时显示识别结果"
    echo "  --gpu     启用 GPU 推理（默认）"
    echo "  --cpu     禁用 GPU，改为 CPU 推理"
    echo ""
    echo "高级选项:"
    echo "  FUNASR_ORT_USE_CUDA=1   强制 2pass 的 ORT 模型使用 GPU（默认关闭，防止高占用和时延抖动）"
    echo ""
}

# 主函数
COMMAND="${1:-start}"
shift 2>/dev/null || true

# 处理剩余参数
while [ $# -gt 0 ]; do
    case "$1" in
        --2pass)
            ASR_SERVER_TYPE="2pass"
            ;;
        --2pass=*)
            ASR_SERVER_TYPE="${1#*=}"
            ;;
        --gpu)
            ASR_DEVICE="gpu"
            ;;
        --cpu)
            ASR_DEVICE="cpu"
            ;;
        *)
            log_error "未知参数: $1"
            show_help
            exit 1
            ;;
    esac
    shift
done

case "$COMMAND" in
    start)
        log_info "ASR 模式: ${ASR_SERVER_TYPE}, 推理设备: ${ASR_DEVICE}"
        start_asr
        start_web
        sleep 2
        status
        ;;
    stop)
        stop_all
        log_info "所有服务已停止"
        ;;
    restart)
        log_info "ASR 模式: ${ASR_SERVER_TYPE}, 推理设备: ${ASR_DEVICE}"
        stop_all
        sleep 2
        start_asr
        start_web
        sleep 2
        status
        ;;
    status)
        status
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        log_error "未知命令: $COMMAND"
        show_help
        exit 1
        ;;
esac
