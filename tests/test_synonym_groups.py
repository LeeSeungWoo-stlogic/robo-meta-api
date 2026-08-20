from app.services.decision_postgres.suffix_store import merge_type_groups
from app.services.meaning_slots import synonym_members_for_needles
from app.services.decision_postgres.aliases import TYPE_GROUPS, peel_type_suffix


def test_synonym_members_stay_inside_matched_group() -> None:
    extras = synonym_members_for_needles(
        [
            {
                "kind": "term",
                "preferred_form": "탁도",
                "members": ["탁도", "turbidity", "NTU"],
            },
            {
                "kind": "term",
                "preferred_form": "권역",
                "members": ["권역", "유역"],
            },
        ],
        ["탁도"],
    )
    assert "탁도" in extras
    assert "turbidity" in extras
    assert "권역" not in extras
    assert "유역" not in extras


def test_synonym_members_ignore_type_suffix_groups() -> None:
    extras = synonym_members_for_needles(
        [
            {
                "kind": "type_suffix",
                "preferred_form": "권역",
                "members": ["권역", "유역본부"],
            }
        ],
        ["권역"],
    )
    assert extras == []


def test_empty_store_suffix_groups_match_constants() -> None:
    groups = merge_type_groups([])
    assert [item.name for item in groups] == [item.name for item in TYPE_GROUPS]
    peeled = peel_type_suffix("금강권역", groups)
    assert peeled is not None
    assert peeled[0] == "금강"
