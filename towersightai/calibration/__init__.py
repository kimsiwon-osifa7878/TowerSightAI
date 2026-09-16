"""Camera calibration helpers (checkerboard generation, intrinsics measurement).

Everything here is tooling for the operator console. Producing or loading a calibration never
changes safety state on its own; missing, invalid, or unreviewed calibration keeps final OK blocked.
"""
