import json
import os
from dataclasses import asdict, dataclass

PRIORITIES = ("low", "normal", "high")


@dataclass
class Task:
    id: int
    title: str
    priority: str = "normal"
    tags: tuple = ()
    done: bool = False


class Store:
    def __init__(self, path):
        self.path = path

    def load(self):
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as handle:
            return [Task(**{**item, "tags": tuple(item["tags"])}) for item in json.load(handle)]

    def save(self, tasks):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump([asdict(t) for t in tasks], handle, indent=2)

    def add(self, title, priority="normal", tags=()):
        tasks = self.load()
        task = Task(max((t.id for t in tasks), default=0) + 1, title, priority, tuple(tags))
        tasks.append(task)
        self.save(tasks)
        return task

    def complete(self, task_id):
        tasks = self.load()
        for task in tasks:
            if task.id == task_id:
                task.done = True
                self.save(tasks)
                return task
        raise KeyError(task_id)


def sort_key(task):
    return (task.done, -PRIORITIES.index(task.priority), task.id)
