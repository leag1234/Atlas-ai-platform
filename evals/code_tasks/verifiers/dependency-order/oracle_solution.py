def order(graph):
    result, seen = [], set()

    def visit(name):
        if name in seen:
            return
        seen.add(name)
        for dependency in graph[name]:
            visit(dependency)
        result.append(name)

    for name in graph:
        visit(name)
    return result
