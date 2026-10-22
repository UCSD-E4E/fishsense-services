"""The mask backfill (2026-10-22): SAM masks for predictions made before
contract 12 stored them, regenerated on the GPU and named against their
predictions only where they reproduce them. Run by hand (`run`), dry run
first; it has no schedule."""
