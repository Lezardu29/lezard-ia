"""Règles de méthode du contrôle du PC, sans dépendance à Windows : partagées par les vrais outils (pc.py) et le
faux bureau du banc d'essai (bench.py), pour que le banc mesure l'agent tel qu'il tourne vraiment."""

from __future__ import annotations

from . import RuleRefusal


def mouse_first(kind: str, last: dict, has_elements: bool, unchanged: bool) -> None:
    """Refuse une touche (pc_key) tant qu'un élément cliquable pourrait servir. Permis : juste après pc_type
    (Entrée valide la saisie), aucun élément détecté, dernier clic sans effet, ou après deux refus (le modèle a
    justifié). Écrire (`type`) exige d'avoir cliqué dans le champ, ou de venir d'ouvrir une application ou une page.
    pc_open, pc_open_url et pc_focus n'y passent pas : ce sont des commandes fiables, pas du clavier au hasard.
    `last` : {"kind": dernière action, "refused": refus clavier consécutifs}, mis à jour ici."""
    if last["refused"] >= 2:
        last["refused"] = 0
        return
    if kind == "type":
        if last["kind"] in ("click", "type", "command"):
            return
        message = "clique d'abord dans le champ où écrire (pc_click sur son numéro), puis pc_type."
    else:
        if last["kind"] == "type" or not has_elements or (last["kind"] == "click" and unchanged):
            return
        message = (
            "SOURIS D'ABORD : un élément cliquable de la liste fait-il l'affaire (bouton, icône de la barre des "
            "tâches, menu, lien) ? Clique-le avec pc_click 'element'. Clavier ou commande seulement si aucun ne "
            "convient : dis pourquoi dans 'reason' et réessaie."
        )
    last["refused"] += 1
    raise RuleRefusal("Refusée : " + message)
