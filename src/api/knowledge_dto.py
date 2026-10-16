from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .dto import StrictRequestModel


class KnowledgeLibraryCreate(StrictRequestModel):
    name: str = Field(min_length=1)
    description: str = ""


class KnowledgeLibraryPatch(KnowledgeLibraryCreate):
    revision: int = Field(ge=1)


class KnowledgeMetadata(StrictRequestModel):
    title: str = ""
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    keywords: list[str] = Field(default_factory=list)
    summary: str = ""


class KnowledgeMetadataPatch(StrictRequestModel):
    metadata: KnowledgeMetadata
    revision: int = Field(ge=1)


class KnowledgeConfirm(StrictRequestModel):
    revision: int = Field(ge=1)


class KnowledgeConfirmItem(KnowledgeConfirm):
    import_id: str = Field(min_length=1)


class KnowledgeBatchConfirm(StrictRequestModel):
    imports: list[KnowledgeConfirmItem] = Field(min_length=1)


class KnowledgeGrantCreate(StrictRequestModel):
    subject_type: Literal["user"] = "user"
    subject_id: str = Field(min_length=1)
    role: Literal["reader"] = "reader"


class KnowledgeSelectionSave(StrictRequestModel):
    mode: Literal["auto", "selected"]
    library_ids: list[str] = Field(default_factory=list)
    revision: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_mode(self) -> KnowledgeSelectionSave:
        if self.mode == "selected" and not self.library_ids:
            raise ValueError("selected mode requires at least one library")
        if self.mode == "auto" and self.library_ids:
            raise ValueError("auto mode must not specify libraries")
        if any(not value.strip() for value in self.library_ids):
            raise ValueError("library IDs must not be empty")
        if len(set(self.library_ids)) != len(self.library_ids):
            raise ValueError("library IDs must be unique")
        return self
