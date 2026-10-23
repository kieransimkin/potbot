"""A fixed, offline local-generation tool for the existing ToolGateway.

This is operator-selected tool execution, not the synthetic InferenceGateway or
an autonomous model controller. The controller owns paths, resources and admission.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from importlib.metadata import version
from pathlib import Path
from typing import Any

from potbot.domain.errors import InvalidInputError


def load_request(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        raw = stream.read(65537)
    if not 1 <= len(raw) <= 65536:
        raise InvalidInputError("Local-model input is outside its byte bound.")
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {"prompt", "max_new_tokens"}:
        raise InvalidInputError("Local-model request fields are invalid.")
    if not isinstance(value["prompt"], str) or not 1 <= len(value["prompt"]) <= 16000:
        raise InvalidInputError("Local-model prompt is outside its bound.")
    if type(value["max_new_tokens"]) is not int or not 1 <= value["max_new_tokens"] <= 512:
        raise InvalidInputError("Local-model output limit is invalid.")
    value["request_sha256"] = hashlib.sha256(raw).hexdigest()
    return value


def checked_model(root: Path, model_id: str, revision: str, manifest_sha256: str) -> dict[str, Any]:
    root = root.resolve(strict=True)
    raw = (root / "model-manifest.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest_sha256:
        raise InvalidInputError("The operator-pinned model manifest changed.")
    manifest = json.loads(raw)
    if (
        not isinstance(manifest, dict)
        or not isinstance(manifest.get("files"), list)
        or not manifest["files"]
    ):
        raise InvalidInputError("The model manifest must enumerate its files.")
    if manifest.get("model_id") != model_id:
        raise InvalidInputError("The operator-selected model identity does not match.")
    if (
        manifest.get("revision") != revision
        or len(revision) != 40
        or any(c not in "0123456789abcdef" for c in revision)
    ):
        raise InvalidInputError("The model revision is not pinned.")
    seen: set[str] = set()
    for entry in manifest["files"]:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"path", "bytes", "sha256"}
            or not isinstance(entry["path"], str)
            or not entry["path"]
            or entry["path"] in seen
            or type(entry["bytes"]) is not int
            or entry["bytes"] < 1
            or not isinstance(entry["sha256"], str)
            or len(entry["sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in entry["sha256"])
        ):
            raise InvalidInputError("A model manifest file entry is invalid.")
        seen.add(entry["path"])
        path = root / entry["path"]
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root):
            raise InvalidInputError("Model files must stay inside the pinned model root.")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(4 * 1024**2), b""):
                digest.update(block)
        if path.stat().st_size != entry["bytes"] or digest.hexdigest() != entry["sha256"]:
            raise InvalidInputError("A pinned model file changed.")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-directory", required=True, type=Path)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    args = parser.parse_args()
    started = time.monotonic()
    request = load_request(args.request)
    manifest = checked_model(
        args.model_directory, args.model_id, args.revision, args.manifest_sha256
    )
    import resource

    import torch  # type: ignore[import-not-found]
    from transformers import AutoModelForCausalLM, AutoTokenizer  # type: ignore[import-not-found]

    torch.set_num_threads(4)
    torch.set_num_interop_threads(1)
    tokenizer = AutoTokenizer.from_pretrained(
        str(args.model_directory), local_files_only=True, trust_remote_code=False
    )
    # A standard assistant prefill sets structure without supplying any answer.
    # Preserve it separately from the genuinely generated continuation below.
    assistant_prefix = "{"
    wire = tokenizer.apply_chat_template(
        [
            {
                "role": "system",
                "content": (
                    "Follow the supplied task. Return only one raw JSON object. "
                    "Do not use Markdown, backticks, code fences, headings or commentary. "
                    "Your response must start with { and end with }."
                ),
            },
            {"role": "user", "content": request["prompt"]},
            {"role": "assistant", "content": assistant_prefix},
        ],
        tokenize=False,
        continue_final_message=True,
    )
    encoded = tokenizer(wire, return_tensors="pt")
    input_tokens = encoded["input_ids"].shape[-1]
    if input_tokens > 4000:
        raise InvalidInputError("The rendered input exceeds the operator's token bound.")
    model = AutoModelForCausalLM.from_pretrained(
        str(args.model_directory),
        local_files_only=True,
        trust_remote_code=False,
        torch_dtype=torch.float32,
        attn_implementation="eager",
        use_safetensors=True,
    ).to("cpu")
    model.eval()
    with torch.inference_mode():
        output = model.generate(
            **encoded,
            max_new_tokens=request["max_new_tokens"],
            do_sample=False,
            pad_token_id=tokenizer.eos_token_id,
        )
    generated = output[0, input_tokens:]
    text = tokenizer.decode(generated, skip_special_tokens=True)
    print(
        json.dumps(
            {
                "fixture": False,
                "route": "registered_offline_local_generation_tool",
                "model_id": manifest["model_id"],
                "revision": manifest["revision"],
                "model_manifest_sha256": args.manifest_sha256,
                "device": "cpu",
                "torch": version("torch"),
                "transformers": version("transformers"),
                "request_sha256": request["request_sha256"],
                "generated_text": text,
                "assistant_prefix": assistant_prefix,
                "response_text": assistant_prefix + text,
                "decoding": "assistant_json_object_prefill_v1",
                "input_tokens": int(input_tokens),
                "output_tokens": int(generated.shape[-1]),
                "usage_origin": "measured",
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "limits": {
                    "address_space_bytes": resource.getrlimit(resource.RLIMIT_AS)[0],
                    "cpu_seconds": resource.getrlimit(resource.RLIMIT_CPU)[0],
                },
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
