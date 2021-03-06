from enum import Enum


class TaskState(Enum):
    NONE = 0,
    INIT = 1,
    RUN = 2,
    FINISHED = 3,
    CLEANUP = 4,
    CLEANED = 5
