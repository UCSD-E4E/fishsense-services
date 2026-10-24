"""Laser calibration's shared kernels: the outlier trim, the four gates, and
the fit that strings them together. Used by stage 13 (slate) and the
checkerboard producer; not a stage itself. The plane a target puts a dot on
is `fishsense_core.plane` (this package's `geometry` module until v0.52)."""
