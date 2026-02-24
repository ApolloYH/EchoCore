#pragma once 
// system 
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <deque>
#include <iostream>
#include <fstream>
#include <sstream>
#include <iterator>
#include <list>
#include <locale.h>
#include <vector>
#include <string>
#include <math.h>
#include <numeric>
#include <cstring>
#include <cctype>

#ifdef _WIN32
#include <win_func.h>
#else
#include <unistd.h>
#endif

using namespace std;
// third part
#if defined(__APPLE__)
#include <onnxruntime/onnxruntime_cxx_api.h>
#else
#include "onnxruntime_run_options_config_keys.h"
#include "onnxruntime_cxx_api.h"
#include "itn-model.h"
#include "itn-processor.h"
#endif

#include "kaldi-native-fbank/csrc/feature-fbank.h"
#include "kaldi-native-fbank/csrc/online-feature.h"
#include "kaldi/decoder/lattice-faster-online-decoder.h"
// mine
#include <glog/logging.h>


#include "common-struct.h"
#include "com-define.h"
#include "commonfunc.h"
#include "predefine-coe.h"
#include "model.h"
#include "vad-model.h"
#include "punc-model.h"
#include "tokenizer.h"
#include "ct-transformer.h"
#include "ct-transformer-online.h"
#include "e2e-vad.h"
#include "fsmn-vad.h"
#include "encode_converter.h"
#include "vocab.h"
#include "phone-set.h"
#include "wfst-decoder.h"
#include "audio.h"
#include "fsmn-vad-online.h"
#include "tensor.h"
#include "util.h"
#include "seg_dict.h"
#include "resample.h"
#include "paraformer.h"
#include "sensevoice-small.h"
#ifdef USE_GPU
#include "paraformer-torch.h"
#endif
#include "paraformer-online.h"
#include "offline-stream.h"
#include "tpass-stream.h"
#include "tpass-online-stream.h"
#include "funasrruntime.h"

#if !defined(__APPLE__)
inline bool ShouldEnableOrtCuda() {
    const char* raw = std::getenv("FUNASR_ORT_USE_CUDA");
    if (raw == nullptr || raw[0] == '\0') {
        return false;
    }

    std::string flag(raw);
    for (char& ch : flag) {
        ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
    }
    if (flag == "1" || flag == "true" || flag == "on" || flag == "yes") {
        return true;
    }
    return false;
}

inline bool TryEnableCudaExecutionProvider(Ort::SessionOptions& options, const std::string& model_tag) {
    if (!ShouldEnableOrtCuda()) {
        LOG(INFO) << model_tag << " CUDAExecutionProvider disabled by FUNASR_ORT_USE_CUDA";
        return false;
    }
    try {
        OrtCUDAProviderOptions cuda_options{};
        cuda_options.device_id = 0;
        options.AppendExecutionProvider_CUDA(cuda_options);
        LOG(INFO) << model_tag << " using CUDAExecutionProvider";
        return true;
    } catch (const std::exception& e) {
        LOG(WARNING) << model_tag << " fallback to CPUExecutionProvider: " << e.what();
        return false;
    }
}
#else
inline bool TryEnableCudaExecutionProvider(Ort::SessionOptions&, const std::string&) {
    return false;
}
#endif
