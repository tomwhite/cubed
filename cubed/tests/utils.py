from typing import Iterable

import numpy as np
import zarr
from rechunker.executors.python import PythonPipelineExecutor

from cubed.runtime.executors.python import PythonDagExecutor

LITHOPS_LOCAL_CONFIG = {"lithops": {"backend": "localhost", "storage": "localhost"}}

ALL_EXECUTORS = [PythonPipelineExecutor(), PythonDagExecutor()]

try:
    from cubed.runtime.executors.beam import BeamDagExecutor

    ALL_EXECUTORS.append(BeamDagExecutor())
except ImportError:
    pass

try:
    from cubed.runtime.executors.lithops import LithopsDagExecutor

    ALL_EXECUTORS.append(LithopsDagExecutor(config=LITHOPS_LOCAL_CONFIG))
except ImportError:
    pass

MODAL_EXECUTORS = []

try:
    from cubed.runtime.executors.modal import AsyncModalDagExecutor, ModalDagExecutor

    MODAL_EXECUTORS.append(AsyncModalDagExecutor())
    MODAL_EXECUTORS.append(ModalDagExecutor())
except ImportError:
    pass


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


def execute_pipeline(pipeline, executor):
    """Executes a pipeline"""
    plan = executor.pipelines_to_plan([pipeline])
    executor.execute_plan(plan)
