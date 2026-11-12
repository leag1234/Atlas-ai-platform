"""Task H structured content contract (REQ-ENG-004, REQ-ENG-007)."""

from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Text = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4000)]
# A table cell may be empty: a template to fill in, a month not started yet.
# It stays empty in every format (a truly empty spreadsheet cell); no
# placeholder character is invented for it.
Cell = Annotated[str, StringConstraints(strict=True, max_length=4000)]


class ContentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Table(ContentModel):
    formulas: bool = Field(default=False, strict=True)
    headers: tuple[Cell, ...] = Field(min_length=1, max_length=8)
    rows: tuple[tuple[Cell, ...], ...] = Field(min_length=1, max_length=50)

    @field_validator("headers", mode="after")
    @classmethod
    def blank_headers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        # A cell of spaces only is an empty cell.
        return tuple(cell if cell.strip() else "" for cell in value)

    @field_validator("rows", mode="after")
    @classmethod
    def blank_cells(
        cls, value: tuple[tuple[str, ...], ...]
    ) -> tuple[tuple[str, ...], ...]:
        return tuple(
            tuple(cell if cell.strip() else "" for cell in row) for row in value
        )

    @model_validator(mode="after")
    def rectangular(self) -> Self:
        if any(len(row) != len(self.headers) for row in self.rows):
            raise ValueError("table_row_width")
        if not any(self.headers):
            # Rows may be blank lines to fill in; the columns still need names.
            raise ValueError("table_headers_required")
        return self


class Section(ContentModel):
    heading: Text
    paragraphs: tuple[Text, ...] = Field(default=(), max_length=50)
    bullets: tuple[Text, ...] = Field(default=(), max_length=50)
    tables: tuple[Table, ...] = Field(default=(), max_length=10)
    images: tuple[Text, ...] = Field(default=(), max_length=5)

    @model_validator(mode="after")
    def substantive(self) -> Self:
        if not (self.paragraphs or self.bullets or self.tables or self.images):
            raise ValueError("section_body_required")
        return self


class Content(ContentModel):
    title: Text
    sections: tuple[Section, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def printable(self) -> Self:
        for text in self.texts():
            if not text.strip() or any(ord(c) < 32 for c in text):
                raise ValueError("blank_or_control_text")
        if sum(map(len, self.texts())) > 200_000:
            raise ValueError("content_size_limit")
        return self

    def texts(self) -> tuple[str, ...]:
        """Every written text; empty table cells hold none."""
        result = [self.title]
        for section in self.sections:
            result.extend((section.heading, *section.paragraphs, *section.bullets))
            for table in section.tables:
                result.extend(cell for cell in table.headers if cell)
                for row in table.rows:
                    result.extend(cell for cell in row if cell)
        return tuple(result)


class PresentationSlide(ContentModel):
    """The bounded model-facing contract for a single presentation slide."""

    title: Text
    bullets: tuple[Text, ...] = Field(min_length=1, max_length=8)
    notes: Text | None = None
    table: Table | None = None


class PresentationOutline(ContentModel):
    """Trusted code maps this concise outline to the generic render contract."""

    title: Text
    slides: tuple[PresentationSlide, ...] = Field(min_length=1, max_length=30)

    def content(self) -> Content:
        return Content(
            title=self.title,
            sections=tuple(
                Section(
                    heading=slide.title,
                    paragraphs=(slide.notes,) if slide.notes else (),
                    bullets=slide.bullets,
                    tables=(slide.table,) if slide.table else (),
                )
                for slide in self.slides
            ),
        )
