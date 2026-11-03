"""Définitions d'outils envoyées au modèle : budget de tokens, outils liés à la demande, descriptions raccourcies."""

import json

from gateway.tools import Tool, ToolResult
from gateway.tools.budget import compact_spec, select_specs, tokens


async def _run(args):
    return ToolResult("ok")


def mcp_tool(server, name, description="x" * 900, props=12):
    schema = {
        "type": "object",
        "properties": {f"p{i}": {"type": "string", "description": "y" * 400, "examples": ["z"]} for i in range(props)},
    }
    return Tool(f"mcp_{server}_{name}", description, schema, _run, level="mcp")


def internal_tool(name):
    return Tool(name, "Outil interne.", {"type": "object", "properties": {}}, _run, level="basic")


def test_internal_tools_are_always_sent_and_the_budget_is_respected():
    tools = [internal_tool("calcul"), internal_tool("date")] + [mcp_tool("github", f"tool{i}") for i in range(73)]
    specs = select_specs(tools, "rien à voir", budget=3000)
    names = [s["function"]["name"] for s in specs]
    assert "calcul" in names and "date" in names
    assert tokens(specs) <= 3000 + 1  # le budget tient, aux outils internes près (toujours envoyés)
    assert len([n for n in names if n.startswith("mcp_")]) < 73  # 73 outils ne tiennent pas dans le budget


def test_tools_related_to_the_request_come_first():
    tools = [mcp_tool("github", f"tool{i}") for i in range(40)] + [
        mcp_tool("heure", "get_current_time", "Donne l'heure.")
    ]
    names = [s["function"]["name"] for s in select_specs(tools, "Quelle heure est-il à Tokyo ?", budget=600)]
    assert names[0] == "mcp_heure_get_current_time"  # le nom du serveur « heure » correspond à la demande


def test_descriptions_and_property_descriptions_are_shortened():
    spec = compact_spec(mcp_tool("brave", "search"))
    fn = spec["function"]
    assert len(fn["description"]) <= 200
    prop = fn["parameters"]["properties"]["p0"]
    assert len(prop["description"]) <= 120 and "examples" not in prop


def test_without_a_matching_request_the_first_tools_are_kept_in_order():
    tools = [mcp_tool("git", f"t{i}", "court", props=0) for i in range(5)]
    names = [s["function"]["name"] for s in select_specs(tools, "", budget=100000)]
    assert names == [t.name for t in tools]
    assert json.dumps(names)  # sérialisable
