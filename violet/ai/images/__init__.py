"""Violet's multi-provider image system (phase 6).

Business code submits standard tasks to ImageService; it never talks to a
vendor API. ImageRouter picks a configured, verified model; a protocol adapter
speaks to it; results are validated and stored by MediaAssetStore; images are
sent only through the outbox. See config/image_providers.json.
"""
