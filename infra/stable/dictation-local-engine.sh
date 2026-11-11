#!/usr/bin/env bash
# Prepared, not run: Python packages and model files for the dictation relay (staging and
# production share /opt/atlas-src/stable-python, so this also changes production's runtime).
set -Eeuo pipefail
PY=/opt/atlas-src/stable-python/bin/python
"$PY" -m pip install websockets==17.3 onnx-asr==0.12.0   # no other dependency: numpy, onnxruntime 1.31.0, huggingface_hub already there
M=/opt/atlas-src/models/stt-stream
mkdir -p "$M"
"$PY" - <<'PYDL'
from huggingface_hub import hf_hub_download, snapshot_download
M = "/opt/atlas-src/models/stt-stream"
snapshot_download("istupakov/parakeet-tdt-0.6b-v3-onnx", revision="8f23f0c03c8761650bdb5b40aaf3e40d2c15f1ce",
                  allow_patterns=["config.json", "vocab.txt", "nemo128.onnx", "encoder-model.int8.onnx", "decoder_joint-model.int8.onnx"],
                  local_dir=f"{M}/parakeet-tdt-0.6b-v3-int8")
hf_hub_download("istupakov/silero-vad-onnx", "silero_vad.onnx", revision="b3e3ee3cce4c11ceb63b1a0b229d916069c1ddf6",
                local_dir=f"{M}/silero-vad")
PYDL
rm -rf "$M"/parakeet-tdt-0.6b-v3-int8/.cache "$M"/silero-vad/.cache
( cd "$M" && sha256sum -c - ) <<'SUM'
666903c76b9798caf2c210afd4f6cd60b08a8dbf9800ec8d7a3bc0d2148ac466  parakeet-tdt-0.6b-v3-int8/config.json
eea7483ee3d1a30375daedc8ed83e3960c91b098812127a0d99d1c8977667a70  parakeet-tdt-0.6b-v3-int8/decoder_joint-model.int8.onnx
6139d2fa7e1b086097b277c7149725edbab89cc7c7ae64b23c741be4055aff09  parakeet-tdt-0.6b-v3-int8/encoder-model.int8.onnx
a9fde1486ebfcc08f328d75ad4610c67835fea58c73ba57e3209a6f6cf019e9f  parakeet-tdt-0.6b-v3-int8/nemo128.onnx
d58544679ea4bc6ac563d1f545eb7d474bd6cfa467f0a6e2c1dc1c7d37e3c35d  parakeet-tdt-0.6b-v3-int8/vocab.txt
1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3  silero-vad/silero_vad.onnx
SUM
du -sh "$M"
