"""One canonical model download namespace across Linux and WSL2 guest tools.

These routes are not authority to install a model. Raw caches and model weights
are sandbox-writable *staging*, never the host-owned shared model library.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

from potbot.domain.errors import InvalidInputError, ScopeDeniedError

MODEL_STAGING_GUEST: Final = "/var/ai"
SHARED_MODELS_GUEST: Final = "/opt/potbot/models"

# Includes the standard paths that ignore environment configuration. Each
# relative path is created in an agent's skeleton HOME and points to a guest
# absolute location. Other model caches remain reachable via env variables.
MODEL_HOME_ALIASES: Final[dict[str, str]] = {
    ".cache/huggingface": "/var/ai/cache/huggingface",
    ".cache/torch": "/var/ai/cache/torch",
    ".cache/whisper": "/var/ai/cache/whisper",
    ".cache/modelscope": "/var/ai/cache/modelscope",
    ".cache/insightface": "/var/ai/cache/insightface",
    ".cache/keras": "/var/ai/cache/keras",
    ".cache/ultralytics": "/var/ai/cache/ultralytics",
    ".ollama/models": "/var/ai/cache/ollama/models",
}

MODEL_ENV: Final[dict[str, str]] = {
    "POTBOT_AI_HOME": "/var/ai",
    "POTBOT_SHARED_MODELS": "/opt/potbot/models",
    "HF_HOME": "/var/ai/cache/huggingface",
    "HF_HUB_CACHE": "/var/ai/cache/huggingface/hub",
    "HF_XET_CACHE": "/var/ai/cache/huggingface/xet",
    "HUGGINGFACE_HUB_CACHE": "/var/ai/cache/huggingface/hub",
    "TORCH_HOME": "/var/ai/cache/torch",
    "MODELSCOPE_CACHE": "/var/ai/cache/modelscope/hub",
    "OLLAMA_MODELS": "/var/ai/cache/ollama/models",
    "XDG_CACHE_HOME": "/var/ai/cache",
    "SENTENCE_TRANSFORMERS_HOME": "/var/ai/cache/sentence-transformers",
    "TFHUB_CACHE_DIR": "/var/ai/cache/tensorflow-hub",
    "KERAS_HOME": "/var/ai/cache/keras",
    "WHISPER_CACHE": "/var/ai/cache/whisper",
    "INSIGHTFACE_HOME": "/var/ai/cache/insightface",
}


@dataclass(frozen=True, slots=True)
class ModelDownloadingTool:
    """Installation-time declaration; no tools gain permission by listing here."""

    name: str
    may_download_models: bool
    required_environment: tuple[str, ...] = ()
    notes: str = ""


# Source-audited DanceFlow risk candidates (not proof every call downloads).
TOOL_DOWNLOAD_POLICIES: Final[tuple[ModelDownloadingTool, ...]] = (
    ModelDownloadingTool("StemLab", True, ("HF_HOME", "TORCH_HOME")),
    ModelDownloadingTool("DanceRudiments", False),
    ModelDownloadingTool("FrameRudiments", True, ("HF_HOME", "TORCH_HOME")),
    ModelDownloadingTool("DanceMoves", False),
    ModelDownloadingTool("KeywordMoves", True, ("HF_HOME", "SENTENCE_TRANSFORMERS_HOME")),
    ModelDownloadingTool("RasterMoves", True, ("HF_HOME", "TORCH_HOME")),
    ModelDownloadingTool("LikenessForge", True, ("HF_HOME", "TORCH_HOME")),
    ModelDownloadingTool("PixelCue", True, ("HF_HOME", "TORCH_HOME")),
    ModelDownloadingTool("PrismMoves", True, ("HF_HOME",)),
    ModelDownloadingTool("ThumbMoves", False),
    ModelDownloadingTool("React Timeline Sequence", False),
    ModelDownloadingTool("EPK Social Metadata", False),
    ModelDownloadingTool("WorkflowMoves", False),
    ModelDownloadingTool("DanceVault", False),
    ModelDownloadingTool("MediaSlide", False),
    ModelDownloadingTool("DanceFlow for Unity", True, ("HF_HOME",)),
    ModelDownloadingTool("Potbot", True, ("HF_HOME", "TORCH_HOME")),
    ModelDownloadingTool("DanceFlow", True, ("HF_HOME", "TORCH_HOME")),
)


def model_environment() -> dict[str, str]:
    """A fresh env mapping; never accept model-selected overrides on sandbox launch."""
    return dict(MODEL_ENV)


def validate_model_destination(path: str) -> str:
    """Reject writes into the read-only model library or persistent /home.

    This is a *syntactic* tool admission check. A real in-guest path check with
    symlink-safe descriptors and a qualified mount namespace is still mandatory.
    """
    if not isinstance(path, str) or "\\" in path or "\x00" in path:
        raise InvalidInputError("model destination must be a canonical Linux absolute path")
    parts = PurePosixPath(path).parts
    if (not path.startswith("/var/ai/") or any(p in {".", "..", ""} for p in path.split("/")[1:])
            or path.endswith("/") or len(path.encode()) > 2048 or len(parts) < 4):
        raise ScopeDeniedError("model downloads must target a concrete /var/ai path")
    if not path.startswith(("/var/ai/cache/", "/var/ai/downloads/", "/var/ai/models/")):
        raise ScopeDeniedError("model download path must be in an admitted /var/ai area")
    return path


def validate_tool_installation(
    tool: ModelDownloadingTool, *, env: dict[str, str], destinations: tuple[str, ...],
    probe_performed: bool,
) -> None:
    """Gate tool install; do not turn an untested env override into a claim.

    The host must execute a no-network instrumented download-path probe and
    supply its observed destinations. Unscoped model downloads are denied.
    """
    if not probe_performed:
        raise InvalidInputError("tool installation needs a download-path probe")
    if tool.may_download_models:
        if not destinations:
            raise InvalidInputError("model-downloading tool needs observed model destinations")
        for key in tool.required_environment:
            if env.get(key) != MODEL_ENV[key]:
                raise ScopeDeniedError(f"model download environment route {key} is not enforced")
        for target in destinations:
            validate_model_destination(target)
    elif destinations:
        raise ScopeDeniedError("undeclared model download: tool risk catalogue needs review")
