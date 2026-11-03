"""Blocage d'un fournisseur entier sur une clé (« fournisseur/* » dans les modèles interdits), sans lister ses modèles."""

import pytest

from gateway.permissions import PermissionDenied, check_model, model_allowed, validate_permissions
from gateway.registry import ModelRegistry

REG = ModelRegistry()
OR_FREE = REG.get("openrouter", "nvidia/nemotron-3.5-lightning:free", "cloud")
OR_OTHER = REG.get("openrouter", "openai/gpt-4o-mini", "cloud")
GROQ = REG.get("groq", "llama-3.1-8b-instant", "cloud")


def test_provider_name_blocks_every_model_of_that_provider():
    perms = validate_permissions({"models": {"deny": ["openrouter"]}})
    assert perms["models"]["deny"] == ["openrouter/*"]  # un nom seul = tout le fournisseur
    assert not model_allowed(perms, OR_FREE) and not model_allowed(perms, OR_OTHER)
    assert model_allowed(perms, GROQ)  # les autres fournisseurs restent permis


def test_blocked_provider_is_refused_with_a_clear_message():
    perms = validate_permissions({"models": {"deny": ["openrouter/*"]}})
    with pytest.raises(PermissionDenied, match="non autorisé"):
        check_model(perms, OR_OTHER)


def test_deny_wins_over_an_allowed_model_of_the_same_provider():
    perms = validate_permissions({"models": {"allow": ["openrouter/openai/gpt-4o-mini"], "deny": ["openrouter/*"]}})
    assert not model_allowed(perms, OR_OTHER)  # le refus l'emporte sur l'autorisation
