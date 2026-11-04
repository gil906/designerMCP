import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from designer_mcp.library import Library


async def test_real_stdio_protocol(populated):
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "designer_mcp.server", "--root", str(populated)],
        cwd=str(Path(__file__).resolve().parents[1]),
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            tools = await client.list_tools()
            names = {t.name for t in tools.tools}
            assert names == {
                "designer_search", "designer_get_reference", "designer_get_asset",
                "designer_brief", "designer_audit", "designer_library_status",
                "designer_get_bundle",
            }
            assert all(t.annotations.readOnlyHint and not t.annotations.openWorldHint for t in tools.tools)
            search_tool = next(t for t in tools.tools if t.name == "designer_search")
            assert search_tool.inputSchema["properties"]["limit"]["maximum"] == 10
            audit_tool = next(t for t in tools.tools if t.name == "designer_audit")
            assert audit_tool.inputSchema["properties"]["description"]["maxLength"] == 500
            assert audit_tool.inputSchema["properties"]["platform"]["maxLength"] == 100
            audit = await client.call_tool("designer_audit", {"description": "This dashboard looks AI"})
            assert not audit.isError
            assert audit.structuredContent["checks"][0]["id"] == "layout"
            assert not audit.structuredContent["matched"]
            result = await client.call_tool("designer_search", {"query": "grid"})
            assert not result.isError
            assert result.structuredContent["results"][0]["id"] == "dashboard"
            detail = await client.call_tool("designer_get_reference", {"id": "dashboard"})
            assert detail.structuredContent["reference"]["title"] == "Responsive dashboard"
            image = await client.call_tool("designer_get_asset", {"reference_id": "dashboard", "asset_id": "preview"})
            assert not image.isError
            assert [c.type for c in image.content] == ["text", "image"]
            assert json.loads(image.content[0].text)["provenance"]["license"] == "MIT"
            animation = await client.call_tool("designer_get_asset", {"reference_id": "dashboard", "asset_id": "motion"})
            assert json.loads(animation.content[0].text)["animation_json"].startswith("{")
            brief = await client.call_tool("designer_brief", {"goal": "grid"})
            assert brief.structuredContent["references"][0]["citation"] == "designer://reference/dashboard"
            missing = await client.call_tool("designer_search", {"query": "nomatchesxyz"})
            assert not missing.structuredContent["matched"]
            for name, args in (
                ("designer_get_reference", {"id": "absent"}),
                ("designer_get_asset", {"reference_id": "dashboard", "asset_id": "vector"}),
                ("designer_get_asset", {"reference_id": "dashboard", "asset_id": "../secret"}),
                ("designer_search", {"query": "grid", "limit": 11}),
                ("designer_search", {"query": "grid", "limit": True}),
                ("designer_search", {"query": "grid", "limit": "5"}),
            ):
                failure = await client.call_tool(name, args)
                assert failure.isError
            resources = await client.list_resources()
            assert {str(r.uri) for r in resources.resources} >= {"designer://guide", "designer://catalog"}
            guide = await client.read_resource("designer://guide")
            assert "designer_get_asset" in guide.contents[0].text
            assert "designer_audit" in guide.contents[0].text
            assert "original starter layout" in guide.contents[0].text
            reference = await client.read_resource("designer://reference/dashboard")
            assert json.loads(reference.contents[0].text)["reference"]["id"] == "dashboard"
            prompts = await client.list_prompts()
            assert [p.name for p in prompts.prompts] == ["design_workflow"]
            prompt = await client.get_prompt("design_workflow", {"goal": "dashboard"})
            assert "Required workflow" in prompt.messages[0].content.text
            assert "designer_audit" in prompt.messages[0].content.text
            status = await client.call_tool("designer_library_status", {})
            assert status.structuredContent["entry_count"] == 1
            assert status.structuredContent["source_status_counts"]["blocked"] == 1


async def test_stdio_absent_data_status(root):
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "designer_mcp.server", "--root", str(root)],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            status = await client.call_tool("designer_library_status", {})
            assert not status.isError
            assert status.structuredContent["entry_count"] == 0
            assert status.structuredContent["library_state"] == "unavailable"
            search = await client.call_tool("designer_search", {"query": "design"})
            assert search.isError and "Library unavailable" in search.content[0].text


async def test_stdio_configured_root_from_other_cwd(populated):
    elsewhere = populated / "unrelated-project"
    elsewhere.mkdir()
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "designer_mcp.server"],
        cwd=str(elsewhere),
        env={**os.environ, "DESIGNER_MCP_ROOT": str(populated), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            status = await client.call_tool("designer_library_status", {})
            assert status.structuredContent["entry_count"] == 1
            search = await client.call_tool("designer_search", {"query": "dashboard"})
            assert search.structuredContent["results"][0]["id"] == "dashboard"


async def test_stdio_default_checkout_root_from_other_cwd(populated):
    project = Path(__file__).resolve().parents[1]
    expected = Library(project).status()
    elsewhere = populated / "unrelated-project"
    elsewhere.mkdir()
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    environment.pop("DESIGNER_MCP_ROOT", None)
    parameters = StdioServerParameters(
        command=str(project / ".venv/bin/designer-mcp"), args=[],
        cwd=str(elsewhere), env=environment,
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            status = await client.call_tool("designer_library_status", {})
            assert status.structuredContent["library_state"] == expected["library_state"]
            assert status.structuredContent["entry_count"] == expected["entry_count"]
            assert status.structuredContent["source_count"] == expected["source_count"]
            # The unrelated cwd fixture has one entry; it must never be selected.
            assert status.structuredContent["entry_count"] != 1


async def test_stdio_unslop_merge_and_style_routing(populated, entry):
    anti = {
        **entry, "id": "unslop-intentional", "title": "Unslop intentional visual design",
        "categories": ["anti-ai-slop"], "tags": ["anti-slop"],
    }
    (populated / "data/unslop-library.json").write_text(json.dumps({"version": 1, "entries": [anti]}))
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "designer_mcp.server", "--root", str(populated)],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            status = await client.call_tool("designer_library_status", {})
            assert status.structuredContent["entry_count"] == 2
            assert "data/unslop-library.json" in status.structuredContent["manifests"]
            search = await client.call_tool("designer_search", {"query": "looks AI"})
            assert [r["id"] for r in search.structuredContent["results"]] == ["unslop-intentional"]
            brief = await client.call_tool("designer_brief", {"goal": "dashboard", "style": "less AI-looking"})
            assert [r["id"] for r in brief.structuredContent["references"]] == ["unslop-intentional"]
            assert "anti_slop" in brief.structuredContent["sections"]
