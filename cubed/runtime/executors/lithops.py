import copy
import logging
import time
from functools import partial
from typing import Callable, Iterable

import networkx as nx
from lithops.executors import FunctionExecutor
from lithops.wait import ALWAYS, ANY_COMPLETED
from rechunker.types import ParallelPipelines, PipelineExecutor
from six import reraise

from cubed.core.array import TaskEndEvent
from cubed.runtime.backup import should_launch_backup
from cubed.runtime.pipeline import already_computed
from cubed.runtime.types import DagExecutor
from cubed.utils import peak_memory

logger = logging.getLogger(__name__)

# Lithops represents delayed execution tasks as functions that require
# a FunctionExecutor.
Task = Callable[[FunctionExecutor], None]


class LithopsPipelineExecutor(PipelineExecutor[Task]):
    """An execution engine based on Lithops."""

    def pipelines_to_plan(self, pipelines: ParallelPipelines) -> Task:
        tasks = []
        for pipeline in pipelines:
            stage_tasks = []
            for stage in pipeline.stages:
                if stage.mappable is not None:
                    stage_func = build_stage_mappable_func(stage, pipeline.config)
                    stage_tasks.append(stage_func)
                else:
                    stage_func = build_stage_func(stage, pipeline.config)
                    stage_tasks.append(stage_func)

            # Stages for a single pipeline must be executed in series
            tasks.append(partial(_execute_in_series, stage_tasks))

        # TODO: execute tasks for different specs in parallel
        return partial(_execute_in_series, tasks)

    def execute_plan(self, plan: Task, **kwargs):
        with FunctionExecutor(**kwargs) as executor:
            plan(executor)


def map_unordered(
    lithops_function_executor,
    map_function,
    map_iterdata,
    include_modules=[],
    max_failures=3,
    use_backups=False,
    return_stats=False,
):
    """
    Apply a function to items of an input list, yielding results as they are completed
    (which may be different to the input order).

    A generalisation of Lithops `map`, with retries, and relaxed return ordering.

    :param lithops_function_executor: The Lithops function executor to use.
    :param map_function: The function to map over the data.
    :param map_iterdata: An iterable of input data.
    :param include_modules: Modules to include.
    :param max_failures: The number of task failures to allow before raising an exception.
    :param use_backups: Whether to launch backup tasks to mitigate against slow-running tasks.
    :param return_stats: Whether to return lithops stats.

    :return: Function values (and optionally stats) as they are completed, not necessarily in the input order.
    """
    failures = 0
    return_when = ALWAYS if use_backups else ANY_COMPLETED

    inputs = map_iterdata
    tasks = {}
    start_times = {}
    end_times = {}
    backups = {}
    pending = []

    futures = lithops_function_executor.map(
        map_function,
        inputs,
        include_modules=include_modules,
    )
    tasks.update({k: v for (k, v) in zip(futures, inputs)})
    start_times.update({k: time.monotonic() for k in futures})
    pending.extend(futures)

    while pending:
        finished, pending = lithops_function_executor.wait(
            pending, throw_except=False, return_when=return_when
        )

        failed = []
        for future in finished:
            if future.error:
                failures += 1
                if failures > max_failures:
                    # re-raise exception
                    # TODO: why does calling status not raise the exception?
                    future.status(throw_except=True)
                    reraise(*future._exception)
                failed.append(future)
            else:
                end_times[future] = time.monotonic()
                if return_stats:
                    # lithops doesn't return peak mem usage, so we have to measure it ourselves
                    # see https://pythonspeed.com/articles/estimating-memory-usage/#measuring-peak-memory-usage
                    result, peak_memory_start, peak_memory_end = future.result()
                    stats = future.stats.copy()
                    stats["peak_memory_start"] = peak_memory_start
                    stats["peak_memory_end"] = peak_memory_end
                    yield result, stats
                else:
                    yield future.result()

            if use_backups:
                # remove backups
                backup = backups.get(future, None)
                if backup:
                    if backup in pending:
                        pending.remove(backup)
                    del backups[future]
                    del backups[backup]

        if failed:
            # rerun and add to pending
            inputs = [v for (fut, v) in tasks.items() if fut in failed]
            # TODO: de-duplicate code from above
            futures = lithops_function_executor.map(
                map_function,
                inputs,
                include_modules=include_modules,
            )
            tasks.update({k: v for (k, v) in zip(futures, inputs)})
            start_times.update({k: time.monotonic() for k in futures})
            pending.extend(futures)

        if use_backups:
            now = time.monotonic()
            for future in copy.copy(pending):
                if future not in backups and should_launch_backup(
                    future, now, start_times, end_times
                ):
                    inputs = [v for (fut, v) in tasks.items() if fut == future]
                    logger.info("Running backup task for %s", inputs)
                    futures = lithops_function_executor.map(
                        map_function,
                        inputs,
                        include_modules=include_modules,
                    )
                    tasks.update({k: v for (k, v) in zip(futures, inputs)})
                    start_times.update({k: time.monotonic() for k in futures})
                    pending.extend(futures)
                    pending.remove(future)  # throw away slow one
                    backup = futures[0]  # TODO: launch multiple backups at once
                    backups[future] = backup
                    backups[backup] = future
            time.sleep(1)


def lithops_stats_to_task_end_event(name, stats):
    return TaskEndEvent(
        array_name=name,
        task_create_tstamp=stats["host_job_create_tstamp"],
        function_start_tstamp=stats["worker_func_start_tstamp"],
        function_end_tstamp=stats["worker_func_end_tstamp"],
        task_result_tstamp=stats["host_status_done_tstamp"],
        peak_memory_start=stats["peak_memory_start"],
        peak_memory_end=stats["peak_memory_end"],
    )


def build_stage_mappable_func(
    stage, config, name=None, callbacks=None, use_backups=False
):
    def sf(mappable):
        peak_memory_start = peak_memory()
        result = stage.function(mappable, config=config)
        peak_memory_end = peak_memory()
        return result, peak_memory_start, peak_memory_end

    def stage_func(lithops_function_executor):
        for _, stats in map_unordered(
            lithops_function_executor,
            sf,
            list(stage.mappable),
            include_modules=["cubed"],
            use_backups=use_backups,
            return_stats=True,
        ):
            if callbacks is not None:
                event = lithops_stats_to_task_end_event(name, stats)
                [callback.on_task_end(event) for callback in callbacks]

    return stage_func


def build_stage_func(stage, config):
    def sf():
        return stage.function(config=config)

    def stage_func(lithops_function_executor):
        futures = lithops_function_executor.call_async(sf, ())
        lithops_function_executor.get_result(futures)

    return stage_func


def _execute_in_series(
    tasks: Iterable[Task], lithops_function_executor: FunctionExecutor
) -> None:
    for task in tasks:
        task(lithops_function_executor)


class LithopsDagExecutor(DagExecutor):
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    # TODO: execute tasks for independent pipelines in parallel
    def execute_dag(self, dag, callbacks=None, **kwargs):
        merged_kwargs = {**self.kwargs, **kwargs}
        use_backups = merged_kwargs.pop("use_backups", False)
        with FunctionExecutor(**merged_kwargs) as executor:
            nodes = {n: d for (n, d) in dag.nodes(data=True)}
            for node in list(nx.topological_sort(dag)):
                if already_computed(nodes[node]):
                    continue
                pipeline = nodes[node]["pipeline"]

                for stage in pipeline.stages:
                    if stage.mappable is not None:
                        stage_func = build_stage_mappable_func(
                            stage,
                            pipeline.config,
                            name=node,
                            callbacks=callbacks,
                            use_backups=use_backups,
                        )
                    else:
                        stage_func = build_stage_func(stage, pipeline.config)

                    # execute each stage in series
                    stage_func(executor)
