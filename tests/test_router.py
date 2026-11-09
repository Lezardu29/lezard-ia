import pytest

from gateway.classifier import classify
from gateway.registry import CLOUD_MODELS, ModelRegistry, infer_local
from gateway.router import LatencyTracker, RoutingError, candidates, normalize_mode, route

LOCAL_SMALL = infer_local("ollama", "llama3.2:3b")
LOCAL_MID = infer_local("ollama", "llama3.1:8b")
LOCAL_CODER = infer_local("ollama", "qwen2.5-coder:7b")
HAIKU = next(m for m in CLOUD_MODELS if m.id == "claude-haiku-4-5")
OPUS = next(m for m in CLOUD_MODELS if m.id == "claude-opus-4-5")
GROQ8 = next(m for m in CLOUD_MODELS if m.id == "llama-3.1-8b-instant")
MINI = next(m for m in CLOUD_MODELS if m.id == "gpt-4o-mini")
ALL = [LOCAL_SMALL, LOCAL_MID, LOCAL_CODER, HAIKU, OPUS, GROQ8, MINI]

SIMPLE = classify({"messages": [{"role": "user", "content": "Salut !"}]})
HARD = classify(
    {
        "messages": [
            {
                "role": "user",
                "content": "Analyse et compare deux architectures, explique pourquoi l'une scale mieux, "
                "puis démontre étape par étape :\n1. réseau\n2. base\n3. cache\nRisques ? Coût ? Délais ?",
            }
        ]
    }
)


def test_local_inference_from_name():
    assert LOCAL_SMALL.quality < LOCAL_MID.quality
    assert infer_local("ollama", "llama3.3:70b").quality == 0.8
    assert infer_local("ollama", "llava:13b").vision
    assert LOCAL_MID.cost_usd(10**6, 10**6) == 0


def test_economy_prefers_free_local():
    d = route(SIMPLE, "economy", ALL)
    assert d.provider == "ollama" and d.est_cost_usd == 0


def test_speed_prefers_lowest_ttft():
    assert route(SIMPLE, "speed", ALL).ref == "groq/llama-3.1-8b-instant"


def test_observed_latency_overrides_prior():
    lat = LatencyTracker()
    lat.observe("groq/llama-3.1-8b-instant", 3000)
    assert route(SIMPLE, "speed", ALL, lat).ref != "groq/llama-3.1-8b-instant"


def test_hard_request_respects_quality_floor():
    for mode in ("economy", "balanced"):
        d = route(HARD, mode, ALL)
        assert d.ref in ("anthropic/claude-haiku-4-5", "anthropic/claude-opus-4-5"), mode
    # économique + difficile -> le moins cher qui atteint le plancher 0.75
    assert route(HARD, "economy", ALL).ref == "anthropic/claude-haiku-4-5"


def test_floor_relaxed_when_only_weak_models():
    d = route(HARD, "balanced", [LOCAL_SMALL, LOCAL_MID])
    assert d.ref == "ollama/llama3.1:8b" and "assoupli" in d.reason


def test_vision_and_context_filters():
    img = classify(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "décris"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,A"}},
                    ],
                }
            ]
        }
    )
    d = route(img, "economy", ALL)
    assert d.ref in ("anthropic/claude-haiku-4-5", "anthropic/claude-opus-4-5", "openai/gpt-4o-mini")
    big = classify({"messages": [{"role": "user", "content": "x " * 40_000}]})
    assert route(big, "economy", ALL).provider != "ollama"  # 8k de contexte local insuffisant


def test_code_bonus_for_coder_model():
    code = classify({"messages": [{"role": "user", "content": "Écris un script python"}]})
    assert route(code, "economy", [LOCAL_MID, LOCAL_CODER]).ref == "ollama/qwen2.5-coder:7b"


def test_alternatives_and_errors():
    d = route(SIMPLE, "balanced", ALL)
    assert d.alternatives and d.ref not in d.alternatives
    with pytest.raises(RoutingError):
        route(SIMPLE, "turbo", ALL)
    img = classify(
        {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "http://x/y.png"}}]}]}
    )
    with pytest.raises(RoutingError):
        route(img, "balanced", [LOCAL_MID])
    assert normalize_mode("Économique") == "economy" and normalize_mode(None) == "balanced"


def test_candidates_filters_unknown_cloud_and_embeddings():
    reg = ModelRegistry()
    ms = candidates(
        reg,
        {"ollama": ["llama3:8b", "nomic-embed-text"], "openai": ["gpt-4o-mini", "dall-e-3"]},
        {"ollama": "local", "openai": "cloud"},
    )
    assert {m.ref for m in ms} == {"ollama/llama3:8b", "openai/gpt-4o-mini"}


# ----- « auto » et la carte graphique : un modèle local qui en déborde est écarté -----
def _placed(fits: dict[str, int], vram_gb: float) -> ModelRegistry:
    reg = ModelRegistry([])
    reg.vram_budget = {"gpu": "RTX", "vram": vram_gb * 1e9}
    reg.local_memory = {f"ollama/{m}": int(gb * 1e9) for m, gb in fits.items()}
    return reg


def test_auto_skips_local_model_that_overflows_the_card():
    reg = _placed({"deepseek-r1:14b": 15.9, "gemma4:e4b": 11.1}, 12.0)
    big, small = reg.get("ollama", "deepseek-r1:14b", "local"), reg.get("ollama", "gemma4:e4b", "local")
    assert (big.fit, small.fit) == ("offload", "vram")
    for mode in ("balanced", "economy", "speed"):
        d = route(HARD, mode, [big, small])
        assert d.ref == small.ref, mode
        assert big.ref in d.alternatives  # reste en secours


def test_overflowing_local_model_still_used_when_alone_or_against_paid_cloud():
    reg = _placed({"deepseek-r1:14b": 15.9}, 12.0)
    big = reg.get("ollama", "deepseek-r1:14b", "local")
    assert route(SIMPLE, "balanced", [big]).ref == big.ref
    assert route(SIMPLE, "economy", [big, HAIKU]).ref == big.ref  # jamais un cloud payant à sa place


def test_unknown_memory_or_no_card_keeps_old_behaviour():
    reg = _placed({}, 12.0)
    assert reg.get("ollama", "deepseek-r1:14b", "local").fit == ""
    reg = ModelRegistry([])
    reg.local_memory = {"ollama/deepseek-r1:14b": int(16e9)}
    assert reg.get("ollama", "deepseek-r1:14b", "local").fit == ""  # matériel pas encore relevé
