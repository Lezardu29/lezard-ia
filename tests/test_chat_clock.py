"""Date et heure du jour donnée au modèle : sans elle, il la devine (et se trompe : « mercredi » pour un lundi)."""

from datetime import datetime
from zoneinfo import ZoneInfo

from gateway.api.chat import clock_message


def test_clock_message_gives_the_weekday_date_and_time_in_the_user_timezone():
    now = datetime(2026, 10, 26, 15, 30, tzinfo=ZoneInfo("Europe/Paris"))
    msg = clock_message(now)
    assert msg["role"] == "system"
    assert "lundi 26/10/2026 15:30 (Europe/Paris)" in msg["content"]
    assert "sans les deviner" in msg["content"]


def test_clock_message_defaults_to_now():
    msg = clock_message()
    assert str(datetime.now(ZoneInfo("Europe/Paris")).year) in msg["content"]


def test_time_questions_are_detected_but_not_words_containing_them():
    from gateway.api.chat import TIME_QUESTION

    assert TIME_QUESTION.search("On est quel jour, et quelle heure est-il à Paris ?")
    assert TIME_QUESTION.search("Quelle est la date d'aujourd'hui ?")
    assert not TIME_QUESTION.search("Bonjour, peux-tu m'aider avec ma facture ?")
    assert not TIME_QUESTION.search("et maintenant ? liste les numéros de chapitre")  # « maintenant » seul : non


def test_clock_without_time_zone_database_uses_this_pc_time(monkeypatch):
    """Windows sans tzdata : ZoneInfo échoue ; la question sur la date ne doit plus finir en erreur 500."""
    import zoneinfo

    from gateway.api import chat

    def missing(key):
        raise zoneinfo.ZoneInfoNotFoundError(f"No time zone found with key {key}")

    monkeypatch.setattr(chat, "ZoneInfo", missing)
    msg = clock_message()
    assert str(datetime.now().year) in msg["content"] and "sans les deviner" in msg["content"]
