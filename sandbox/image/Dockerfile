# Build-time only: Docker Compose makes an immutable Linux rootfs. Potbot NEVER
# uses Docker for sandbox execution. Downloads stay in the host-side /var/ai bind.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG SOURCE_COMMIT
LABEL org.opencontainers.image.title="Potbot minimal Linux sandbox rootfs" \
      org.opencontainers.image.source="https://github.com/kieransimkin/potbot" \
      org.opencontainers.image.revision="${SOURCE_COMMIT}"
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        bash ca-certificates coreutils findutils grep sed git gzip tar util-linux \
        python3 python3-venv chromium fonts-liberation \
        figlet fortune-mod fortunes-min lolcat \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /home /var/ai /opt/potbot/models /usr/local/tools /etc/ai /etc/ai-past /etc/skel/.cache /etc/skel/.ollama \
    && chmod 2775 /usr/local/tools /etc/ai \
    && ln -s /usr/local/tools /etc/skel/Tools \
    && ln -s /var/ai/cache/huggingface /etc/skel/.cache/huggingface \
    && ln -s /var/ai/cache/torch /etc/skel/.cache/torch \
    && ln -s /var/ai/cache/whisper /etc/skel/.cache/whisper \
    && ln -s /var/ai/cache/modelscope /etc/skel/.cache/modelscope \
    && ln -s /var/ai/cache/insightface /etc/skel/.cache/insightface \
    && ln -s /var/ai/cache/keras /etc/skel/.cache/keras \
    && ln -s /var/ai/cache/ultralytics /etc/skel/.cache/ultralytics \
    && ln -s /var/ai/cache/ollama/models /etc/skel/.ollama/models \
    && rm -rf /tmp /var/tmp \
    && ln -s /home/.tmp /tmp \
    && ln -s /home/.tmp /var/tmp
# These must stay in sync with potbot.tools.model_cache_routes.MODEL_ENV.
ENV HOME=/home PATH=/usr/bin:/bin LANG=C.UTF-8 TMPDIR=/home/.tmp \
    POTBOT_AI_HOME=/var/ai POTBOT_SHARED_MODELS=/opt/potbot/models \
    HF_HOME=/var/ai/cache/huggingface \
    HF_HUB_CACHE=/var/ai/cache/huggingface/hub \
    HF_XET_CACHE=/var/ai/cache/huggingface/xet \
    HUGGINGFACE_HUB_CACHE=/var/ai/cache/huggingface/hub \
    TORCH_HOME=/var/ai/cache/torch MODELSCOPE_CACHE=/var/ai/cache/modelscope/hub \
    OLLAMA_MODELS=/var/ai/cache/ollama/models XDG_CACHE_HOME=/var/ai/cache \
    SENTENCE_TRANSFORMERS_HOME=/var/ai/cache/sentence-transformers \
    TFHUB_CACHE_DIR=/var/ai/cache/tensorflow-hub KERAS_HOME=/var/ai/cache/keras \
    WHISPER_CACHE=/var/ai/cache/whisper INSIGHTFACE_HOME=/var/ai/cache/insightface
# Xpra-controlled browser is part of the separately qualified immutable image.
# A copied Python executable keeps the guest path inside the rootfs admission.
RUN python3 -m venv --copies /opt/potbot/browser \
    && /opt/potbot/browser/bin/pip install --no-cache-dir \
         playwright==1.64.1 patchright==1.64.2 playwright-stealth==2.0.3
COPY src/potbot/tools/xpra_browser_worker.py /opt/potbot/browser_worker.py
COPY src/potbot/tools/browser_https_routes.py /opt/potbot/browser_https_routes.py
WORKDIR /home
CMD ["/bin/bash"]
