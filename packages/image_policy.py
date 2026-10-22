"""Administrator policy is loaded once before image requests can be accepted."""

import json
import os
from pathlib import Path
import re
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

SafetyLevel = Literal["standard", "strict"]
ContentLevel = Literal["standard", "extended"]


class ImagePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    image_safety_level: SafetyLevel = "standard"
    content_level: ContentLevel = "standard"
    adult_audience_attested: bool = False
    extended_request_patterns: dict[str, list[str]] = Field(
        default_factory=dict, max_length=12
    )
    extended_instruction: str = Field(default="", max_length=16000)
    extended_profiles: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("content_level", mode="before")
    @classmethod
    def legacy_content_level(cls, value: object) -> object:
        return "extended" if value == "adult" else value

    @field_validator("extended_request_patterns")
    @classmethod
    def valid_extended_patterns(
        cls, value: dict[str, list[str]]
    ) -> dict[str, list[str]]:
        for language, patterns in value.items():
            if not language or len(language) > 16 or not patterns or len(patterns) > 32:
                raise ValueError("invalid_extended_request_patterns")
            for pattern in patterns:
                if not pattern or len(pattern) > 512:
                    raise ValueError("invalid_extended_request_patterns")
                try:
                    re.compile(pattern)
                except re.error as error:
                    raise ValueError("invalid_extended_request_patterns") from error
        return value

    @field_validator("extended_instruction")
    @classmethod
    def valid_extended_instruction(cls, value: str) -> str:
        if value and not value.strip():
            raise ValueError("invalid_extended_instruction")
        return value

    @field_validator("extended_profiles")
    @classmethod
    def valid_extended_profiles(cls, value: list[str]) -> list[str]:
        if any(not profile.strip() or len(profile) > 128 for profile in value):
            raise ValueError("invalid_extended_profiles")
        return value

    @model_validator(mode="after")
    def coherent(self) -> "ImagePolicy":
        if self.content_level == "extended" and not self.adult_audience_attested:
            raise ValueError("extended_without_attestation")
        if self.content_level == "extended" and self.image_safety_level == "strict":
            raise ValueError("extended_with_strict_images")
        return self


def tighten(
    system: ImagePolicy, organization: ImagePolicy | None = None
) -> ImagePolicy:
    # Future organization policy may only tighten the system level, never loosen it.
    strict = system.image_safety_level == "strict" or (
        organization is not None and organization.image_safety_level == "strict"
    )
    extended = (
        not strict
        and system.content_level == "extended"
        and (organization is None or organization.content_level == "extended")
    )
    return ImagePolicy(
        image_safety_level="strict" if strict else "standard",
        content_level="extended" if extended else "standard",
        adult_audience_attested=extended,
        extended_request_patterns=(
            system.extended_request_patterns if extended else {}
        ),
        extended_instruction=system.extended_instruction if extended else "",
        extended_profiles=system.extended_profiles if extended else [],
    )


def load_policy(path: Path | None = None) -> ImagePolicy:
    source = path or Path(
        os.environ.get(
            "ATLAS_POLICY_FILE",
            str(Path(__file__).resolve().parents[1] / "config/atlas-policy.json"),
        )
    )
    try:
        return ImagePolicy.model_validate(json.loads(source.read_text()))
    except (OSError, ValueError, ValidationError):
        # Never echo invalid configuration values into the startup log.
        raise RuntimeError(
            "Invalid image policy: expected a JSON object with only "
            "image_safety_level ('standard' or 'strict'), content_level ('standard' "
            "or 'extended'), and valid extended settings; file must be readable"
        ) from None


POLICY = tighten(load_policy())
