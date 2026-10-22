"""Expose the answer string progressively, never the internal consolidation fields."""

from packages.limits import LimitError

import json

from services.orchestrator.stream_client import Sink


class AnswerStream:
    def __init__(self, sink: Sink) -> None:
        self.sink = sink
        self.buffer = ""
        self.shown = ""

    def answer(self) -> str:
        data = self.buffer.lstrip()
        if not data.startswith("{"):
            return ""
        decoder = json.JSONDecoder()
        position = 1
        while position < len(data):
            while position < len(data) and data[position] in " \r\n\t,":
                position += 1
            try:
                key, position = decoder.raw_decode(data, position)
                while position < len(data) and data[position].isspace():
                    position += 1
                if position == len(data) or data[position] != ":":
                    return ""
                position += 1
                while position < len(data) and data[position].isspace():
                    position += 1
                if key != "answer":
                    _, position = decoder.raw_decode(data, position)
                    continue
                if position == len(data) or data[position] != '"':
                    return ""
                try:
                    value, _ = decoder.raw_decode(data, position)
                except json.JSONDecodeError:
                    tail = data[position + 1 :]
                    # Retain incomplete escapes and surrogate pairs until complete.
                    for end in range(len(tail), max(-1, len(tail) - 12), -1):
                        try:
                            value = json.loads('"' + tail[:end] + '"')
                            value.encode("utf-8")
                            break
                        except (ValueError, UnicodeEncodeError):
                            continue
                    else:
                        return ""
                return str(value)
            except json.JSONDecodeError:
                return ""
        return ""

    async def __call__(self, event: dict[str, object]) -> None:
        if "phase" in event:
            if event["phase"] == "generating":
                self.buffer = self.shown = ""
            await self.sink(event)
            return
        delta = event.get("delta")
        if not isinstance(delta, dict):
            raise ValueError("invalid_delta")
        if "reasoning_content" in delta:
            await self.sink(
                {"delta": {"reasoning_content": delta["reasoning_content"]}}
            )
        if "content" in delta:
            self.buffer += str(delta["content"])
            if len(self.buffer) > 32000:
                raise LimitError("stream_limit", len(self.buffer), 32000, "characters")
            answer = self.answer()
            if answer:
                if not answer.startswith(self.shown):
                    raise ValueError("invalid_project_stream")
                text = answer[len(self.shown) :]
                self.shown = answer
                if text:
                    await self.sink({"delta": {"content": text}})
