from potbot.models.local_selection import shortlist_local_models


def test_moe_memory_uses_total_parameters_not_active() -> None:
    small = shortlist_local_models(vram_mb=8192)
    names = {entry['model'] for entry in small}
    assert 'microsoft/Phi-4-mini-reasoning' in names
    assert 'Qwen/Qwen3.5-35B-A3B' not in names
    assert all(not entry['qualified'] for entry in small)
    larger = shortlist_local_models(vram_mb=24576)
    assert 'Qwen/Qwen3.5-35B-A3B' in {entry['model'] for entry in larger}
