"""Reproducible synthetic benchmark: run each measurement in a fresh process."""

import argparse
import json
import resource
import time
from pathlib import Path

from PIL import Image

from packages.doc_render import Content, Section, Table, render


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--sections", type=int, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    root = args.output.parent
    fixture = Content.model_validate_json(
        (Path(__file__).parent / "fixtures/doc_render.json").read_text()
    )
    sample = fixture.sections[0]
    Image.new("RGB", (100, 50), (20, 90, 160)).save(root / "figure.png")
    sections = []
    for index in range(args.sections):
        sections.append(
            Section(
                heading=f"{sample.heading} {index + 1}",
                paragraphs=sample.paragraphs,
                bullets=sample.bullets if index % 3 == 2 else (),
                tables=(
                    Table(headers=sample.tables[0].headers, rows=sample.tables[0].rows),
                )
                if index % 3 == 1
                else (),
                images=("figure.png",) if index % 3 == 0 else (),
            )
        )
    content = Content(title=fixture.title, sections=tuple(sections))
    start = time.perf_counter()
    verified = render(content, args.output, image_root=root)
    print(
        json.dumps(
            {
                "format": args.output.suffix,
                "sections": args.sections,
                "elapsed_ms": round((time.perf_counter() - start) * 1000, 3),
                "maxrss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "bytes": args.output.stat().st_size,
                "units": verified.count,
                "unit": verified.unit,
                "pagination_verified": verified.pagination_verified,
                "all_units_have_text": all(t.strip() for t in verified.text),
            }
        )
    )


if __name__ == "__main__":
    main()
