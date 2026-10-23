"""Agent Lab branding, shared theme assets and versioned chat assets on the real UI server."""

import tomllib

import httpx
import pytest
from browser_protocol import LocalServer

pytestmark = pytest.mark.slow


async def test_agent_lab_branding_on_login_chat_and_parent_pages(server: LocalServer) -> None:
    """Installed wheels supply the same icon before sign-in and inside mounted chat."""
    async with httpx.AsyncClient(base_url=server.origin, trust_env=False) as anonymous:
        login = await anonymous.get("/login")
        assert "<title>Sign in locally · Agent Lab</title>" in login.text
        assert "href='/favicon'" in login.text
        assert "src='/ui/agent-lab.svg'" in login.text
        icon = await anonymous.get("/ui/agent-lab.svg")
        assert icon.status_code == 200
        assert icon.headers["content-type"].startswith("image/svg+xml")
        assert '<title id="title">Agent Lab</title>' in icon.text
        favicon = await anonymous.get("/favicon")
        assert favicon.status_code == 200
        assert favicon.content == icon.content
        from xml.etree import ElementTree

        artwork = ElementTree.fromstring(icon.content)  # noqa: S314 - bundled local SVG
        namespace = {"svg": "http://www.w3.org/2000/svg"}
        assert artwork.findall("svg:rect", namespace) == []
        assert len(artwork.findall("svg:g/svg:circle", namespace)) == 3
        assert "currentColor" in icon.text
        assert "prefers-color-scheme: dark" in icon.text
        assert "#777" in icon.text
        assert "#ccc" in icon.text
        theme = await anonymous.get("/ui/theme.css")
        assert '.dark img[src*="/ui/agent-lab.svg"]' in theme.text
        assert "brightness(0) invert(1)" in theme.text
        assert (
            await anonymous.get("/favicon", headers={"host": "evil.example"})
        ).status_code == 403
        # Public branding must not expose neighboring shell assets or private settings.
        for path in ("/ui/private.svg", "/chat/public/chat.css", "/chat/project/settings"):
            assert (await anonymous.get(path)).status_code == 401
    async with server.login() as client:
        chat = await client.get("/chat/")
        assert "<title>Agent Lab</title>" in chat.text
        assert 'href="/chat/favicon"' in chat.text
        settings = (await client.get("/chat/project/settings")).json()
        assert settings["ui"]["name"] == "Agent Lab"
        assert settings["ui"]["default_avatar_file_url"] == "/ui/agent-lab.svg"
        assert any(link["display_name"] == "Agent Lab" for link in settings["ui"]["header_links"])
        for path in ("/chat/favicon", "/chat/logo?theme=light", "/chat/logo?theme=dark"):
            image = await client.get(path)
            assert image.status_code == 200
            assert image.content == icon.content
        for path, title in (("/catalog", "Choose agent"), ("/conversations", "Conversations")):
            page = await client.get(path)
            assert f"<title>{title} · Agent Lab</title>" in page.text
            assert "src='/ui/agent-lab.svg'" in page.text


async def test_shared_theme_assets_and_navigation_are_available_on_all_pages(
    server: LocalServer,
) -> None:
    """Packaged styles also work on sign-in without exposing protected app assets."""
    async with httpx.AsyncClient(base_url=server.origin, trust_env=False) as anonymous:
        for path, media in (("/ui/theme.css", "text/css"), ("/ui/theme.js", "text/javascript")):
            asset = await anonymous.get(path)
            assert asset.status_code == 200
            assert asset.headers["content-type"].startswith(media)
            assert "unsafe-eval" not in asset.headers["content-security-policy"]
            assert (await anonymous.get(path, headers={"host": "evil.example"})).status_code == 403
        assert (await anonymous.get("/ui/private.css")).status_code == 401
    async with server.login() as client:
        for path in ("/login", "/catalog", "/logs", "/conversations"):
            response = await client.get(path)
            assert "href='/ui/theme.css'" in response.text
            assert "src='/ui/theme.js'" in response.text
            assert "aria-label='Theme'" in response.text
            assert "class='agent-lab-page'" in response.text
        assert "href='/catalog' aria-current='page'" in (await client.get("/catalog")).text
        assert "href='/logs' aria-current='page'" in (await client.get("/logs")).text
        css = await client.get("/chat/public/chat.css")
        assert '@import url("/ui/theme.css")' in css.text


async def test_versioned_chat_assets_are_served_after_shell_preparation(
    server: LocalServer,
) -> None:
    config_path = server.config.parent / "shell/.chainlit/config.toml"
    ui = tomllib.loads(config_path.read_text(encoding="utf-8"))["UI"]
    async with server.login() as client:
        for key, name in (("custom_css", "chat.css"), ("custom_js", "memos.js")):
            url = ui[key]
            assert url.startswith(f"/public/{name}?v=")
            response = await client.get("/chat" + url)
            assert response.status_code == 200
            assert response.content == (server.config.parent / "shell/public" / name).read_bytes()
