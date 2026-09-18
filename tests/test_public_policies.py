from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "src"


def test_public_policy_routes_are_registered():
    app = (FRONTEND / "App.tsx").read_text(encoding="utf-8")
    for route in ("/privacy", "/terms", "/refunds", "/support"):
        assert f'path="{route}"' in app


def test_public_policies_cover_product_specific_data_flows():
    policies = (FRONTEND / "routes" / "Legal" / "index.tsx").read_text(encoding="utf-8")
    for expected in (
        "support@explainora.dev",
        "Supabase",
        "Oracle Cloud",
        "Cloudflare",
        "Groq",
        "Cerebras",
        "NVIDIA",
        "OpenRouter",
        "private Storage objects",
        "365 days",
        "National Consumer Helpline",
    ):
        assert expected in policies


def test_footers_do_not_contain_known_placeholder_links():
    paths = (
        FRONTEND / "routes" / "Landing" / "Footer" / "Footer.tsx",
        FRONTEND / "routes" / "Home" / "components" / "Footer.tsx",
    )
    forbidden = ("#linkedin", "#youtube", "#contact", "#privacy", "#careers", "#docs", "#changelog")
    for path in paths:
        source = path.read_text(encoding="utf-8")
        for placeholder in forbidden:
            assert placeholder not in source


def test_legal_review_is_an_explicit_release_gate():
    review = (ROOT / "LEGAL_REVIEW.md").read_text(encoding="utf-8")
    assert "Pending professional review" in review
    assert "support@explainora.dev" in review
    assert "before paid or general-public release" in review.lower()
