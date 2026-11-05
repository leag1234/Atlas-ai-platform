import sys

from textkit.case import to_snake
from textkit.words import truncate_words, word_count


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 2:
        print("usage: textkit snake|count|summary TEXT...", file=sys.stderr)
        return 2
    command, text = argv[0], " ".join(argv[1:])
    if command == "snake":
        print(to_snake(text))
    elif command == "count":
        print(word_count(text))
    elif command == "summary":
        print(f"{word_count(text)} words: {truncate_words(text, 5)}")
    else:
        print(f"unknown command {command}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
