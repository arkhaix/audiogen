"""The fields of a job record, as jobs.json holds it and /api/state shows it.

jobs.py and nomad.py write to one record, a plain dict. Each field is declared here once, with what it means, so
two modules don't use one name for two things. A new field is declared here before code reads or writes it.
RecordFieldsTest reads jobs.py, nomad.py and server.py and fails for a field that is not declared, where the code
uses it on a variable of one of the names the test knows a record by. Nothing checks a record against these at
run time. Standard library only.
"""
from __future__ import annotations

from typing import TypedDict

# one dispatch of a job: the class of node it runs on, the CLI arguments of its step, and what the page calls it
Stage = TypedDict("Stage", {"class": str, "args": list, "label": str}, total=False)


class GaveWay(TypedDict):
    """The attempt of a job that was stopped and queued again, kept for a repeated upload of its result."""
    key_sha: str        # the SHA-256 of the key that attempt's worker has
    report: dict | None  # the report its result got


class NomadPart(TypedDict, total=False):
    """What follows a job on the cluster, from the pass that starts its first stage. A job that is queued again
    before its first stage ran loses it."""
    key: str                # what a worker's requests for the job have to name. New each time the job starts
    stage: int              # the index of the stage that runs or runs next
    stages: list[Stage]
    dispatching: bool       # a pass started the stage, and Nomad has not taken its dispatch yet
    retry_at: float         # when a dispatch that failed is tried again, in epoch seconds
    dispatched: str | None  # the Nomad job that runs the stage: its own, or the one of the worker it was handed to
    dispatched_at: str      # when Nomad took the dispatch or the job was handed, as ISO time
    follows: str | None     # the job whose worker was handed this one with its result
    target: str | None      # the node the app picked for the stage, or None when Nomad places it
    node: str | None        # the node that runs the stage, as Nomad or the worker names it
    alloc: str | None       # the stage's Nomad allocation
    client_status: str | None  # that allocation's client status at the last poll
    polled: float           # when the app last asked Nomad about the stage, in epoch seconds
    ended_seen: float | None   # when the app first saw the allocation ended, in epoch seconds
    wait: str | None        # why the stage has not started, or what its node is waiting for, for the page
    contact: str | None     # when a worker fetched the stage's bundle, as ISO time
    log_got: int            # the bytes of the worker's log of this stage that the app has
    marks: dict             # when the first line of each part of the stage arrived, by the names of jobs.LOG_MARKS
    cut_off: bool | None    # the node was seen disconnected during this run of the stage
    cancel: bool | None     # the worker is asked to stop
    stop_at: float          # when it was asked, in epoch seconds
    evict: str | bool       # the job the node goes to, for backlog work that was asked to stop for it
    requeue: str | None     # "hold" or "now": the job was asked to stop and go back into the queue, paused or not
    by_user: bool           # a plain stop came after evict, so the job is not queued again. It takes requeue off
    reports: dict           # the report each finished stage's result got, by the stage's index as text


class Job(TypedDict, total=False):
    """A job record."""
    id: str                 # <date>-<time>-<six hex digits>, which also names the job's log
    kind: str               # the CLI command the job runs, or nomad.SYNC_KIND
    params: dict            # the request's parameters, which a retry queues again
    args: list              # the CLI arguments jobs.build() made of them
    heavy: bool             # one machine runs one heavy job at a time
    gpu: bool               # the job uses the card
    comfy: bool             # the job queues ComfyUI prompts
    title: str
    slug: str | None        # the serial, or None for a job on no serial
    meta: dict              # what the rules read of the request: chapters, lines, engine, characters and the like
    batch: str | None       # the batch whose queued jobs are skipped when this one fails
    seq: int                # the job's place in the queue
    backlog: bool           # backlog work, which starts on an empty queue
    status: str             # queued, running, or one of jobs.FINISHED
    created: str            # ISO time
    started: str | None     # ISO time its process started, or its worker fetched the first bundle
    finished: str | None    # ISO time
    exit_code: int | None
    pid: int | None         # the wrapper process of a job on the app's machine
    note: str | None        # one line for the page on how the job ended or why it waits again
    stopping: bool          # a stop is under way. Not kept over a start of the app
    asked: str              # ISO time of the later request that a waiting backlog render stands in for
    asked_stop: str         # the running job this one asked to stop for its node
    evicted: bool           # a backlog render gave its node away, which it does once
    gave_way: GaveWay
    taken_back: int         # how often the job was queued again because the worker it was handed to never ran it
    lost_node: int          # how often in a row it was queued again because its node went away
    paused: bool            # a queued job that waits for resume()
    nomad: NomadPart


class JobView(Job, total=False):
    """A job as /api/state shows it: the record without its key and marks, with what the app works out for it."""
    progress: dict | None   # phase, fraction, eta and detail of a running job
    wait: str | None        # why a queued job has not started
    estimate: float         # the seconds its remaining stages are expected to take
    on: str | None          # the GPU node it is expected to run on
    start_in: float         # seconds until it is expected to start
    end_in: float           # seconds until it is expected to end
    pausable: bool          # a running job that keeps what it made when it is paused


class NodeSummary(TypedDict, total=False):
    """A Nomad node as the app schedules on it and the page shows it, from nomad.worker_summary()."""
    name: str
    id: str
    status: str             # Nomad's status of the node
    eligible: bool          # Nomad places jobs on it
    busy: bool              # the node's own meta says it is in use
    classes: list           # the classes of job its meta offers: breeze, comfyui, llm
    gpu: str | None         # nvidia, metal or none
    os: str | None          # the kernel name
    held: bool              # the app holds it, so it gets no job
    until_back: bool        # the hold ends when the node was away and is ready again
    ready: bool             # the app can send it a job now. A rule that places a job reads this
