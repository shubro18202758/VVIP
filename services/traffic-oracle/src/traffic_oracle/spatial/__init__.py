"""Spatial reads over corridor.road_segments with how each line was drawn (C-K).

``traffic_oracle.spatial.router.router`` is mounted by
``traffic_oracle.__main__`` through a guarded include placed ahead of the
inline Spatial handlers (contract C-S)::

    from traffic_oracle.spatial.router import router as spatial_router

(Nothing is re-exported here: a package attribute named ``router`` would
shadow the submodule of the same name.)
"""
