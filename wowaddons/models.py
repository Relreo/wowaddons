"""Shared TypedDict models."""

from __future__ import annotations

from typing import TypedDict


class ProjectData(TypedDict):
    """Metadata for a locally installed CurseForge addon project."""

    folders: list[str]
    title: str
    version: str


class UnmatchedAddon(TypedDict):
    """Metadata for a local addon folder without a CurseForge project ID."""

    folder: str
    title: str
    version: str
