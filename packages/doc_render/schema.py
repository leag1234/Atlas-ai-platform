"""Task H structured content contract (REQ-ENG-004, REQ-ENG-007)."""

from typing import Annotated, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .cells import SHEET_COLUMNS, Reading, plain, read_table


def font_spaces(value: str) -> str:
    # The embedded font has no narrow or thin space: French typography
    # ("1 250,00 €" written with one) keeps a no-break space instead of being
    # refused as an unsupported character.
    return value.replace("\u202f", "\u00a0").replace("\u2009", "\u00a0")


def tidy(cell: str) -> str:
    # A cell of spaces only is an empty cell. Emphasis or backticks around a
    # formula are dropped: "**=SUM(B2:B5)**" is the formula, not a text.
    if not cell.strip():
        return ""
    shown = plain(cell)
    return shown if shown.startswith("=") else cell


Text = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=4000),
    AfterValidator(font_spaces),
]
# A table cell may be empty: a template to fill in, a month not started yet.
# It stays empty in every format (a truly empty spreadsheet cell); no
# placeholder character is invented for it.
Cell = Annotated[
    str, StringConstraints(strict=True, max_length=4000), AfterValidator(font_spaces)
]


class ContentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Table(ContentModel):
    formulas: bool = Field(default=False, strict=True)
    # A workbook takes wide tables (twelve months and more); the formats laid
    # out on a page have their own lower bound, checked when rendering.
    headers: tuple[Cell, ...] = Field(min_length=1, max_length=SHEET_COLUMNS)
    rows: tuple[tuple[Cell, ...], ...] = Field(min_length=1, max_length=50)

    @field_validator("headers", mode="after")
    @classmethod
    def blank_headers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(tidy(cell) for cell in value)

    @field_validator("rows", mode="after")
    @classmethod
    def blank_cells(
        cls, value: tuple[tuple[str, ...], ...]
    ) -> tuple[tuple[str, ...], ...]:
        return tuple(tuple(tidy(cell) for cell in row) for row in value)

    @model_validator(mode="after")
    def rectangular(self) -> Self:
        if any(len(row) != len(self.headers) for row in self.rows):
            raise ValueError("table_row_width")
        if not any(self.headers):
            # Rows may be blank lines to fill in; the columns still need names.
            raise ValueError("table_headers_required")
        return self

    def read(self) -> tuple[tuple[Reading, ...], ...]:
        """The header row, then the rows: inline markers and numbers read."""
        return read_table(self.headers, self.rows)


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

    def texts(self, *, sheet: bool = False) -> tuple[str, ...]:
        """Every written text as displayed; empty table cells hold none.

        Inline markers of table cells are not part of the text. With sheet,
        a cell a workbook stores as a number gives that number.
        """
        result = [self.title]
        for section in self.sections:
            result.extend((section.heading, *section.paragraphs, *section.bullets))
            for table in section.tables:
                for row in table.read():
                    result.extend(
                        cell.stored if sheet else cell.text for cell in row if cell.text
                    )
        return tuple(result)

    def repeats_title(self) -> bool:
        """Whether the first heading says the title again.

        A Markdown text that opens with its title gives both: its first
        section is headed by that title. Each format then writes the text
        once (a workbook showed it twice under its table, staging 2026-11-12).
        """
        return "".join(self.title.split()) == "".join(self.sections[0].heading.split())


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
