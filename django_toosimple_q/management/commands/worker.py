import logging
import signal
import time

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Case, Value, When
from django.utils import timezone
from django.utils.translation import ugettext as _

from ...logging import logger, show_registry
from ...models import ScheduleExec, TaskExec
from ...registry import schedules_registry, tasks_registry


class Command(BaseCommand):

    help = _("Run tasks an schedules")

    def add_arguments(self, parser):
        queue = parser.add_mutually_exclusive_group()
        queue.add_argument(
            "--queue",
            action="append",
            help="which queue to run (can be used several times, all queues are run if not provided)",
        )
        queue.add_argument(
            "--exclude_queue",
            action="append",
            help="which queue not to run (can be used several times, all queues are run if not provided)",
        )

        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--once",
            action="store_true",
            help="run once then exit (useful for debugging)",
        )
        mode.add_argument(
            "--until_done",
            action="store_true",
            help="run until no tasks are available then exit (useful for debugging)",
        )

        parser.add_argument(
            "--tick",
            default=10.0,
            type=float,
            help="frequency in seconds at which the database is checked for new tasks/schedules",
        )

    def handle(self, *args, **options):

        if int(options["verbosity"]) > 1:
            logger.setLevel(logging.DEBUG)
        else:
            logger.setLevel(logging.INFO)

        # Handle SIGTERM and SIGINT (default_int_handler raises KeyboardInterrupt)
        # see https://stackoverflow.com/a/40785230
        signal.signal(signal.SIGINT, signal.default_int_handler)
        signal.signal(signal.SIGTERM, signal.default_int_handler)

        logger.info("Starting worker")
        show_registry()

        self.queues = options["queue"]
        self.excluded_queues = options["exclude_queue"]
        self.tick_duration = options["tick"]

        if self.queues:
            logger.info(f"Starting queues {self.queues}...")
        elif self.excluded_queues:
            logger.info(f"Starting queues except {self.excluded_queues}...")
        else:
            logger.info(f"Starting all queues...")

        last_run = timezone.now()
        while True:
            did_something = self.tick()

            if options["once"]:
                logger.info("Exiting loop because --once was passed")
                break

            if options["until_done"] and not did_something:
                logger.info("Exiting loop because --until_done was passed")
                break

            if not did_something:
                # wait for next tick
                dt = (timezone.now() - last_run).total_seconds()
                time.sleep(max(0, self.tick_duration - dt))

            last_run = timezone.now()

    def tick(self):
        """Returns True if something happened (so you can loop for testing)"""

        did_something = False

        logger.debug(f"Disabling orphaned schedules...")
        with transaction.atomic():
            count = (
                ScheduleExec.objects.exclude(state=ScheduleExec.States.INVALID)
                .exclude(name__in=schedules_registry.keys())
                .update(state=ScheduleExec.States.INVALID)
            )
            if count > 0:
                logger.warning(f"Found {count} invalid schedules")

        logger.debug(f"Disabling orphaned tasks...")
        with transaction.atomic():
            count = (
                TaskExec.objects.exclude(state=TaskExec.States.INVALID)
                .exclude(task_name__in=tasks_registry.keys())
                .update(state=TaskExec.States.INVALID)
            )
            if count > 0:
                logger.warning(f"Found {count} invalid tasks")

        logger.debug(f"Checking schedules...")
        schedules_to_check = schedules_registry.for_queue(
            self.queues, self.excluded_queues
        )
        for schedule in schedules_to_check:
            did_something |= schedule.execute(self.tick_duration)

        logger.debug(f"Waking up tasks...")
        TaskExec.objects.filter(state=TaskExec.States.SLEEPING).filter(
            due__lte=timezone.now()
        ).update(state=TaskExec.States.QUEUED)

        logger.debug(f"Checking tasks...")
        # We compile an ordering clause from the registry
        order_by_priority_clause = Case(
            *[
                When(task_name=task.name, then=Value(-task.priority))
                for task in tasks_registry.values()
            ],
            default=Value(0),
        )
        tasks_to_check = tasks_registry.for_queue(self.queues, self.excluded_queues)
        tasks_execs = TaskExec.objects.filter(state=TaskExec.States.QUEUED)
        tasks_execs = tasks_execs.filter(task_name__in=[t.name for t in tasks_to_check])
        tasks_execs = tasks_execs.order_by(order_by_priority_clause, "due", "created")
        with transaction.atomic():
            task_exec = tasks_execs.select_for_update().first()
            if task_exec:
                task_exec.started = timezone.now()
                task_exec.state = TaskExec.States.PROCESSING
                task_exec.save()

        if task_exec:
            task = tasks_registry[task_exec.task_name]
            did_something |= task.execute(task_exec)

        return did_something
