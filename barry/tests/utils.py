from typing import Iterable

import numpy as np
import zarr
from rechunker.executors.python import PythonPipelineExecutor


def create_zarr(a, /, store, *, dtype=None, chunks=None):
    # from dask.asarray
    if not isinstance(getattr(a, "shape", None), Iterable):
        # ensure blocks are arrays
        a = np.asarray(a, dtype=dtype)
    if dtype is None:
        dtype = a.dtype

    # write to zarr
    za = zarr.open(store, mode="w", shape=a.shape, dtype=dtype, chunks=chunks)
    za[:] = a
    return za


def execute_pipeline(pipeline, executor=None):
    """Executes a pipeline"""
    if executor is None:
        executor = PythonPipelineExecutor()
    plan = executor.pipelines_to_plan([pipeline])
    executor.execute_plan(plan)
