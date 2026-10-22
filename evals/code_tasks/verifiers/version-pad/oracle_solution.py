def newer(left, right):
    a = [int(part) for part in left.split(".")]
    b = [int(part) for part in right.split(".")]
    width = max(len(a), len(b))
    return tuple(a + [0] * (width - len(a))) > tuple(b + [0] * (width - len(b)))
