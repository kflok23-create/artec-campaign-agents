"""Output of the asset tagger: what one image shows, for search and child-safety checks."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Issue = Literal[
    "blurry", "dark", "low_resolution", "screenshot", "watermark", "cluttered", "cropped_subject"
]


class AssetVision(BaseModel):
    description: str = Field(min_length=3, max_length=240)
    tags: list[str] = Field(min_length=3, max_length=15)
    people: Literal["none", "adults", "children", "adults_and_children"]
    faces_identifiable: bool
    text_in_image: bool
    issues: list[Issue] = Field(default_factory=list, max_length=7)

    @field_validator("tags", mode="after")
    @classmethod
    def _normalise_tags(cls, tags: list[str]) -> list[str]:
        seen: list[str] = []
        for tag in tags:
            clean = " ".join(tag.lower().split())[:40]
            if clean and clean not in seen:
                seen.append(clean)
        if len(seen) < 3:
            raise ValueError("at least 3 distinct tags are needed")
        return seen
