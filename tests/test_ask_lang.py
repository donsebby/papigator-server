"""Frag Papigator auf Englisch: Antwort in der Sprache der Frage, sonst nach `lang`; deutscher Prompt unverändert."""
import pytest

from app import ask


@pytest.mark.parametrize("question,lang,expected", [
    ("When does my car insurance expire?", None, "en"),
    ("How much did I pay for electricity last year?", "de", "en"),       # Frage zählt vor der Oberfläche
    ("Wann läuft meine Kfz-Versicherung aus?", "en", "de"),
    ("Telekom", None, "de"),                                              # nicht zu erkennen: wie bisher Deutsch
    ("Telekom", "en", "en"),
    ("Telekom", "en-GB", "en"),
])
def test_question_lang(question, lang, expected):
    assert ask.question_lang(question, lang) == expected


def test_german_answer_prompt_unchanged():
    # Deutsch: genau der bisherige Prompt; Englisch tauscht nur die Sprach-Regel
    assert ask.answer_prompt("de") is ask.ANSWER_PROMPT
    en = ask.answer_prompt("en")
    assert ask.EN_ANSWER_RULE in en and "Antworte auf Deutsch" not in en
    assert en.replace(ask.EN_ANSWER_RULE, "- Antworte auf Deutsch, kurz und konkret.") == ask.ANSWER_PROMPT


def test_keyword_prompt(monkeypatch):
    seen = []
    monkeypatch.setattr(ask, "_chat", lambda prompt, **k: seen.append(prompt) or '["Kfz-Versicherung"]')
    ask.keywords_for("Wann läuft die Versicherung aus?")
    ask.keywords_for("When does the insurance expire?", "en")
    assert seen[0] == ask.KEYWORD_PROMPT.format(question="Wann läuft die Versicherung aus?")
    assert ask.EN_KEYWORD_NOTE in seen[1] and seen[1].endswith("Frage: When does the insurance expire?")


def _setup(admin, monkeypatch, reply="Answer [Dok 1]"):
    admin.patch("/settings", json={"llm_enabled": True, "llm_url": "http://192.168.1.50:11434/v1", "llm_model": "m"})
    prompts = []

    def fake(prompt, timeout=180, purpose="tag"):
        prompts.append(prompt)
        return '["Versicherung"]' if len(prompts) == 1 else reply
    monkeypatch.setattr(ask, "_chat", fake)
    return prompts


def test_english_question_gets_english_answer_rule(admin, monkeypatch):
    admin.upload(text="Kfz-Versicherung Beitrag 2026", title="Versicherung")
    prompts = _setup(admin, monkeypatch)
    r = admin.post("/ask", json={"question": "When does my insurance expire?", "lang": "en"}).json()
    assert r["lang"] == "en" and r["answer"] == "Answer [Dok 1]"
    assert ask.EN_ANSWER_RULE in prompts[1]
    r = admin.post("/ask", json={"question": "Wann läuft meine Versicherung aus?", "lang": "en"}).json()
    assert r["lang"] == "de" and "Antworte auf Deutsch" in prompts[3]


def test_fixed_texts_in_english(admin, monkeypatch):
    r = admin.post("/ask", json={"question": "What is my tax ID?", "lang": "en"}).json()
    assert r["error"] and r["answer"].startswith("Asking is turned off")
    _setup(admin, monkeypatch)
    r = admin.post("/ask", json={"question": "Where is my tax ID?"}).json()
    assert r["answer"] == "I couldn't find any matching documents for that." and r["sources"] == []
    r = admin.post("/ask", json={"question": "Wo ist meine Steuer-ID?"}).json()
    assert r["answer"] == "Dazu habe ich keine passenden Dokumente gefunden."


def test_english_sum_note():
    answer = "- Jan: 10,00 € [Dok 1]\n- Feb: 20,00 € [Dok 2]\nTotal: 40,00 €"
    assert ask.check_sum(answer, "en").endswith("Total: 30,00 € (recalculated by the server)")


def test_english_question_words_are_no_search_terms(monkeypatch):
    monkeypatch.setattr(ask, "_chat", lambda *a, **k: '["Kfz-Versicherung"]')
    assert ask.keywords_for("When does my insurance expire?", "en") == ["Kfz-Versicherung", "insurance", "expire"]
