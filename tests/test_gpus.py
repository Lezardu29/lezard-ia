"""Cartes graphiques de tous les fabricants (gateway/gpus.py) : AMD et Intel vues comme NVIDIA, partie graphique du
processeur reconnue et jamais retenue pour les modèles. Cas réel : Ryzen avec partie graphique intégrée et Radeon
RX 9070 XT de 16 Go, sous Windows (DXGI) et sous Linux (amdgpu)."""

import os

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import core, gpus, hardware, marketplace, model_catalog
from gateway.config import ProviderConfig
from gateway.main import create_app
from gateway.plans import PlanResolver
from gateway.store import DocumentStore

GIB = 1024**3
MASTER = {"Authorization": "Bearer master"}

# ce que DXGI rend sur ce PC : partie graphique du Ryzen, la carte, le rendu logiciel de Windows, un écran Hyper-V
RYZEN_9070XT = [
    {
        "name": "AMD Radeon(TM) Graphics",
        "vendor_id": 0x1002,
        "dedicated": 512 * 1024**2,
        "used": 100 * 1024**2,
        "flags": 0,
    },
    {"name": "AMD Radeon RX 9070 XT", "vendor_id": 0x1002, "dedicated": 16 * GIB, "used": 1 * GIB, "flags": 0},
    {"name": "Microsoft Basic Render Driver", "vendor_id": 0x1414, "dedicated": 0, "used": None, "flags": 2},
    {"name": "Microsoft Hyper-V Video", "vendor_id": 0x1414, "dedicated": 0, "used": None, "flags": 0},
]


@pytest.mark.parametrize(
    "vendor,name,dedicated,kind",
    [
        ("amd", "AMD Radeon RX 9070 XT", 16 * GIB, "dedicated"),
        ("amd", "AMD Radeon RX 7600", 8 * GIB, "dedicated"),
        ("amd", "AMD Radeon PRO W7900", 48 * GIB, "dedicated"),
        ("amd", "AMD Radeon(TM) Graphics", 512 * 1024**2, "integrated"),  # Ryzen 7000 / 9000
        ("amd", "AMD Radeon(TM) Graphics", 4 * GIB, "integrated"),  # même avec de la mémoire réservée par le BIOS
        ("amd", "AMD Radeon 780M Graphics", 2 * GIB, "integrated"),
        ("amd", "AMD Radeon 8060S Graphics", 32 * GIB, "integrated"),  # Ryzen AI Max : beaucoup de RAM, intégrée
        ("amd", "AMD Radeon(TM) Vega 8 Graphics", 512 * 1024**2, "integrated"),
        ("intel", "Intel(R) UHD Graphics 770", 128 * 1024**2, "integrated"),
        ("intel", "Intel(R) Iris(R) Xe Graphics", 128 * 1024**2, "integrated"),
        ("intel", "Intel(R) Arc(TM) Graphics", 128 * 1024**2, "integrated"),  # Core Ultra
        ("intel", "Intel(R) Arc(TM) A770 Graphics", 16 * GIB, "dedicated"),
        ("intel", "Intel(R) Arc(TM) B580 Graphics", 12 * GIB, "dedicated"),
        ("nvidia", "NVIDIA GeForce RTX 4070", 12 * GIB, "dedicated"),
        ("other", "Carte inconnue", 8 * GIB, "dedicated"),
        ("other", "Carte inconnue", 256 * 1024**2, "integrated"),
    ],
)
def test_dedicated_card_or_graphics_part_of_the_processor(vendor, name, dedicated, kind):
    assert gpus.kind_of(vendor, name, dedicated) == kind


def test_windows_sees_the_radeon_and_the_ryzen_graphics_but_not_virtual_adapters():
    found = gpus.from_dxgi(RYZEN_9070XT)
    assert [(g["name"], g["vendor"], g["kind"]) for g in found] == [
        ("AMD Radeon(TM) Graphics", "amd", "integrated"),
        ("AMD Radeon RX 9070 XT", "amd", "dedicated"),
    ]
    card = found[1]
    assert card["vram_total"] == 16 * GIB and card["vram_used"] == GIB and card["vram_free"] == 15 * GIB
    assert [g["index"] for g in found] == [0, 1]
    twice = gpus.from_dxgi(RYZEN_9070XT + [RYZEN_9070XT[1]])  # même carte vue deux fois (deux sorties)
    assert len(twice) == 2


def test_linux_reads_amdgpu(tmp_path):
    def card(name, vendor, total=None, used=None, product=None, device="0x7550"):
        d = tmp_path / name / "device"
        d.mkdir(parents=True)
        (d / "vendor").write_text(vendor)
        (d / "device").write_text(device)
        if total is not None:
            (d / "mem_info_vram_total").write_text(str(total))
            (d / "mem_info_vram_used").write_text(str(used))
        if product:
            (d / "product_name").write_text(product)

    card("card0", "0x1002", 512 * 1024**2, 64 * 1024**2, device="0x164e")  # partie graphique du Ryzen
    card("card1", "0x1002", 16 * GIB, 2 * GIB, "AMD Radeon RX 9070 XT")
    card("card2", "0x10de", device="0x2786")  # NVIDIA : lue par NVML
    (tmp_path / "card1-DP-1").mkdir()  # une sortie, pas une carte
    found = gpus.sysfs_gpus(tmp_path)
    assert [(g["name"], g["kind"], g["vram_total"]) for g in found] == [
        ("AMD Radeon (PCI 164e)", "integrated", 512 * 1024**2),
        ("AMD Radeon RX 9070 XT", "dedicated", 16 * GIB),
    ]
    assert found[1]["vram_used"] == 2 * GIB and found[1]["source"] == "amdgpu"
    assert gpus.sysfs_gpus(tmp_path / "absent") == []


def ryzen_pc(monkeypatch):
    """PC sans NVIDIA : NVML absent, pas de nvidia-smi ; DXGI rend la partie graphique du Ryzen et la Radeon."""
    monkeypatch.setattr(hardware.shutil, "which", lambda name: None)
    monkeypatch.setattr(hardware, "other_gpus", lambda: gpus.from_dxgi(RYZEN_9070XT))


def test_gpu_info_merges_amd_cards_and_hides_the_missing_nvidia_driver(monkeypatch):
    ryzen_pc(monkeypatch)
    info = hardware.gpu_info(None)
    assert info["error"] is None and info["source"] == "dxgi"  # pas de « NVML absent » sur un PC AMD
    assert [g["name"] for g in info["gpus"]] == ["AMD Radeon(TM) Graphics", "AMD Radeon RX 9070 XT"]


def test_models_are_recommended_for_the_radeon_never_for_the_processor_graphics(monkeypatch):
    ryzen_pc(monkeypatch)
    hw = {"gpus": hardware.gpu_info(None)["gpus"], "ram": {"total": 32 * GIB, "available": 24 * GIB}, "disk": {}}
    b = model_catalog.budget(hw)
    assert b["gpu"] == "AMD Radeon RX 9070 XT" and b["vram"] > 14 * GIB  # 16 Go moins la réserve de l'affichage
    reco = model_catalog.recommend(hw)
    fits = {m["model"]: m["fit"] for m in reco["models"]}
    assert any(f == "vram" for f in fits.values())
    best = reco["best"]["chat"]
    assert fits[best] == "vram", (best, fits[best])  # le modèle conseillé tient dans la carte
    # avant : « pas de carte graphique », modèles légers pour le processeur
    only_igpu = {**hw, "gpus": [g for g in hw["gpus"] if g["kind"] == "integrated"]}
    assert model_catalog.budget(only_igpu)["gpu"] is None and model_catalog.budget(only_igpu)["vram"] == 0


def test_bundles_use_the_radeon_but_comfyui_still_needs_nvidia(monkeypatch):
    ryzen_pc(monkeypatch)
    hw = {"gpus": hardware.gpu_info(None)["gpus"], "ram": {"total": 32 * GIB, "available": 24 * GIB}}
    out = {b["id"]: b for b in marketplace.bundles_for(model_catalog.recommend(hw), hw, lambda n: None, "win32")}
    assert out["equipe"]["suggested"] and out["code-local"]["suggested"]  # modèles rapides sur la carte
    images = out["images"]
    assert not images["suggested"] and "CUDA" in images["why_not"][0] and "9070 XT" in images["why_not"][0]


def test_dictation_only_counts_nvidia_cards(monkeypatch):
    """faster-whisper calcule sur la carte par CUDA : une Radeon le laisse sur le processeur, sans l'annoncer."""
    from gateway.providers import local_whisper

    ryzen_pc(monkeypatch)
    monkeypatch.setattr(local_whisper, "cuda_ready", lambda: False)
    size, device, why = local_whisper._plan_for_this_pc(None)
    assert device == "cpu" and "Radeon" not in why


def test_hardware_page_and_recommendations_show_the_radeon(monkeypatch):
    ryzen_pc(monkeypatch)
    app = create_app(
        [ProviderConfig("ollama", "openai", "http://ollama/v1", "local")],
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": [], "models": []}))
        ),
        plans=PlanResolver({}, "free", master_key="master"),
        store=DocumentStore(":memory:"),
    )
    monkeypatch.setattr(core, "HardwareScanner", hardware.HardwareScanner)
    with TestClient(app) as c:
        hw = c.get("/v1/admin/hardware", headers=MASTER).json()
        assert [(g["name"], g["kind"]) for g in hw["gpus"]] == [
            ("AMD Radeon(TM) Graphics", "integrated"),
            ("AMD Radeon RX 9070 XT", "dedicated"),
        ]
        reco = c.get("/v1/admin/models/recommendations", headers=MASTER).json()
        assert reco["budget"]["gpu"] == "AMD Radeon RX 9070 XT"


@pytest.mark.skipif(os.name != "nt", reason="DXGI : Windows seulement")
def test_real_dxgi_enumeration_on_this_windows_pc():
    """Sur Windows (CI windows-latest comprise), le vrai appel DXGI par ctypes : il rend une liste bien formée, sans
    le rendu logiciel de Windows, et ne lève jamais."""
    adapters = gpus.dxgi_adapters()
    assert isinstance(adapters, list) and adapters, "DXGI n'a rendu aucun adaptateur (au moins le rendu logiciel)"
    for a in adapters:
        assert isinstance(a["name"], str) and a["name"] and isinstance(a["dedicated"], int) and a["dedicated"] >= 0
        assert a["used"] is None or isinstance(a["used"], int)
    for g in gpus.from_dxgi(adapters):
        assert g["kind"] in ("dedicated", "integrated") and g["vendor"] != "microsoft"
    assert isinstance(gpus.other_gpus(), list)
