# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""The contestants (bench "arms"): contract names, plain labels, and what each needs.

The names are the live-POC contract with senseai-bench; the labels are what a business
reader sees. Unknown arm names still display, under their own name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Kind = Literal["reflex", "detector"]
Need = Literal["nothing", "checkpoint", "torchvision", "ultralytics"]


@dataclass(frozen=True)
class Arm:
    """One contestant."""

    name: str
    label: str
    kind: Kind
    needs: Need
    blurb: str


ARMS: tuple[Arm, ...] = (
    Arm(
        "panther_prior",
        "Panther (no training)",
        "reflex",
        "nothing",
        "Panther's built-in knowledge of how cats see at night. Needs no trained model.",
    ),
    Arm(
        "panther_vision",
        "Panther",
        "reflex",
        "checkpoint",
        "Panther's night vision with the trained SenseAI world model.",
    ),
    Arm(
        "panther_fused",
        "Panther prior + learned",
        "reflex",
        "checkpoint",
        "Panther's built-in knowledge combined with the trained world model.",
    ),
    Arm(
        "panther",
        "Panther (full profile)",
        "reflex",
        "checkpoint",
        "The complete Panther profile, including its more cautious reflex.",
    ),
    Arm(
        "null_profile",
        "SenseAI without a profile",
        "reflex",
        "checkpoint",
        "The same SenseAI pipeline with no animal profile: the control.",
    ),
    Arm(
        "torchvision",
        "Standard camera AI (torchvision)",
        "detector",
        "torchvision",
        "An off-the-shelf object detector (Faster R-CNN) on the normal camera.",
    ),
    Arm(
        "yolo",
        "YOLO",
        "detector",
        "ultralytics",
        "A popular off-the-shelf detector (YOLOv8) on the normal camera. Benchmark only.",
    ),
)
BY_NAME = {a.name: a for a in ARMS}
PANTHER_PREFERENCE = ("panther_vision", "panther_fused", "panther", "panther_prior")
BASELINE_PREFERENCE = ("torchvision", "yolo", "null_profile")


def label(name: str) -> str:
    """Plain label of an arm, or its name if unknown."""
    a = BY_NAME.get(name)
    return a.label if a else name


def default_pair(names: list[str]) -> tuple[str, str] | None:
    """Left = the Panther arm present, right = the preferred baseline present."""
    left = next((n for n in PANTHER_PREFERENCE if n in names), None)
    right = next((n for n in BASELINE_PREFERENCE if n in names and n != left), None)
    if left is None or right is None:
        return None
    return left, right
