"""Source-audited concurrency risk catalogue for the 17 DanceFlow components.

These are declared hazard *classes*, not authorization grants. Onboarding a new
component must review actual writer entry points, cache location overrides and
host-specific output directories before assigning a narrow effect footprint.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ComponentRisks:
    name: str
    repository: str
    source_commit: str
    package_locks: tuple[str, ...] = ()
    mutable_areas: tuple[str, ...] = ()
    global_effects: tuple[str, ...] = ()


DANCEFLOW_RISKS: tuple[ComponentRisks, ...] = (
    ComponentRisks(
        "DanceFlow", "kieransimkin/DanceFlow", "8963a2f270ec5264ab78974ec22a77258e1e2161",
        ("demos/package-lock.json",),
        ("demos/node_modules/", "release/", "generated MCP job artifacts"),
        ("GitHub release/registry recipes", "external WordPress deployment"),
    ),
    ComponentRisks(
        "StemLab", "kieransimkin/stemlab", "ffb903eeeaea3f45b77c7dc0743ac785bb478593",
        ("package-lock.json", "docker/requirements.lock.txt"),
        ("models/", ".cache/", ".work/", "results/", "docker-output/", "music/"),
        ("GPU model weights", "MCP job/status store", "release output"),
    ),
    ComponentRisks(
        "DanceRudiments", "kieransimkin/DanceRudiments", "0e2eb0aa053f82b395a1ff322b67ae70d0cf7b75",
        ("package-lock.json",),
        ("models/", "motion-packs/", "generated/", "build/", ".cache/"),
        ("npm cache", "published collections/recordings"),
    ),
    ComponentRisks(
        "FrameRudiments", "kieransimkin/FrameRudiments", "85acc6e9d2846708de865da761bb7406b45ce715",
        ("src/framerudiments/visualiser_assets/package-lock.json",),
        ("models/", ".uv-cache/", "visualiser-analysis/", ".build-temp/", "build/"),
        ("GPU/encoding devices", "shared preset caches", "release artifacts"),
    ),
    ComponentRisks(
        "DanceMoves", "kieransimkin/DanceMoves", "4661a7f43b3d58eb70d1664b92882fcca1f5b7a5",
        ("package-lock.json", "tests/harness/harness-template.lock.json"),
        (".build/", ".next/", "dist/", "release-output/", "models/"),
        ("WordPress destination", "release version/tag"),
    ),
    ComponentRisks(
        "KeywordMoves", "kieransimkin/keywordmoves", "9044ba40460af6e6165f6b2c49a0d2454704fdd7",
        (), (".keywordmoves-cache/", ".monitor-private/", "*.sqlite*-wal", "models/", ".task-temp/"),
        ("source/provider rate budgets", "SQLite monitoring/watchlist store"),
    ),
    ComponentRisks(
        "RasterMoves", "kieransimkin/RasterMoves", "02895dee6bb8b9da4afaab887a3886cc1d43793b",
        (), ("models/", "model-cache/", "*.part", "*.lock", "build/"),
        ("model downloads", "GPU memory"),
    ),
    ComponentRisks(
        "LikenessForge", "kieransimkin/LikenessForge", "c27f079ccaa481773bd971b508b26bc2fa398487",
        ("uv.lock",), ("models/", "outputs/", "*.safetensors", "build/"),
        ("GPU/LoRA training", "local model cache"),
    ),
    ComponentRisks(
        "PixelCue", "kieransimkin/PixelCue", "66c714a07ce71f11c4dbdfeb9a8d91438e5b4738",
        ("uv.lock",),
        ("models/", ".cache/", "*.sqlite3-wal", "*.sqlite3-shm", ".pixelcue-model-manifest.json"),
        ("GPU model load", "model manifest publication", "thumbnail cache"),
    ),
    ComponentRisks(
        "ThumbMoves", "kieransimkin/PixelCue", "66c714a07ce71f11c4dbdfeb9a8d91438e5b4738",
        (), ("packages/thumbmoves/", "OS thumbnail cache"),
        ("OS-level thumbnail cache" ,),
    ),
    ComponentRisks(
        "PrismMoves", "kieransimkin/PrismMoves", "bc0a1bd3e21e1e94e1510d40f4011be624c45b14",
        (), ("outputs/", "models/", "preset result/report paths"),
        ("PixelCue model backend", "GPU device"),
    ),
    ComponentRisks(
        "React Timeline Sequence", "kieransimkin/react-timeline-sequence", "169edf420bc4eb5c0664a10fb962421a5afe0814",
        ("package-lock.json",), ("node_modules/", "dist/", "package-artifacts/"),
        ("npm cache", "release deployment"),
    ),
    ComponentRisks(
        "EPK Social Metadata", "kieransimkin/kieran-epk-social-metadata", "2f5c3b569fe0522f128e3404a81d91ee21c9e10d",
        (), ("dist/", "build/"),
        ("WordPress metadata destination", "WordPress registry release"),
    ),
    ComponentRisks(
        "WorkflowMoves", "kieransimkin/WorkflowMoves", "b788ba9599cfd68c5e9ed8bb8eadf7686663cb79",
        (), (".work/", ".task-temp/", "managed target instructions", "install record"),
        ("guarded installation in OTHER projects", "release publication"),
    ),
    ComponentRisks(
        "DanceVault", "kieransimkin/DanceVault", "3c0e687e672d9b70501002319ca03669047ed11e",
        (), ("private/", "dist/", "tests/integration/.state/"),
        ("encrypted export and password vault", "WordPress deployment"),
    ),
    ComponentRisks(
        "MediaSlide", "kieransimkin/react-mediaslide", "cae7b86605d086ff8a80a26b41e7f21dbe8f44d7",
        ("package-lock.json",), ("node_modules/", "dist/"),
        ("npm cache", "demo publish", "package release"),
    ),
    ComponentRisks(
        "Potbot", "kieransimkin/potbot", "9a0c9ed9e410e93d5e0a4a6d7262c8e529bdb067",
        ("uv.lock", "native/potbot-identity/Cargo.lock", "frontend/package-lock.json"),
        (".venv/", "frontend/node_modules/", "models/", "memory/", "workspace home/"),
        ("PostgreSQL transactions", "workspace lease broker", "GitHub refs"),
    ),
    ComponentRisks(
        "DanceFlow for Unity", "kieransimkin/DanceFlow.Unity", "b7820d65f3eadf28f4d4eae395540ca6457f3437",
        ("tools/kandi-webgl/asset-lock.json", "tools/kandi-webgl/song-lock.json"),
        ("Library/", "Temp/", "Obj/", "Builds/", "Cache/", "UserSettings/"),
        ("Unity Editor asset database", "Unity build/publisher"),
    ),
)
