"""Vision tagging for the asset indexer: one Haiku look at one image, behind the budget guard."""

from __future__ import annotations

from pathlib import Path

from agents.asset_tagger.schema import AssetVision
from agents.asset_tagger.tools import TOOLS
from agents.runner import AgentSpec
from services.config import BrandConfig

NAME = "asset_tagger"
PROMPT = (Path(__file__).parent / "prompt.md").read_text(encoding="utf-8")
CEILING_USD = 0.02  # one image is about 1,500 input tokens; Haiku costs a fraction of a cent


def spec(brand: BrandConfig) -> AgentSpec[AssetVision]:
    return AgentSpec(
        name=NAME,
        model=brand.models[NAME],
        system_prompt=PROMPT,
        output_model=AssetVision,
        tools=TOOLS,
        max_turns=3,
        ceiling_usd=CEILING_USD,
    )


def user_prompt(folder_path: str, file_name: str) -> str:
    # Angle brackets in names could fake the closing tag, so they are replaced.
    safe_path = folder_path.replace("<", "(").replace(">", ")")
    safe_name = file_name.replace("<", "(").replace(">", ")")
    return (
        "Catalogue this image.\n"
        f"<file_context>\nfolder: {safe_path or '(top level)'}\nfile: {safe_name}\n"
        "</file_context>"
    )
