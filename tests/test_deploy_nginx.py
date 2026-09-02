from pathlib import Path

from maxread.admin_server import AdminHandler


def test_nginx_post_allowlist_covers_public_web_mutations():
    config = (
        Path(__file__).parents[1]
        / "deploy"
        / "nginx"
        / "maxread-location.conf.example"
    ).read_text(encoding="utf-8")

    for route in (
        "submit",
        "binding-code",
        "retry",
        "pet/chat",
        "project-action",
        "organize",
        "categories",
    ):
        assert route in config

    source = Path(AdminHandler.do_POST.__code__.co_filename).read_text(encoding="utf-8")
    assert 'parsed.path == "/api/web/categories"' in source
