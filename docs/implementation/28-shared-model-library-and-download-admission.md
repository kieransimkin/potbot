# S3 — Shared model library, sandbox staging, and model-download admission

**Scope:** additive to S1/S2 on Potbot `main` at `9a0c9ed9e410e93d5e0a4a6d7262c8e529bdb067`.
**Status:** offline controller/launcher implementation, not a production-qualified
Windows/WSL2 mount or an end-to-end large-model download acceptance.

## Filesystem contract

```text
Host operator filesystem (private; must be disjoint from all workspace homes):
  $POTBOT_GLOBAL_MODELS_ROOT/
    blobs/<sha256-first-2>/<sha256>          # verified immutable bytes (0444)
    manifests/<canonical-model-key>.json    # reviewed exact file/revision contract
  $POTBOT_MODEL_LOCK_ROOT/model-store.lock  # operator-only Linux flock

Workspace A, guest:
  /                                        # read-only pinned rootfs
  /home                                   # persistent, writable shared workspace home
  /var/ai                                 # writable WORKSPACE-LOCAL EPHEMERAL staging bind
    cache/{huggingface,torch,modelscope,ollama,...}/
    downloads/<operator-ticket-uuid>/{weights,tokenizer,configuration}
    models/<canonical-model-key>/<file> -> /opt/potbot/models/blobs/<xx>/<sha256>
  /opt/potbot/models                       # GLOBAL READ-ONLY host bind

Workspace B: independent /var/ai, same read-only /opt/potbot/models.
```

The main Potbot app reads the **same** configured host model library through
`POTBOT_GLOBAL_MODELS_ROOT`; operator promotion writes it. Each sandbox uses
its own `/var/ai` staging. The image and global library are NOT writable inside
sandboxes. Staging is not part of a persistent backup and is disposable during
rootfs upgrades; `/home` and the separately managed `/usr/local/tools`
share-store are the only persistent guest-writable mounts.

Model files are named using a case-normalized `publisher--repository__r-<exact
revision>__<quantization>` key. The *operator*, not a model file name, attests to
source repository, immutable revision, quantization, complete file set and SHA-256.
Each file is streamed into a private temporary artifact, compared with the
expected byte count/digest, checked for a supported header, then linked into
the global content-addressed library. Cross-workspace deduplication is exact
SHA-256 equality. No pickle/`torch.load`, arbitrary code or weights are run by
this process. Verified bytes do not establish a runnable, safe or useful model.

The operator watches Linux filesystem changes using best-effort inotify with a
periodic scan fallback. It distinguishes *settling*, *needs_ticket*, *linked*
and *requires_review*. Common Hugging Face cache snapshot symlinks are treated
as tentative **only if** they resolve into the workspace's staging tree.
Ollama content-addressed blob filenames are detected but never assumed to
identify a full model. Partial `.part`, `.lock` and incomplete transfers are not
promoted. The operator can automatically promote only explicitly enrolled
private tickets with complete, settled files in `/var/ai/downloads/<ticket>`;
arbitrary unknown cache observations require review and an enrolled copy.

## Known model download locations and redirection

| Runtime | Familiar location | Sandbox route |
| --- | --- | --- |
| Hugging Face Hub/Transformers | `~/.cache/huggingface/hub` | `HF_HOME=/var/ai/cache/huggingface`; `HF_HUB_CACHE=/var/ai/cache/huggingface/hub` |
| Hugging Face Xet | `~/.cache/huggingface/xet` | `HF_XET_CACHE=/var/ai/cache/huggingface/xet` |
| PyTorch Hub | `~/.cache/torch/hub` | `TORCH_HOME=/var/ai/cache/torch` |
| Sentence Transformers | `~/.cache/torch/sentence_transformers` or configured location | `SENTENCE_TRANSFORMERS_HOME=/var/ai/cache/sentence-transformers` |
| ModelScope | `~/.cache/modelscope/hub` | `MODELSCOPE_CACHE=/var/ai/cache/modelscope/hub` |
| Ollama | `~/.ollama/models` for a user-mode runtime | `OLLAMA_MODELS=/var/ai/cache/ollama/models` |
| Whisper | `~/.cache/whisper` | home symlink and `WHISPER_CACHE` for compatible tools |
| InsightFace, Keras, Ultralytics | framework-specific `.cache` or dotdirs | image/skeleton aliases plus respective environment routes |
| TensorFlow Hub | environment-based cache | `TFHUB_CACHE_DIR=/var/ai/cache/tensorflow-hub` |

`/etc/skel` in the image and each newly created agent home include aliases for
frameworks that bypass environment settings. The absolute target remains inside
`/var/ai` in guest view. Existing customized agent homes are not overwritten;
operator-led migration must ask before creating missing aliases. Some third-party
libraries ignore these routes entirely and must fail tool-install qualification.

## Tool installation gate

A tool that can download a model must declare **all** model destinations and
its download routes, including hard-coded `models/` and local HF cache overrides.
An *instrumented, no-network* dry-run must record every destination. The installer
must reject a tool when the observed paths are outside `/var/ai` or when a
required environment route is missing. The suite covers the DanceFlow inventory,
including StemLab, PixelCue, RasterMoves, LikenessForge, FrameRudiments,
KeywordMoves and Potbot. A source audit is necessary to confirm unsupported
adapter hard-coded paths and additional download backends.

The stage gate is:

```sh
PYTHONPATH=src python scripts/check_model_cache_policy.py --image-contract sandbox/image/Dockerfile
PYTHONPATH=src python scripts/check_model_cache_policy.py \
  --tool RasterMoves --probe /operator/private/rastermoves-download-probe.json
```

The probe must be a host-owned JSON object with schema
`potbot-tool-model-probe/v1`, `tool`, `network_used:false`, exact
`environment`, and `paths` actually observed. A passing probe **does not
install or authorize** the tool; normal ToolGateway/SYN admission remains
mandatory. The host's real installer must invoke this gate on every new or
changed tool build, *not* rely solely on an image-build CI check. An
unrestricted Bash shell can still override its environment and write to
`/home`; path-only checks cannot defeat a malicious process. Keep such shells
workspace-exclusive until mediated writes and host filesystem confinement are
qualified.

## Host-operator workflow

1. Provision two disjoint, operator-controlled directories for the shared model
   library and model-store lock, and one ephemeral `/var/ai` staging directory
   per workspace. Store their canonical paths in trusted host configuration.
2. Verify the rootfs image digest/signature, the Linux/WSL filesystem and
   model-store ownership. Bootstrap standard cache subdirectories in staging.
3. Create an operator-owned exact download ticket (example schema below). The
   ticket is stored **outside `/home`, `/var/ai`, and the shared library**.
4. Start `scripts/model_store_operator.py watch --staging ... --tickets-dir ...
   --shared-root ... --lock-root ...`. It listens using inotify where supported,
   rescans periodically, and only promotes matching complete ticket files.
5. Model loading remains a separate device-/backend-/tokenizer-qualification
   step, through the global library (host) or `/var/ai/models/` links (guest).
6. Emit progress to Potbot's durable progress journal once the UI backend's
   progress API is integrated; no fabricated percentages or download ETA.

Example private ticket, with real hashes and exact immutable revision substituted:

```json
{
  "schema": "potbot-model-download-ticket/v1",
  "ticket_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "model": {
    "repository": "Qwen/Qwen3-0.6B-GGUF",
    "revision": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "quantization": "q4_k_m",
    "format": "gguf"
  },
  "files": [{"path": "model.gguf", "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc", "bytes": 1000000000}]
}
```

This is an **illustrative**, deliberately nonverified ticket. Neither the
revision nor the SHA/byte count is claimed to identify a real Qwen release.

## Qualification gaps / next steps

- Run real WSL2/Linux Bubblewrap tests proving `/var/ai` writable, `/home`
  persistent, rootfs/global library read-only, denied alternate mount paths,
  and no symlink traversal to a host directory.
- Replace the current file-stat-and-check intake with an openat2/`RESOLVE_BENEATH`
  hardened read broker and inode pinning to defeat hostile TOCTOU; verify
  permitted Linux mount devices and drop all host capabilities.
- Verify actual safetensors tensor ranges, GGUF metadata quantization and ONNX
  syntax, multi-shard integrity and model-license/provenance before acceptance.
- Add an operator-configured quota, free-disk budget, per-model rate controls,
  cancellation, reservations and garbage collection for temporary downloads.
- Move verified progress/manifest events into the workspace-scoped PostgreSQL
  event ledger and expose them to the UI's Download and Model Install panes.
- Test actual installations with pinned revisions of every DanceFlow tool that
  can download weights. Verify the installer invokes the gate on **every** path.
- Test WSL2 inotify behavior (including overflow/unmount) and reconciled
  incomplete transfers after restart. Never retry unknown remote requests.
- Include mixed quantization shards, a malicious cache index, disk exhaustion,
  multiple concurrent producers, tampered symlinks and cross-workspace
  promotion attempts in an adversarial test suite.

## Sources

- [HF cache guide](https://huggingface.co/docs/huggingface_hub/guides/manage-cache)
- [PyTorch Hub cache](https://github.com/pytorch/pytorch/blob/main/docs/source/hub.md)
- [Ollama model storage](https://github.com/ollama/ollama/blob/main/docs/faq.mdx)
- [ModelScope cache implementation](https://github.com/modelscope/modelscope/blob/master/modelscope/hub/utils/utils.py)
