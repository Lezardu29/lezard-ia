from gateway.classifier import classify


def user(text, **extra):
    return {"messages": [{"role": "user", "content": text}], **extra}


def test_simple_chat_is_low():
    c = classify(user("Salut, ça va ?"))
    assert c.task == "chat" and c.complexity == "low"
    assert not (c.needs_code or c.needs_json or c.needs_vision or c.needs_tools)


def test_code_detection():
    c = classify(user("Corrige ce bug :\n```python\ndef f(x):\n    return x/0\n```"))
    assert c.needs_code and c.task == "code"


def test_code_intent_without_syntax():
    assert classify(user("Écris un script Python qui renomme des fichiers")).needs_code


def test_json_via_response_format_and_keyword():
    assert classify(user("Donne les villes", response_format={"type": "json_object"})).needs_json
    assert classify(user("Réponds en JSON avec nom et âge")).needs_json


def test_vision_and_tools():
    msg = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Décris l'image"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                ],
            }
        ],
        "tools": [{"type": "function", "function": {"name": "f"}}],
    }
    c = classify(msg)
    assert c.needs_vision and c.needs_tools and c.prompt_tokens >= 850


def test_hard_reasoning_is_high():
    c = classify(
        user(
            "Analyse et compare deux architectures de microservices, explique pourquoi l'une "
            "scale mieux, puis démontre étape par étape l'impact sur la latence :\n"
            "1. réseau\n2. base de données\n3. cache\nQuels trade-offs ? Quels risques ? Quel coût ?"
        )
    )
    assert c.complexity == "high" and c.task == "reasoning"


def test_math():
    c = classify(user("Calcule l'intégrale de x^2 entre 0 et 3"))
    assert c.task == "math" and c.complexity in ("medium", "high")


def test_task_labels():
    assert classify(user("Traduis ce texte en anglais : bonjour")).task == "translation"
    assert classify(user("Résume cet article en 3 lignes")).task == "summarization"


def test_context_size():
    long_text = "mot " * 20_000
    c = classify(user(long_text, max_tokens=500))
    assert c.prompt_tokens > 15_000 and c.min_context == c.prompt_tokens + 500
    assert "prompt long" in c.signals


def test_a_request_with_several_deliverables_is_high():
    """« Prépare un plan : A, B, C, D, E » : chaque partie peut aller à un expert (multi-agents auto)."""
    c = classify(
        user(
            "Prépare un plan complet pour lancer une petite entreprise de réparation de PC : étude de marché locale, "
            "statut juridique en France, budget de départ détaillé, stratégie de communication, et planning sur six mois."
        )
    )
    assert c.complexity == "high" and "plusieurs parties" in c.signals
    three = classify(user("Rédige une lettre de motivation, un CV et un message LinkedIn, pour un poste de technicien"))
    assert "plusieurs parties" in three.signals


def test_a_plain_enumeration_is_not_more_complex():
    c = classify(user("Donne-moi le prix, la taille, la couleur et le poids de ce modèle."))
    assert "plusieurs parties" not in c.signals and c.complexity == "low"
    c = classify(user("Le développement web, mobile, cloud et IA : lequel me conseilles-tu ?"))
    assert "plusieurs parties" not in c.signals  # « développement » : un nom, pas une demande à produire
