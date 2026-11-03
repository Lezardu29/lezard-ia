"""Compétences (gateway/skills.py) : catalogue, licences, validation, injection dans la requête de chat."""

import pytest

from gateway import preferences, skills
from gateway.preferences import PreferenceError

from .test_workspaces import api  # noqa: F401 (fixture : gateway avec un faux Ollama qui garde les requêtes)


# ---------- catalogue ----------


def test_catalog_lists_every_pack_in_display_order():
    ids = [s["id"] for s in skills.catalog()]
    assert ids == list(skills.ORDER)
    for s in skills.catalog():
        assert s["name"] and s["description"] and s["tokens"] > 0
        assert "content" not in s  # le texte ne part qu'avec /v1/skills/{id}


def test_every_pack_credits_its_source_and_ships_its_license():
    notice = (skills.PACKS_DIR / "NOTICE.md").read_text(encoding="utf-8")
    for s in skills.load().values():
        assert (skills.PACKS_DIR / "licenses" / s.license_file).is_file(), s.id
        assert s.url.startswith("https://github.com/") and s.url in notice and s.id in notice
        assert s.license in ("MIT", "Apache-2.0")
    # « Caveman » est une marque : la compétence porte un autre nom et cite son origine
    concis = skills.load()["concis"]
    assert "Caveman" not in concis.name and "Caveman" in concis.source


def test_packs_stay_small():
    # chaque compétence active coûte ses tokens à chaque message : adaptations condensées
    assert all(s["tokens"] < 800 for s in skills.catalog())


def test_parse_rejects_a_pack_without_header_or_fields():
    with pytest.raises(skills.SkillError):
        skills.parse("# Sans en-tête")
    with pytest.raises(skills.SkillError, match="name"):
        skills.parse("---\nid: x\n---\nTexte")
    with pytest.raises(skills.SkillError):
        skills.parse("---\n" + "\n".join(f"{f}: v" for f in skills.FIELDS).replace("id: v", "id: Pas Bon") + "\n---\nT")


# ---------- validation ----------


def test_normalize_accepts_lists_and_comma_text():
    assert skills.normalize(None) == [] and skills.normalize("") == [] and skills.normalize([]) == []
    assert skills.normalize("concis, tdd,concis") == ["concis", "tdd"]
    assert skills.normalize(["tdd", "tdd"]) == ["tdd"]


@pytest.mark.parametrize(
    "value, message",
    [
        (["inconnue"], "inconnue"),
        ([1], "liste"),
        ({"concis": True}, "liste"),
        (["concis", "cadrer", "plan", "tdd", "debogage"], "au plus"),
    ],
)
def test_normalize_rejects_bad_values(value, message):
    with pytest.raises(skills.SkillError, match=message):
        skills.normalize(value)


def test_chat_preferences_keep_the_active_skills():
    assert preferences.validate_chat({"skills": ["concis"]})["skills"] == ["concis"]
    assert "skills" not in preferences.validate_chat({"skills": []})
    with pytest.raises(PreferenceError):
        preferences.validate_chat({"skills": ["inconnue"]})


# ---------- API ----------


def test_skills_routes(api):  # noqa: F811
    c, _ = api
    r = c.get("/v1/skills").json()
    assert [s["id"] for s in r["data"]] == list(skills.ORDER) and r["max_active"] == skills.MAX_ACTIVE
    assert r["catalog_url"].startswith("https://github.com/")
    detail = c.get("/v1/skills/concis").json()
    assert detail["content"].startswith("# Réponses concises") and detail["license"] == "Apache-2.0"
    assert c.get("/v1/skills/inconnue").status_code == 404


def test_active_skills_reach_the_model_after_the_project_prompt(api):  # noqa: F811
    c, up = api
    c.post(
        "/v1/chat/completions",
        json={
            "model": "ollama/llama3.1:8b",
            "skills": ["concis", "tdd"],
            "messages": [{"role": "user", "content": "tri"}],
        },
    )
    first = up.bodies[-1]["messages"][0]
    assert (
        first["role"] == "system"
        and "# Réponses concises" in first["content"]
        and "# Développement piloté" in first["content"]
    )
    assert "skills" not in up.bodies[-1]  # champ de la gateway, jamais transmis au fournisseur
    # en-tête, et après le prompt système d'un projet
    proj = c.post("/v1/projects", json={"name": "Dev", "system_prompt": "Réponds en Python."}).json()
    c.post(
        "/v1/chat/completions",
        headers={"x-gateway-skills": "debogage"},
        json={"project": proj["id"], "model": "ollama/llama3.1:8b", "messages": [{"role": "user", "content": "bug"}]},
    )
    msgs = up.bodies[-1]["messages"]
    assert msgs[0]["content"] == "Réponds en Python." and "# Débogage méthodique" in msgs[1]["content"]


def test_unknown_skill_is_refused_before_any_call(api):  # noqa: F811
    c, up = api
    n = len(up.bodies)
    r = c.post(
        "/v1/chat/completions",
        json={"model": "ollama/llama3.1:8b", "skills": ["x"], "messages": [{"role": "user", "content": "a"}]},
    )
    assert r.status_code == 400 and "inconnue" in r.json()["error"]["message"] and len(up.bodies) == n
