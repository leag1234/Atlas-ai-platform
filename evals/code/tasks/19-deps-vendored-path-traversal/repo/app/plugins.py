import json
import os

import app  # noqa: F401
import packkit


def install_plugin(archive_path, plugins_dir):
    """Install a plugin archive into ``plugins_dir/<name>`` and return the plugin name.

    The archive contains ``plugin.json`` (with a ``name``) and the plugin files.
    """
    staging = os.path.join(plugins_dir, ".staging")
    packkit.extract_all(archive_path, staging)
    with open(os.path.join(staging, "plugin.json"), encoding="utf-8") as handle:
        name = json.load(handle)["name"]
    os.replace(staging, os.path.join(plugins_dir, name))
    return name
