# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""One-click POC on the box with the data: fetch and prepare recordings, run the pipeline.

``s3`` lists and downloads; ``prepare`` arranges and validates; ``stages`` are the five
pipeline steps; ``jobs`` runs them in a supervised background process; ``setup`` checks and
repairs the installation. Everything is off unless ``SENSEAI_RUNNER=1``.
"""
