# CPU picture server: stable-diffusion.cpp (MIT) built for the host CPU, and the
# gate (gate.py: TLS, shared token, one queue, content classifier).
# Build on the image server itself: GGML_NATIVE=ON targets that CPU.
# Weights are never in the image: fetch_weights.sh puts them on the host and
# install.sh mounts them read-only.
FROM ubuntu:24.04 AS engine
ARG SDCPP_COMMIT=5d787408c0e053568c4558e3c9043e8ebabd4cae
ARG GGML_PREFIX=d803c238
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential cmake git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN git init -q /src \
 && cd /src \
 && git -c core.hooksPath=/dev/null remote add origin https://github.com/leejet/stable-diffusion.cpp \
 && git -c core.hooksPath=/dev/null fetch -q --depth 1 origin "$SDCPP_COMMIT" \
 && git -c core.hooksPath=/dev/null checkout -q FETCH_HEAD \
 && git -c core.hooksPath=/dev/null submodule update -q --init --depth 1 --recursive \
 && test "$(git rev-parse HEAD)" = "$SDCPP_COMMIT" \
 && git -C ggml rev-parse HEAD | grep -q "^$GGML_PREFIX"
RUN cmake -S /src -B /build -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=ON \
      -DSD_SERVER_BUILD_FRONTEND=OFF -DGGML_CCACHE=OFF \
 && cmake --build /build -j"$(nproc)" --target sd-cli sd-server

FROM ubuntu:24.04
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      python3 python3-pip libgomp1 ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /opt/gate/requirements.txt
# Binary wheels only, every version pinned, no dependency resolution.
RUN pip3 install --break-system-packages --no-cache-dir --only-binary :all: --no-deps \
      --index-url https://download.pytorch.org/whl/cpu torch==2.5.1+cpu \
 && pip3 install --break-system-packages --no-cache-dir --only-binary :all: --no-deps \
      -r /opt/gate/requirements.txt
COPY --from=engine /build/bin/sd-server /build/bin/sd-cli /opt/sd/
COPY gate.py /opt/gate/gate.py
ENV HOME=/tmp HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1
USER 65534:65534
EXPOSE 8790
ENTRYPOINT ["python3", "/opt/gate/gate.py"]
