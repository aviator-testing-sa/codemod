# Debugging Celery commands
import collections
import gc
import math
import os
import sys
from typing import Any, DefaultDict

from celery.worker.control import inspect_command  # type: ignore[import-untyped]
from flask import Blueprint, jsonify

from main import celery
from schema import StrictBaseModel
from util.flaskutil import RouteResponse

debug_celery_blueprint: Blueprint = Blueprint("debug_celery", __name__)


class ObjectDump(StrictBaseModel):
    name: str
    total: int = 0
    sizes: DefaultDict[str, int] = collections.defaultdict(int)


class ProcessMemoryDump(StrictBaseModel):
    pid: int
    objects: list[ObjectDump]


@inspect_command()
def memory_dump(state: Any) -> dict[Any, Any]:
    objects: dict[str, ObjectDump] = dict()
    for obj in gc.get_objects():
        if not hasattr(obj, "__class__"):
            continue
        name = str(obj.__class__)
        sz = sys.getsizeof(obj, 0)
        sz_str = _bucketize_object_size(sz)
        if sz == 0:
            continue
        d = objects.get(name, None)
        if not d:
            d = ObjectDump(name=name)
            objects[name] = d
        d.total += sz
        d.sizes[sz_str] += 1
    objs = list(sorted(objects.values(), key=lambda od: od.total, reverse=True))
    return ProcessMemoryDump(pid=os.getpid(), objects=objs).tojs_dict()


def _bucketize_object_size(sz: int) -> str:
    if sz < 1024:
        return "<1kb"
    if sz < 1024 * 1024:
        return "<1mb"
    if sz < 16 * 1024 * 1024:
        return "<16mb"
    return "more"


@debug_celery_blueprint.route("/debug/celery/memory_dump")
def _memory_dump() -> RouteResponse:
    inspect = celery.control.inspect()  # type: ignore[attr-defined]
    replies = inspect._request("memory_dump")
    return jsonify(replies)
