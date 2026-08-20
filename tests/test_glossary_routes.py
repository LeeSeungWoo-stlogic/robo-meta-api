from pathlib import Path

from app.services.t2sql.llm import GENERATE_PROMPT

_REPO = Path(__file__).resolve().parents[1]


def test_find_glossary_routes_uses_approved_terms_not_short_aliases() -> None:
    source = (_REPO / "app/services/metadata_repository/_search.py").read_text(
        encoding="utf-8"
    )
    assert "kair_platform_terms" in source
    assert "kair_platform_active_glossary_head" in source
    assert "kair_platform_short_aliases" not in source
    assert "w.abbreviation" in source
    assert "w.english_name" in source
    assert "jsonb_array_elements_text(w.aliases)" in source
    assert "kair_platform_synonym_groups" in source
    assert "kair_platform_type_suffix_groups" not in source
    assert "kair_platform_short_aliases" not in source


def test_generate_prompt_uses_glossary_standard_term() -> None:
    assert "glossary_routes[].standard_term은 승인 용어집 표준명" in GENERATE_PROMPT
    assert "용어만으로 테이블을 창작하지 마라" in GENERATE_PROMPT


def test_engine_passes_glossary_routes() -> None:
    source = (_REPO / "app/services/t2sql/engine.py").read_text(encoding="utf-8")
    assert '"glossary_routes": [' in source
